"""Интеграция HTTP, постоянных разрешений и контролируемого транспорта Telegram."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
import re
import sqlite3
import time

from fastapi.testclient import TestClient
from secrets import token_urlsafe

from catalog import Catalog
from infrastructure import Infrastructure, Identity, InfraError, digest, revision, IntegrationFeatures
from infrastructure_api import create_app, NsuExport, WebSettings
from settings import Settings
from telegram_bridge import Exports, dispatch, listen, split_lines, deliver

OWNER, ADMIN, EMPLOYEE, OTHER = 10001, 10002, 10003, 10004
ORIGIN = "http://localhost:8765"
PREFIX = "/api/v1"
AUTH = {"Origin": ORIGIN}


def result(key=None, first=None, second=None, **values):
    return {"request_id": key or token_urlsafe(18), "mode": "pairs" if second is None else "two_passes",
            "report": {"matched": 2, "expected": 2, "missing_count": 0, "extra_count": 0, **values},
            "first": first or ["00A", "  Ab", "00A", "  Ab"], "second": second or []}


class Fixture:
    def create(self, path, clock=time.time):
        service = Infrastructure(path, settings=lambda: Settings(owner_telegram_id=OWNER), clock=clock)
        service.prepare()
        with service.repository() as repo:
            service.put_meta(repo, "bot_username", "SyntheticHelperBot")
            repo.set_admin(OWNER, ADMIN, True)
            repo.change_employee(ADMIN, EMPLOYEE, "give", name="Смена 2")
            repo.change_employee(ADMIN, OTHER, "give")
        return service

    def link(self, service, uid, scope="android"):
        verifier = token_urlsafe(32)
        challenge, _, _ = service.link_start(uid, "Тестовый Android", "0.0.1", digest(verifier), scope)
        if not service.link_for_telegram(uid, challenge, True):
            raise AssertionError("Подтверждение тестового пользователя отклонено")
        return service.exchange(challenge, verifier, scope)


class ApiTest(Fixture, TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "catalog.sqlite3"
        self.now = 100000
        self.service = self.create(self.path, lambda: self.now)
        self.sent = []

        async def send(sid, text):
            self.sent.append(text)
            return "delivered"

        self.app = create_app(self.service, transport=send)
        context = TestClient(self.app, base_url=ORIGIN)
        self.client = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def post(self, path, data, headers=None):
        return self.client.post(PREFIX + path, json=data, headers=headers)

    def auth(self, uid=EMPLOYEE):
        security = self.link(self.service, uid)
        return {"Authorization": "Bearer " + security["access_token"]}, security

    def test_link_bound_to_actual_telegram_user_and_client(self):
        verifier = token_urlsafe(32)
        start = self.post("/auth/link/start", {"telegram_id": EMPLOYEE, "device_name": "ТСД", "proof_sha256": digest(verifier)})
        self.assertEqual(start.status_code, 201, start.text)
        challenge = start.json()["challenge"]
        payload = {"challenge": challenge, "verifier": verifier}
        self.assertIn("start=link_" + challenge, start.json()["telegram_url"])
        self.assertIsNone(self.service.link_for_telegram(OTHER, challenge, True))
        self.assertEqual(self.post("/auth/link/exchange", payload).json(), {"code": "link_pending"})
        self.assertTrue(self.service.link_for_telegram(EMPLOYEE, challenge, True))
        self.assertIsNone(self.service.link_for_telegram(EMPLOYEE, challenge, True))
        self.assertEqual(self.post("/auth/link/exchange", {**payload, "verifier": token_urlsafe(32)}).status_code, 404)
        self.assertEqual(self.post("/auth/link/exchange", {**payload, "device_name": "Подмена"}).status_code, 422)
        session = self.post("/auth/link/exchange", payload)
        self.assertEqual(session.status_code, 201)
        self.assertEqual(self.post("/auth/link/exchange", payload).status_code, 404)
        self.assertEqual(self.client.get(PREFIX + "/auth/me", headers={
            "Authorization": "Bearer " + session.json()["access_token"]}).json()["telegram_id"], EMPLOYEE)

    def test_expired_challenge_and_unknown_user(self):
        proof = digest(token_urlsafe(32))
        old, _, _ = self.service.link_start(EMPLOYEE, "ТСД", "", proof, "android")
        unknown, _, _ = self.service.link_start(10099, "ТСД", "", proof, "android")
        self.assertIsNone(self.service.link_for_telegram(10099, unknown, True))
        self.now += 301
        self.assertIsNone(self.service.link_for_telegram(EMPLOYEE, old, True))

    def test_revocation_and_rotation(self):
        headers, security = self.auth()
        self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 401)
        self.assertEqual(self.client.get(PREFIX + "/auth/me", headers={"Authorization": "Bearer invalid"}).status_code, 401)
        next_session = self.post("/auth/refresh", {"refresh_token": security["refresh_token"]})
        self.assertEqual(next_session.status_code, 200)
        self.assertEqual(self.post("/auth/refresh", {"refresh_token": security["refresh_token"]}).status_code, 401)
        self.assertEqual(self.client.get(PREFIX + "/auth/me", headers=headers).status_code, 401)
        rotated = {"Authorization": "Bearer " + next_session.json()["access_token"]}
        with self.service.repository() as repo:
            row = repo.employee(EMPLOYEE)
            repo.change_employee(ADMIN, EMPLOYEE, "disable", row["revision"])
            repo.change_employee(ADMIN, EMPLOYEE, "restore", row["revision"]+1)
        self.assertEqual(self.client.get(PREFIX + "/auth/me", headers=rotated).status_code, 401)

    def test_device_cannot_be_accessed_by_other_user(self):
        headers, one = self.auth()
        _, other = self.auth(OTHER)
        self.assertEqual(self.client.delete(PREFIX + "/devices/" + other["device_id"], headers=headers).status_code, 404)
        items = self.client.get(PREFIX + "/devices?page=0", headers=headers).json()["items"]
        self.assertEqual([row["id"] for row in items], [one["device_id"]])
        self.assertEqual(self.client.delete(PREFIX + "/devices/" + one["device_id"], headers=headers).status_code, 204)
        self.assertEqual(self.client.get(PREFIX + "/auth/me", headers=headers).status_code, 401)

    def test_sync_etag_and_global_gates(self):
        from infra_features import write_common
        auth, _ = self.auth()
        url = PREFIX + "/catalog"
        initial = self.client.get(url, headers=auth)
        self.assertTrue(initial.json()["items"])
        self.assertEqual(self.client.get(url, headers={**auth, "If-None-Match": initial.headers["etag"]}).status_code, 304)
        with self.service.repository() as repo:
            features = IntegrationFeatures(self.service)
            current = features.current().options()
            write_common(repo.db, {**current,"barcode_catalog":False}, revision(current), features.legacy.current)
        self.assertEqual(self.client.get(url, headers=auth).json()["items"], [])
        self.assertFalse(self.client.get(PREFIX + "/features", headers=auth).json()["android"]["catalog_sync"])

    def test_nsu_real_api_worker_and_idempotency(self):
        headers, _ = self.auth()
        payload = result(second=["00A", "  Ab", "  Ab", "00A"])
        self.assertEqual(self.post("/telegram/exports", {**payload, "chat_id": OTHER}, headers).status_code, 422)
        accepted = self.post("/telegram/exports", payload, headers)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        for _ in range(40):
            delivery = self.client.get(PREFIX + "/telegram/exports/" + payload["request_id"], headers=headers)
            if delivery.json()["status"] == "delivered":
                break
            time.sleep(0.02)
        self.assertEqual(delivery.json()["confirmed_parts"], 3)
        self.assertEqual(self.sent[1:], ["00A\n  Ab\n00A\n  Ab", "00A\n  Ab\n  Ab\n00A"])
        self.assertTrue(self.sent[0].startswith("НСУ"))
        self.assertEqual(self.post("/telegram/exports", payload, headers).status_code, 202)
        self.assertEqual(self.post("/telegram/exports", {**payload, "first": ["другая строка"]}, headers).status_code, 409)
        self.assertEqual(len(self.sent), 3)
        self.assertFalse(any(record.parts for record in self.app.state.exports.records.values()))
        self.assertNotIn(b"00A", self.path.read_bytes())
        self.assertNotIn(b"chat_id", accepted.content)

    def test_size_and_rate_limits(self):
        response = self.client.post(PREFIX + "/telegram/exports", content=b"x" * 524289,
                                    headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 413)
        for _ in range(12):
            self.assertEqual(self.post("/auth/link/start", {}).status_code, 422)
        self.assertEqual(self.post("/auth/link/start", {}).status_code, 429)

    def test_web_recovery_csrf_and_live_updates(self):
        self.assertEqual(self.client.get("/features", follow_redirects=False).status_code, 303)
        code = self.service.recovery_issue()
        entry = self.client.post("/web/recovery", headers=AUTH, data={"code":code}, follow_redirects=False)
        self.assertEqual(entry.status_code, 303)
        self.assertIn("HttpOnly", entry.headers.get_list("set-cookie")[0])
        page = self.client.get("/employees")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        form = {"action": "give", "uid": "10099", "name": "<b>Команда</b>", "csrf": csrf}
        self.assertEqual(self.client.post("/web/employee", data=form).status_code, 403)
        self.assertEqual(self.client.post("/web/employee", headers=AUTH, data={**form,"csrf":"wrong"}).status_code, 403)
        self.assertEqual(self.client.post("/web/employee", headers=AUTH, data=form).status_code, 200)
        with self.service.repository() as repo:
            self.assertTrue(repo.allowed(10099))
        self.assertIn("&lt;b&gt;Команда&lt;/b&gt;", self.client.get("/employees?id=10099").text)
        second = self.client.post("/web/recovery", headers=AUTH, data={"code":code}, follow_redirects=False)
        self.assertIn("invalid_login", second.headers["location"])

    def test_admin_cannot_get_owner_privilege_and_revoked_web_session(self):
        token = self.link(self.service, ADMIN, "web")["access_token"]
        self.client.cookies.set("cugam_session", token)
        page = self.client.get("/features")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        self.assertEqual(self.client.post("/web/admin", headers=AUTH, data={"uid":"10099","csrf":csrf}).status_code, 403)
        self.assertEqual(self.client.post("/web/features", headers=AUTH, data={"mode":"client","csrf":csrf}).status_code, 403)
        with self.service.repository() as repo:
            repo.set_admin(OWNER, ADMIN, False)
        self.assertEqual(self.client.get("/features", follow_redirects=False).headers["location"], "/login")

    def test_https_deployment_cookie(self):
        secure = create_app(self.service, config=WebSettings(host="cugam.example", local=False))
        with TestClient(secure, base_url="https://cugam.example") as client:
            code = self.service.recovery_issue()
            response = client.post("/web/recovery", headers={"Origin":"https://cugam.example"},
                                   data={"code":code}, follow_redirects=False)
            self.assertIn("__Host-cugam_session=", response.headers.get_list("set-cookie")[0])
            self.assertIn("Secure", response.headers.get_list("set-cookie")[0])
            self.assertEqual(client.get("http://cugam.example/api/v1/health").status_code, 400)


class ExportTests(Fixture, IsolatedAsyncioTestCase):
    async def test_partial_and_unknown_delivery(self):
        user = Identity("session", EMPLOYEE, "android", "device", "")
        for transport, expected in ((["delivered","failed"], "partially_delivered"),
                                    (["delivered","delivery_unknown"], "delivery_unknown")):
            send = AsyncMock(side_effect=transport)
            pool = Exports(lambda _: True, send)
            record = pool.submit(user, "a", ["отчёт", "000441"])
            await pool.drain()
            self.assertEqual((record.status, record.confirmed, record.parts), (expected, 1, []))
            self.assertEqual(send.await_count, 2)

    async def test_pending_revoke_expiration_and_status_ttl(self):
        now, eligible = 0, True
        send = AsyncMock(return_value="delivered")
        user = Identity("session", EMPLOYEE, "android", "device", "")
        pool = Exports(lambda _: eligible, send, lambda: now)
        invalidated = pool.submit(user, "a", ["отчёт"])
        eligible = False
        await pool.drain()
        self.assertEqual((invalidated.status, invalidated.parts), ("cancelled", []))
        eligible = True
        now = 10
        expired = pool.submit(user, "b", ["отчёт"])
        now = 131
        await pool.drain()
        self.assertEqual((expired.status, expired.parts), ("expired", []))
        self.assertEqual(send.await_count, 0)
        now = 611
        pool.sweep()
        self.assertFalse(pool.records)

    async def test_unix_bridge_cannot_choose_recipient(self):
        with TemporaryDirectory() as path:
            path = Path(path)
            service = self.create(path / "db.sqlite3")
            token = self.link(service, EMPLOYEE)["access_token"]
            with service.repository() as repo:
                sid = service.identify(repo, token, "android").id
            bot = AsyncMock()
            try:
                runner = await listen(service, bot, IntegrationFeatures(service), path / "bridge")
            except PermissionError:
                if os.getenv("CI"):
                    raise
                self.skipTest("Локальная песочница запрещает AF_UNIX; тест обязателен в CI.")
            try:
                self.assertEqual(await dispatch(path / "bridge", sid, "00 Ab"), "delivered")
                self.assertEqual(bot.send_message.await_args.args, (EMPLOYEE, "00 Ab"))
                self.assertIsNone(bot.send_message.await_args.kwargs["parse_mode"])
                with service.repository() as repo:
                    repo.change_employee(ADMIN, EMPLOYEE, "disable", 1)
                self.assertEqual(await dispatch(path / "bridge", sid, "00 Ab"), "cancelled")
                self.assertEqual(bot.send_message.await_count, 1)
            finally:
                await runner.cleanup()

    async def test_delivery_to_verified_recipient_and_denial_after_revoke(self):
        with TemporaryDirectory() as path:
            service = self.create(Path(path) / "db.sqlite3")
            access = self.link(service, EMPLOYEE)["access_token"]
            with service.repository() as repo:
                sid = service.identify(repo, access, "android").id
            bot = AsyncMock()
            policy = IntegrationFeatures(service)
            self.assertEqual(await deliver(service, bot, policy, sid, "00 Ab"), "delivered")
            self.assertEqual(bot.send_message.await_args.args, (EMPLOYEE, "00 Ab"))
            self.assertIsNone(bot.send_message.await_args.kwargs["parse_mode"])
            with service.repository() as repo:
                repo.change_employee(ADMIN, EMPLOYEE, "disable", 1)
            self.assertEqual(await deliver(service, bot, policy, sid, "00 Ab"), "cancelled")
            self.assertEqual(bot.send_message.await_count, 1)

    async def test_nsu_source_and_large_chunks(self):
        lines = ["Aa_" + str(n).zfill(5) + " " * 9 for n in range(900)]
        rendered = split_lines(lines)
        self.assertGreater(len(rendered), 1)
        self.assertEqual("\n".join(rendered).split("\n"), lines)
        for matched, missing, extra, mode in ((50,0,0,"pairs"), (49,1,0,"pairs"), (50,0,1,"pairs"),
                                                (50,0,0,"two_passes"), (49,1,0,"two_passes"),
                                                (50,0,1,"two_passes")):
            payload = NsuExport.model_validate(result(first=lines, second=lines if mode=="two_passes" else None,
                                                       matched=matched, expected=50, missing_count=missing,
                                                       extra_count=extra, missing=["00009"] if missing else [],
                                                       extra=["A a"] if extra else []))
            report, *messages = payload.messages()
            self.assertIn("обнаружены расхождения" if missing or extra else "совпадает", report)
            self.assertEqual(messages, rendered * (2 if mode=="two_passes" else 1))

    async def test_additive_migration_snapshot_and_legacy_access(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite3"
            old = Catalog(path, OWNER, seed=lambda:["CODeX"])
            old.change_employee(OWNER, EMPLOYEE, "give")
            old.close()
            service = Infrastructure(path, settings=lambda: Settings(owner_telegram_id=OWNER))
            service.prepare()
            service.prepare()
            with sqlite3.connect(path.with_name(path.name + ".pre-infrastructure.bak")) as snapshot:
                self.assertEqual(snapshot.execute("SELECT value FROM barcodes").fetchone()[0], "CODeX")
                self.assertEqual(snapshot.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertFalse(snapshot.execute("SELECT name FROM sqlite_master WHERE name='infra_links'").fetchone())
            with service.repository() as repo:
                self.assertTrue(repo.allowed(EMPLOYEE))
                self.assertEqual(repo.get(1).value, "CODeX")


class TelegramLinkTests(Fixture, IsolatedAsyncioTestCase):
    async def test_aiogram_start_and_confirm_in_private_chat(self):
        from aiogram import Bot
        from aiogram.methods import SendMessage
        from aiogram.types import Update
        from bot import dp
        from protection import Protection
        from test_bot import TelegramStub

        with TemporaryDirectory() as path:
            db = self.create(Path(path) / "db.sqlite3")
            verifier = token_urlsafe(32)
            challenge, _, _ = db.link_start(EMPLOYEE, "Демонстрационный ТСД", "", digest(verifier), "android")
            session = TelegramStub()
            guard = Protection(Settings())
            await dp.storage.close()
            try:
                async with Bot("123456:unit_test_token", session=session) as bot:
                    with db.repository() as repo:
                        person = {"id": EMPLOYEE, "is_bot": False, "first_name": "Тест"}
                        chat = {"id": EMPLOYEE, "type": "private"}
                        message = {"message_id": 1, "date": 1, "chat": chat, "from": person,
                                   "text": "/start link_" + challenge}
                        await dp.feed_update(bot, Update.model_validate({"update_id": 301, "message": message}),
                                             repo=repo, guard=guard, infrastructure=db)
                        for pool in guard.pools:
                            for job in tuple(pool.tasks):
                                await job
                        sent = [method for method in session.requests if isinstance(method, SendMessage)]
                        self.assertIn("Демонстрационный ТСД", sent[0].text)
                        button = sent[0].reply_markup.inline_keyboard[0][0].callback_data
                        callback = {"id":"c1","chat_instance":"demo","from":person,"data":button,"message":message}
                        await dp.feed_update(bot, Update.model_validate({"update_id":302, "callback_query":callback}),
                                             repo=repo, guard=guard, infrastructure=db)
                        self.assertTrue(db.exchange(challenge, verifier, "android")["device_id"])
            finally:
                await guard.close()


class InfrastructureDeploymentTests(TestCase):
    def test_failed_api_start_restores_existing_bot_and_service_unit(self):
        import deployment
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "old", root / "new"
            old.mkdir()
            new.mkdir()
            app = root / "app"
            app.symlink_to(old)
            bot_unit, api_unit = root / deployment.SERVICE, root / deployment.API_SERVICE
            bot_unit.write_bytes(b"old bot")
            (new / deployment.SERVICE).write_bytes(b"new bot")
            (new / deployment.API_SERVICE).write_bytes(b"api")
            with patch.multiple(deployment, APP_DIR=app, RELEASES=root, UNIT_FILE=bot_unit,
                                API_UNIT=api_unit, CLI_LINK=root/"cli"), \
                 patch.object(deployment, "service_active", return_value=True), \
                 patch.object(deployment, "systemctl") as control, \
                 patch.object(deployment, "start_and_check") as start_bot, \
                 patch.object(deployment, "start_api_and_check", side_effect=deployment.DeploymentError("test failure")):
                with self.assertRaisesRegex(deployment.DeploymentError, "Восстановлена"):
                    deployment.activate(new, start_service=True, install=False)
            self.assertEqual(app.resolve(), old)
            self.assertEqual(bot_unit.read_bytes(), b"old bot")
            self.assertFalse(api_unit.exists())
            control.assert_any_call("stop", deployment.API_SERVICE)
            start_bot.assert_called_once()
