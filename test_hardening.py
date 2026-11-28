"""Верификация подписи, изоляции протокола и безопасной политики."""
from base64 import b64encode
from contextlib import closing
from io import BytesIO
from pathlib import Path
import json
import os
import sqlite3
import stat
import struct
import subprocess
from tempfile import TemporaryDirectory
import time
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

from admin_guard import approve, consume, fingerprint
from build_release import freeze_tree, isolated_build
from catalog import Catalog
from deployment import update
from infrastructure import Infrastructure
from isolated_worker import serve, MAX_INPUT
from retention import sweep, prepare_restore
from secure_release import (key_id, verify_bundle, check_floor, record_floor, ReleaseError,
                            ASSET, MANIFEST, SIGNATURE, sha256)
from settings import Settings


class SignedReleaseTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.key, self.pub = self.path / "private", self.path / "public"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", self.key], check=True)
        subprocess.run(["openssl", "pkey", "-in", self.key, "-pubout", "-out", self.pub], check=True)
        self.trust = self.path / "trust"
        (self.trust / "keys").mkdir(parents=True)
        self.key_id = key_id(self.pub)
        self.installed = self.trust / "keys" / (self.key_id + ".pem")
        self.installed.write_bytes(self.pub.read_bytes())
        self.archive = self.path / ASSET
        self.manifest = self.path / MANIFEST
        self.signature = self.path / SIGNATURE
        self.digest = "a" * 40

    def release(self, *, broken_name=None, link=False, version="3.1.0", zip_version=None, expires=1000, sign=True):
        with ZipFile(self.archive, "w") as zip:
            zip.writestr("VERSION", (zip_version or version) + "\n")
            zip.writestr("REVISION", self.digest + "\n")
            if broken_name:
                zip.writestr(broken_name, b"test")
            if link:
                info = ZipInfo("docs/symlink")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zip.writestr(info, "VERSION")
        arc = self.archive.read_bytes()
        self.manifest.write_text(json.dumps({
            "format": 1, "product":"Cugam Helper", "version":version, "revision":self.digest,
            "asset":ASSET, "sha256":sha256(arc).hexdigest(), "size":len(arc), "schema":2,
            "issued":int(time.time()) - 1, "expires":int(time.time()) + expires, "key_id":self.key_id,
            "python":["3.10", "3.12"], "platform":"linux-x86_64", "channel":"stable"}))
        if sign:
            subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", self.key,
                            "-in", self.manifest, "-out", self.signature], check=True)

    def verify(self, extract=False):
        with TemporaryDirectory() as work:
            work = Path(work)
            result = verify_bundle(self.archive, self.manifest, self.signature, work, self.trust,
                                   strict=False, extract=extract)
            if extract:
                self.assertEqual((work / "source" / "VERSION").read_text().strip(), result.version)
            return result

    def test_correct_signature_and_private_snapshot(self):
        self.release()
        self.verify(extract=True)
        import secure_release
        real_verify = secure_release.verify_archive

        def replace_source(snapshot, *args):
            self.archive.write_bytes(b"substitution after snapshot")
            return real_verify(snapshot, *args)
        with patch.object(secure_release, "verify_archive", side_effect=replace_source):
            self.verify(extract=True)

    def test_missing_and_bad_signatures(self):
        self.release(sign=False)
        with self.assertRaises(ReleaseError):
            self.verify()
        self.signature.write_bytes(b"a" * 64)
        with self.assertRaisesRegex(ReleaseError, "Подпись"):
            self.verify()

    def test_revoked_and_unknown_keys(self):
        self.release()
        self.installed.unlink()
        with self.assertRaisesRegex(ReleaseError, "отозван"):
            self.verify()

    def test_wrong_archive_and_manifest(self):
        self.release()
        self.manifest.write_bytes(self.manifest.read_bytes() + b" ")
        with self.assertRaisesRegex(ReleaseError, "Подпись"):
            self.verify()
        self.release()
        self.archive.write_bytes(b"modified")
        with self.assertRaisesRegex(ReleaseError, "SHA-256"):
            self.verify()

    def test_expired_version_and_paths(self):
        for options in ({"zip_version":"3.0.0"}, {"expires":-1}, {"broken_name":"../outside"},
                        {"broken_name":"/outside"}, {"link":True}):
            with self.subTest(options=options):
                self.release(**options)
                with self.assertRaises(ReleaseError):
                    self.verify(extract=True)

    def test_floor_rejects_older_version_and_different_same_version(self):
        self.release()
        first = self.verify()
        floor = self.trust / "floor.json"
        record_floor(first, floor)
        with patch("secure_release.safe_file"):
            check_floor(first, floor, "3.0.0")
            for version, extra in (("3.0.0", None), ("3.1.0", "another.txt")):
                self.release(version=version, broken_name=extra)
                with self.assertRaises(ReleaseError):
                    check_floor(self.verify(), floor)

    def test_failure_precedes_switch(self):
        from contextlib import nullcontext
        with patch("deployment.maintenance_lock", return_value=nullcontext()), \
             patch("deployment.TRUST", self.trust), patch("deployment.safe_file"), \
             patch("deployment.available_release", side_effect=ReleaseError("нет доверия")), \
             patch("deployment.deploy") as deploy:
            with self.assertRaisesRegex(RuntimeError, "нет доверия"):
                update(configured=lambda: True)
        deploy.assert_not_called()

    def test_build_invokes_systemd_uid_and_refuses_artifact_symlink(self):
        with patch("build_release.subprocess.run") as call:
            isolated_build("cugambuild", ["/usr/bin/python3", "-V"])
        argv = call.call_args.args[0]
        self.assertIn("--uid=cugambuild", argv)
        self.assertIn("--property=ProtectSystem=strict", argv)
        tree = self.path / "output"
        tree.mkdir()
        (tree / "unexpected").symlink_to("/etc/passwd")
        with self.assertRaises(ValueError):
            freeze_tree(tree)


