"""Независимый загрузчик и проверка Ed25519. Только стандартная библиотека."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from zipfile import ZipFile, BadZipFile

TRUST = Path("/etc/tg-number-cleaner/release-trust")
ASSET = "Cugam-Helper.zip"
MANIFEST = "Cugam-Helper.manifest.json"
SIGNATURE = "Cugam-Helper.manifest.sig"
CHANNEL = "https://github.com/Lazarev33/cugamhelper/releases/latest/download/"
MAX_ARCHIVE = 24 * 1024**2
MAX_EXPANDED = 48 * 1024**2
MAX_MANIFEST = 8192
HEX = re.compile(r"[0-9a-f]{64}")
SHA = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
NAME = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,100}")
TOP_DOT = {".github", ".gitignore", ".env.example"}
KEY_PREFIX = bytes.fromhex("302a300506032b6570032100")
CLEAN_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}


class ReleaseError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def version_number(value):
    require(isinstance(value, str) and VERSION.fullmatch(value), "Некорректная версия релиза.")
    return tuple(map(int, value.split(".")))


def safe_file(path, *, directory=False):
    info = path.lstat()
    require(info.st_uid == 0 and not info.st_mode & 0o022 and
            (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)),
            "Хранилище доверия должно принадлежать root и быть закрыто для чужой записи.")


def key_id(path):
    try:
        der = subprocess.check_output(["/usr/bin/openssl", "pkey", "-pubin", "-in", str(path), "-pubout",
                                       "-outform", "DER"], env=CLEAN_ENV, stderr=subprocess.DEVNULL, timeout=8)
    except subprocess.SubprocessError:
        raise ReleaseError("Некорректный публичный ключ.") from None
    require(der.startswith(KEY_PREFIX) and len(der) == len(KEY_PREFIX) + 32, "Ожидается Ed25519.")
    return sha256(der).hexdigest()


def unique_keys(items):
    result = {}
    for key, value in items:
        require(key not in result, "Повторяющееся поле в манифесте.")
        result[key] = value
    return result


def document(data):
    try:
        return json.loads(data, object_pairs_hook=unique_keys)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ReleaseError("Некорректный JSON манифеста.") from None


def snapshot(path, dest, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source, dest.open("xb") as target:
            info = os.fstat(source.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, "Недопустимый файл релиза.")
            size = 0
            while chunk := source.read(min(65536, limit + 1 - size)):
                size += len(chunk)
                require(size <= limit, "Файл релиза превышает лимит.")
                target.write(chunk)
    except OSError:
        raise ReleaseError("Не удалось принять файл релиза.") from None


@dataclass(frozen=True)
class Release:
    version: str
    revision: str
    checksum: str
    key: str
    size: int


def validate_manifest(path, signature, trust=TRUST, *, strict=True, now=None):
    if strict:
        for directory in (trust.parent, trust, trust / "keys"):
            safe_file(directory, directory=True)
    data = document(path.read_bytes())
    fields = {"format", "product", "version", "revision", "asset", "sha256", "size", "schema",
              "issued", "expires", "key_id", "python", "platform", "channel"}
    require(type(data) is dict and set(data) == fields, "Неизвестный формат манифеста.")
    key = data["key_id"]
    require(isinstance(key, str) and HEX.fullmatch(key), "Некорректный идентификатор ключа.")
    public = trust / "keys" / (key + ".pem")
    require(public.is_file(), "Ключ выпуска отсутствует или отозван.")
    if strict:
        safe_file(public)
    require(key_id(public) == key, "Отпечаток публичного ключа изменился.")
    result = subprocess.run(["/usr/bin/openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public),
                             "-rawin", "-in", str(path), "-sigfile", str(signature)], env=CLEAN_ENV,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
    require(result.returncode == 0, "Подпись релиза недействительна.")
    version_number(data["version"])
    now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
    require(data["product"] == "Cugam Helper" and type(data["format"]) is int and data["format"] == 1 and
            data["asset"] == ASSET and type(data["schema"]) is int and data["schema"] == 2 and
            data["channel"] == "stable" and data["python"] == ["3.11", "3.12", "3.13"] and
            data["platform"] == "linux-x86_64" and
            isinstance(data["revision"], str) and SHA.fullmatch(data["revision"]) and
            isinstance(data["sha256"], str) and HEX.fullmatch(data["sha256"]) and
            type(data["size"]) is int and 0 < data["size"] <= MAX_ARCHIVE and
            type(data["issued"]) is int and type(data["expires"]) is int and
            data["issued"] <= now + 300 and now < data["expires"] and
            0 < data["expires"] - data["issued"] <= 90 * 86400,
            "Манифест просрочен, несовместим или содержит неверные поля.")
    return Release(data["version"], data["revision"], data["sha256"], key, data["size"])


def verify_archive(archive, release, destination=None):
    with archive.open("rb") as source:
        checksum = sha256()
        for block in iter(lambda: source.read(65536), b""):
            checksum.update(block)
        require(source.tell() == release.size and checksum.hexdigest() == release.checksum,
                "Размер или SHA-256 архива не совпали.")
        source.seek(0)
        try:
            with ZipFile(source) as package:
                names, total = set(), 0
                require(len(package.infolist()) <= 256, "Слишком много файлов в ZIP.")
                for item in package.infolist():
                    name = item.orig_filename
                    rel = PurePosixPath(name)
                    parts = rel.parts
                    mode = (item.external_attr >> 16) & 0xFFFF
                    require(name == item.filename and '\\' not in name and not rel.is_absolute() and
                            name.rstrip("/") == rel.as_posix() and 1 <= len(parts) <= 4 and all(
                                NAME.fullmatch(part) or (index == 0 and part == "cugam-document@.service") or (index == 0 and part in TOP_DOT)
                                for index, part in enumerate(parts)) and
                            not any(part in (".", "..") for part in parts) and name.rstrip("/") not in names and
                            stat.S_IFMT(mode) in (0, stat.S_IFDIR if item.is_dir() else stat.S_IFREG) and
                            not item.flag_bits & 1 and
                            (item.is_dir() or not mode & 0o7111 or name in {"install.sh", "cli.py"}),
                            "Недопустимый путь, тип или режим файла в ZIP.")
                    names.add(name.rstrip("/"))
                    total += item.file_size
                    require(total <= MAX_EXPANDED, "Слишком большой развёрнутый ZIP.")
                require(package.read("VERSION").strip().decode("ascii") == release.version and
                        package.read("REVISION").strip().decode("ascii") == release.revision,
                        "ZIP относится к другой версии или Git-ревизии.")
                require(package.testzip() is None, "Повреждённое содержимое ZIP.")
                if destination is not None:
                    destination.mkdir(mode=0o700)
                    for item in package.infolist():
                        output = destination / item.filename
                        if item.is_dir():
                            output.mkdir(parents=True, exist_ok=True)
                        else:
                            output.parent.mkdir(parents=True, exist_ok=True)
                            with package.open(item) as incoming, output.open("xb") as outgoing:
                                for block in iter(lambda: incoming.read(65536), b""):
                                    outgoing.write(block)
        except (BadZipFile, KeyError, RuntimeError, UnicodeError, NotImplementedError):
            raise ReleaseError("Повреждённый или несовместимый ZIP.") from None


def verify_bundle(archive, manifest, signature, work, trust=TRUST, *, strict=True, extract=False):
    snapshots = [work / name for name in (ASSET, MANIFEST, SIGNATURE)]
    snapshot(manifest, snapshots[1], MAX_MANIFEST)
    snapshot(signature, snapshots[2], 64)
    release = validate_manifest(*snapshots[1:], trust, strict=strict)
    snapshot(archive, snapshots[0], MAX_ARCHIVE)
    verify_archive(snapshots[0], release, work / "source" if extract else None)
    return release


def check_floor(release, path, installed=None):
    if installed:
        require(version_number(release.version) >= version_number(installed), "Версия старше установленной.")
    if path.exists() or path.is_symlink():
        safe_file(path)
        saved = document(path.read_bytes())
        offered = version_number(release.version)
        floor = version_number(saved["version"])
        require(offered > floor or (offered == floor and release.checksum == saved["sha256"]),
                "Устаревший или подменённый выпуск отклонён.")


def record_floor(release, path):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as target:
        pending = Path(target.name)
        target.write(json.dumps({"version": release.version, "sha256": release.checksum}).encode())
        target.flush()
        os.fsync(target.fileno())
    try:
        pending.replace(path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        pending.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["key-id", "verify", "install"])
    parser.add_argument("files", type=Path, nargs="+")
    parser.add_argument("--trust", type=Path, default=TRUST)
    args = parser.parse_args()
    if args.action == "key-id" and len(args.files) == 1:
        print(key_id(args.files[0]))
        return
    require(len(args.files) == 3, "Укажи ZIP, манифест и подпись.")
    install = args.action == "install"
    require(not install or (os.geteuid() == 0 and args.trust == TRUST), "Для установки нужен системный каталог доверия.")
    if install:
        safe_file(TRUST.parent, directory=True)
        safe_file(TRUST, directory=True)
    with tempfile.TemporaryDirectory(prefix="cugam-verification-", dir=TRUST if install else None) as tmp:
        work = Path(tmp)
        release = verify_bundle(*args.files, work, args.trust, strict=install, extract=install)
        print(f"Подпись Ed25519 и архив проверены. Версия {release.version}, Git {release.revision}.", flush=True)
        if install:
            import platform
            require(".".join(map(str, sys.version_info[:2])) in {"3.11", "3.12", "3.13"}
                    and platform.machine() == "x86_64",
                    "Поддерживаются Linux x86_64 и системный CPython 3.11–3.13.")
            subprocess.run(["/usr/bin/python3", "-Es", str(work / "source/deployment.py"),
                            str(work / ASSET), str(work / MANIFEST), str(work / SIGNATURE)],
                           env=CLEAN_ENV, check=True)


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Установка отклонена: {error}") from None