def worker_request(operation, payload):
    response = BytesIO()
    serve(BytesIO(operation + struct.pack(">I", len(payload)) + payload), response)
    return json.loads(response.getvalue()[4:])


class WorkerTests(TestCase):
    def test_roundtrip_literal_text(self):
        data = {"items":[{"kind":"text","opts":{},"data":b64encode(b"0007\n0002").decode()}],
                "mode":"asc","limit":100,"expected":None}
        response = worker_request(b"D", json.dumps(data).encode())
        self.assertEqual(response["outputs"][0]["data"], [["0002", "0007"]])

    def test_frame_and_operation_bounds(self):
        result = BytesIO()
        serve(BytesIO(b"D" + struct.pack(">I", MAX_INPUT+1)), result)
        self.assertIn("error", json.loads(result.getvalue()[4:]))
        self.assertIn("error", worker_request(b"X", b"1"))
        self.assertIn("error", worker_request(b"D", b"{}"))
        self.assertIn("error", worker_request(b"I", b"bad-image"))


class GovernanceTests(TestCase):
    def test_preview_retention_and_fail_closed_restore(self):
        with TemporaryDirectory() as root:
            root = Path(root)
            db = root / "main.db"
            service = Infrastructure(db, settings=lambda: Settings(owner_telegram_id=42))
            service.prepare()
            with service.repository() as repo:
                repo.change_employee(42, 43, "give")
                repo.set_admin(42, 44, True)
                repo.remember_start(45, "synthetic", "")
                repo.db.execute("UPDATE start_visitors SET seen=1")
            cfg = root / "policy"
            proposal = {"enabled":False,"visitors":7,"employees_revoked":30,"journal":90,
                        "devices_revoked":30,"backups":None}
            cfg.write_text(json.dumps(proposal))
            peek = sweep(db, cfg, root, apply=True)
            self.assertEqual(peek["counts"]["visitors"], 1)
            self.assertEqual(peek["mode"], "предпросмотр")
            cfg.write_text(json.dumps({**proposal, "enabled":True}))
            self.assertEqual(sweep(db, cfg, root, apply=True)["counts"]["visitors"], 1)
            self.assertEqual(sweep(db, cfg, root, apply=True)["counts"]["visitors"], 0)
            recovered = root / "restored"
            prepare_restore(db, recovered)
            with service.repository() as repo:
                self.assertTrue(repo.allowed(43))
            old = Catalog(recovered, owner=42)
            try:
                self.assertTrue(old.allowed(42))
                self.assertFalse(old.allowed(43))
                self.assertFalse(old.allowed(44))
                self.assertTrue(old.get(1))
            finally:
                old.close()

    def test_confirmation_bound_and_single_use(self):
        with TemporaryDirectory() as root:
            db = Path(root) / "test.db"
            value = fingerprint(["session", "role", 19])
            approve(db, 42, "role", value)
            with closing(sqlite3.connect(db)) as conn:
                self.assertFalse(consume(conn, 41, "role", value))
                self.assertFalse(consume(conn, 42, "devices", value))
                self.assertTrue(consume(conn, 42, "role", value))
                self.assertFalse(consume(conn, 42, "role", value))
