"""Установка и ручное обновление бота с откатом к рабочей версии."""

import argparse
import platform
import pwd
from contextlib import contextmanager
import fcntl
import http.client
import json
import socket
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from build_release import isolated_build, freeze_tree
from secure_release import (TRUST, CHANNEL, ASSET, MANIFEST, SIGNATURE, MAX_ARCHIVE, MAX_MANIFEST,
                            CLEAN_ENV, ReleaseError, validate_manifest, verify_bundle, check_floor,
                            record_floor, safe_file)


APP_DIR = Path("/opt/tg-number-cleaner")
RELEASES = Path("/opt/tg-number-cleaner-releases")
CONFIG_DIR = Path("/etc/tg-number-cleaner")
SERVICE = "tg-number-cleaner.service"
UNIT_FILE = Path("/etc/systemd/system") / SERVICE
API_SERVICE = "cugam-api.service"
API_UNIT = Path("/etc/systemd/system") / API_SERVICE
WORKER_SOCKET = "cugam-document.socket"
WORKER_FILES = (WORKER_SOCKET, "cugam-document@.service", "cugam-retention.service", "cugam-retention.timer")
CLI_LINK = Path("/usr/local/bin/numberbot")
LOCK_FILE = CONFIG_DIR / "maintenance.lock"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
RELEASE_FILE = re.compile(r"(?:docs/)?[A-Za-z0-9][A-Za-z0-9_.-]*")
REQUIRED_FILES = {
    "VERSION", "bot.py", "branding.py", "parser.py", "barcodes.py", "barcodes.json",
    "cli.py", "deployment.py", "requirements.txt", SERVICE,
}


class DeploymentError(RuntimeError):
    pass


def checked(args: list, *, env=None, output=False, timeout=120) -> str:
    result = subprocess.run(
        args, check=True, env=env, timeout=timeout, text=True,
        stdout=subprocess.PIPE if output else None,
    )
    return result.stdout.strip() if output else ""


def systemctl(*args: str) -> None:
    checked(["systemctl", *args], timeout=100)


def service_active() -> bool:
    return subprocess.run(
        ["systemctl", "is-active", "--quiet", SERVICE], timeout=10, check=False,
    ).returncode == 0


@contextmanager
def maintenance_lock():
    with LOCK_FILE.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeploymentError("Другая команда уже меняет установку или состояние службы.") from None
        yield


@contextmanager
def protect_switch():
    # После остановки службы завершаем переключение или откат даже при выходе из SSH.
    signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
    handlers = {sig: signal.signal(sig, signal.SIG_IGN) for sig in signals}
    try:
        yield
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def revision_at(path: Path) -> str | None:
    revision = path / "REVISION"
    if revision.is_file():
        value = revision.read_text(encoding="ascii").strip()
        if SHA_PATTERN.fullmatch(value):
            return value
    return None


def release_label(path: Path) -> str:
    version = path / "VERSION"
    label = f"v{version.read_text(encoding='ascii').strip()}" if version.is_file() else "v2.1"
    revision = revision_at(path)
    return f"{label} ({revision[:12]})" if revision else f"{label} (без ревизии)"


def download_asset(name, target, limit):
    if name not in {ASSET, MANIFEST, SIGNATURE}:
        raise DeploymentError("Неизвестный артефакт выпуска.")
    # stdout получает root в приватный временный каталог; загрузчик не может заменить файл.
    args = ["/usr/sbin/runuser", "-u", "cugambuild", "--", "/usr/bin/curl", "-q", "--fail", "--silent",
            "--show-error", "--proto", "=https", "--proto-redir", "=https", "--location", "--max-redirs", "3",
            "--max-time", "45", "--max-filesize", str(limit), CHANNEL + name]
    with subprocess.Popen(args, stdout=subprocess.PIPE, env=CLEAN_ENV) as transfer, target.open("xb") as output:
        try:
            used = 0
            while chunk := transfer.stdout.read(65536):
                used += len(chunk)
                if used > limit:
                    raise DeploymentError("Загруженный файл превышает лимит.")
                output.write(chunk)
            if transfer.wait(timeout=5) != 0:
                raise DeploymentError("Не удалось загрузить релиз по HTTPS.")
        finally:
            if transfer.poll() is None:
                transfer.kill()


def available_release(work):
    download_asset(MANIFEST, work / MANIFEST, MAX_MANIFEST)
    download_asset(SIGNATURE, work / SIGNATURE, 64)
    result = validate_manifest(work / MANIFEST, work / SIGNATURE, TRUST)
    installed = (APP_DIR / "VERSION").read_text().strip() if (APP_DIR / "VERSION").is_file() else None
    check_floor(result, TRUST / "floor.json", installed)
    return result


def remote_revision():
    safe_file(TRUST, directory=True)
    with tempfile.TemporaryDirectory(prefix="cugam-manifest-", dir=TRUST) as temp:
        return available_release(Path(temp)).revision


def install_host():
    if sys.version_info[:2] not in {(3, 11), (3, 12), (3, 13)} or platform.machine() != "x86_64":
        raise DeploymentError("Поддерживаются Linux x86_64 с системным Python 3.11–3.13.")
    for user in ("tgnumclean", "cugamwork", "cugambuild"):
        try:
            pwd.getpwnam(user)
        except KeyError:
            checked(["/usr/sbin/useradd", "--system", "--user-group", "--home-dir", "/nonexistent",
                     "--shell", "/usr/sbin/nologin", user])
    for user in ("cugamwork", "cugambuild"):
        isolated = pwd.getpwnam(user)
        if (isolated.pw_uid == 0 or isolated.pw_shell != "/usr/sbin/nologin"
                or set(os.getgrouplist(user, isolated.pw_gid)) != {isolated.pw_gid}
                or isolated.pw_gid == pwd.getpwnam("tgnumclean").pw_gid):
            raise DeploymentError("Для worker и сборщика нужны отдельные заблокированные пользователи.")
    account = pwd.getpwnam("tgnumclean")
    CONFIG_DIR.mkdir(exist_ok=True)
    CONFIG_DIR.chmod(0o750)
    os.chown(CONFIG_DIR, 0, account.pw_gid)
    token = CONFIG_DIR / "bot.env"
    if not token.exists():
        with os.fdopen(os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640), "wb"):
            os.chown(token, 0, account.pw_gid)


def stage_release(source: Path, candidate: Path, revision: str | None) -> None:
    print("Подготовка файлов релиза.")
    files = (source / "deploy-files.txt").read_text(encoding="utf-8").splitlines()
    if not REQUIRED_FILES.issubset(files):
        raise DeploymentError("В манифесте отсутствуют обязательные файлы установки.")
    for name in files:
        original = source / name
        if ((not RELEASE_FILE.fullmatch(name) and name != "cugam-document@.service") or original.is_symlink() or not original.is_file()
                or not original.resolve().is_relative_to(source.resolve())):
            raise DeploymentError(f"Некорректный файл сборки: {name!r}")
        target = candidate / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(original, target)
        target.chmod(0o755 if name == "cli.py" else 0o644)
    if revision:
        (candidate / "REVISION").write_text(revision + "\n", encoding="ascii")
    candidate.chmod(0o755)
    environment = candidate / ".venv"
    environment.mkdir(mode=0o700)
    account = pwd.getpwnam("cugambuild")
    os.chown(environment, account.pw_uid, account.pw_gid)
    python = environment / "bin/python"
    isolated_build("cugambuild", ["/usr/bin/python3", "-m", "venv", "--copies", environment],
                   writable=environment)
    print("Установка колёс с проверкой SHA-256.")
    isolated_build("cugambuild", [python, "-m", "pip", "--isolated", "install", "--require-hashes",
                   "--only-binary=:all:", "--no-cache-dir", "-r", candidate / "requirements.lock"],
                   writable=environment)
    print("Непривилегированные тесты релиза.")
    isolated_build("cugambuild", [python, "-m", "unittest", "discover", "-s", candidate, "-q"],
                   writable=environment)
    freeze_tree(environment)


def replace_link(path: Path, target: Path) -> None:
    with tempfile.TemporaryDirectory(prefix=".numberbot-", dir=path.parent) as work:
        pending = Path(work) / "link"
        pending.symlink_to(target)
        pending.replace(path)


def write_unit(content: bytes | None, *, path=None) -> None:
    path = UNIT_FILE if path is None else path
    if content is None:
        path.unlink(missing_ok=True)
        return
    with tempfile.TemporaryDirectory(prefix=".numberbot-", dir=path.parent) as work:
        pending = Path(work) / SERVICE
        pending.write_bytes(content)
        pending.chmod(0o644)
        pending.replace(path)


def start_and_check() -> None:
    subprocess.run(["systemctl", "reset-failed", SERVICE], check=False, timeout=10,
                   stderr=subprocess.DEVNULL)
    systemctl("restart", SERVICE)
    if not service_active():
        raise DeploymentError("Служба завершилась до проверки готовности.")


def api_settings():
    path = CONFIG_DIR / "api.env"
    settings = {"CUGAM_HOST": "localhost", "CUGAM_PORT": "8765"}
    if path.is_file():
        for line in path.read_text(encoding="ascii").splitlines():
            key, value = line.split("=", 1)
            if key not in settings:
                raise DeploymentError("В api.env обнаружена неизвестная настройка.")
            settings[key] = value
    host, port = settings["CUGAM_HOST"], settings["CUGAM_PORT"]
    if not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", host) or not port.isdigit() or not 1024 <= int(port) <= 65535:
        raise DeploymentError("Проверь имя хоста и порт API.")
    return host, int(port)


def api_probe():
    host, port = api_settings()
    with_connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        with_connection.request("GET", "/api/v1/health", headers={
            "Host": host, "X-Forwarded-Proto": "https",
        })
        response = with_connection.getresponse()
        return response.status == 200 and response.read(256) == b'{"status":"ok"}'
    except (OSError, http.client.HTTPException):
        return False
    finally:
        with_connection.close()


def start_api_and_check():
    systemctl("restart", API_SERVICE)
    end = time.monotonic() + 14
    while time.monotonic() < end:
        if api_probe():
            return
        time.sleep(0.25)
    raise DeploymentError("API не подтвердил запуск по loopback.")


def check_worker():
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(6)
        connection.connect("/run/cugam-document.sock")
        connection.sendall(b"H" + bytes((0, 0, 0, 1)) + b"1")
        with connection.makefile("rb") as response:
            size = int.from_bytes(response.read(4), "big")
            if not 0 < size <= 64 or json.loads(response.read(size)) != {"status": "ok"}:
                raise DeploymentError("Worker не подтвердил готовность по IPC.")


def activate(candidate: Path, *, start_service: bool, install: bool) -> None:
    worker = {}
    for name in WORKER_FILES:
        source = candidate / name
        if source.is_file():
            target = UNIT_FILE.parent / name
            worker[target] = (target.read_bytes() if target.is_file() else None, source.read_bytes())
    worker_started = False
    worker_enabled = False
    worker_stopped = False
    old_unit = UNIT_FILE.read_bytes() if UNIT_FILE.exists() else None
    new_unit = (candidate / SERVICE).read_bytes()
    api_file = candidate / API_SERVICE
    new_api = api_file.read_bytes() if api_file.is_file() else None
    old_api = API_UNIT.read_bytes() if (new_api is not None and API_UNIT.exists()) else None
    api_was_active = bool(old_api and subprocess.run(
        ["systemctl", "is-active", "--quiet", API_SERVICE], check=False, timeout=10).returncode == 0)
    api_changed = api_started = api_enabled = api_stopped = False
    was_active = service_active()
    old_target = APP_DIR.resolve(strict=True) if APP_DIR.is_symlink() else None
    old_cli_target = CLI_LINK.readlink() if CLI_LINK.is_symlink() else None
    if install and CLI_LINK.exists() and old_cli_target is None:
        raise DeploymentError(f"{CLI_LINK} уже занят другим файлом.")

    previous = RELEASES / "previous"
    obsolete = previous.resolve() if previous.is_symlink() else None
    legacy = None
    switched = False
    unit_changed = False
    cli_changed = False
    enabled = False
    with protect_switch():
        try:
            if old_api:
                systemctl("stop", API_SERVICE)
                api_stopped = True
            if old_unit is not None:
                systemctl("stop", SERVICE)
            if worker and worker[UNIT_FILE.parent / WORKER_SOCKET][0] is not None:
                systemctl("stop", WORKER_SOCKET)
                worker_stopped = True
            if old_target is None and APP_DIR.exists():
                legacy = Path(tempfile.mkdtemp(prefix="legacy-", dir=RELEASES))
                APP_DIR.rename(legacy / "app")
            replace_link(APP_DIR, candidate)
            switched = True
            if new_unit != old_unit:
                write_unit(new_unit)
                unit_changed = True
            if new_api is not None and new_api != old_api:
                write_unit(new_api, path=API_UNIT)
                api_changed = True
            for path, (_, content) in worker.items():
                write_unit(content, path=path)
            if unit_changed or api_changed or worker:
                systemctl("daemon-reload")
            if worker:
                worker_started = True
                systemctl("start", WORKER_SOCKET)
                check_worker()
                if not worker_stopped:
                    worker_enabled = True
                    systemctl("enable", WORKER_SOCKET)
            if new_api is not None and (old_api is None or api_was_active):
                api_started = True
                start_api_and_check()
                if old_api is None:
                    api_enabled = True
                    systemctl("enable", API_SERVICE)
            if start_service:
                start_and_check()
            if install:
                replace_link(CLI_LINK, APP_DIR / "cli.py")
                cli_changed = True
                systemctl("enable", SERVICE)
                enabled = True
            if legacy is not None or old_target is not None:
                replace_link(previous, legacy / "app" if legacy else old_target)
        except (OSError, subprocess.SubprocessError, DeploymentError, ValueError) as error:
            print(f"Сбой переключения версии: {error}", file=sys.stderr)
            try:
                if switched and (start_service or unit_changed):
                    systemctl("stop", SERVICE)
            except (OSError, subprocess.SubprocessError):
                print("systemd не подтвердил остановку новой версии.", file=sys.stderr)
            if api_started:
                try:
                    systemctl("stop", API_SERVICE)
                except (OSError, subprocess.SubprocessError):
                    print("systemd не подтвердил остановку нового API.", file=sys.stderr)
            for action, unit, needed in (
                    ("stop", WORKER_SOCKET, worker_started), ("disable", WORKER_SOCKET, worker_enabled),
                    ("disable", API_SERVICE, api_enabled), ("disable", SERVICE, enabled and old_unit is None)):
                if needed:
                    try:
                        systemctl(action, unit)
                    except (OSError, subprocess.SubprocessError):
                        print("Не удалось выполнить очистку systemd для " + unit, file=sys.stderr)
            if cli_changed:
                if old_cli_target is None:
                    CLI_LINK.unlink(missing_ok=True)
                else:
                    replace_link(CLI_LINK, old_cli_target)
            if legacy is not None and (legacy / "app").exists():
                if switched:
                    APP_DIR.unlink()
                (legacy / "app").rename(APP_DIR)
                legacy.rmdir()
            elif switched:
                if old_target is None:
                    APP_DIR.unlink()
                else:
                    replace_link(APP_DIR, old_target)
            if unit_changed:
                write_unit(old_unit)
            if api_changed:
                write_unit(old_api, path=API_UNIT)
            for path, (previous_content, _) in worker.items():
                write_unit(previous_content, path=path)
            if unit_changed or api_changed or worker:
                systemctl("daemon-reload")
            if worker_stopped:
                systemctl("start", WORKER_SOCKET)
            if was_active:
                start_and_check()
            if api_stopped and api_was_active:
                start_api_and_check()
            if old_target is not None or APP_DIR.exists():
                raise DeploymentError("Восстановлена предыдущая установка. Проверь журнал службы.") from None
            raise DeploymentError("Установка не завершена. Проверь журнал службы.") from None

    if obsolete and obsolete != old_target and obsolete != candidate:
        if obsolete.parent == RELEASES and obsolete.name.startswith("release-"):
            shutil.rmtree(obsolete, ignore_errors=True)


def deploy(work, release, *, start_service, install=False):
    RELEASES.mkdir(mode=0o755, exist_ok=True)
    safe_file(RELEASES, directory=True)
    candidate = Path(tempfile.mkdtemp(prefix="release-", dir=RELEASES))
    try:
        stage_release(work / "source", candidate, release.revision)
        shutil.copyfile(work / MANIFEST, candidate / MANIFEST)
        shutil.copyfile(work / SIGNATURE, candidate / SIGNATURE)
        (candidate / "RELEASE_VERIFIED").write_text(release.version + " " + release.revision + "\n")
        record_floor(release, TRUST / "floor.json")
        print("Переключение проверенного релиза.")
        activate(candidate, start_service=start_service, install=install)
    finally:
        if APP_DIR.resolve() != candidate:
            shutil.rmtree(candidate, ignore_errors=True)


def update(*, configured, expected=None) -> None:
    safe_file(TRUST, directory=True)
    with maintenance_lock(), tempfile.TemporaryDirectory(prefix="cugam-download-", dir=TRUST) as temp:
        work = Path(temp)
        try:
            offered = available_release(work)
            if expected is not None and offered.revision != expected:
                raise DeploymentError("Подписанная ревизия изменилась. Повтори проверку.")
            if offered.revision == revision_at(APP_DIR):
                print("Установлена актуальная подписанная ревизия.")
                return
            print(f"Доступна подписанная версия {offered.version}, Git {offered.revision}.")
            if expected is None and input("Установить? [д/Н]: ").strip().lower() not in {"д", "да", "y", "yes"}:
                return
            download_asset(ASSET, work / ASSET, MAX_ARCHIVE)
            verified = work / "verified"
            verified.mkdir(mode=0o700)
            release = verify_bundle(work / ASSET, work / MANIFEST, work / SIGNATURE, verified, TRUST, extract=True)
            if release != offered:
                raise DeploymentError("Манифест изменился между проверками.")
            install_host()
            deploy(verified, release, start_service=configured())
        except ReleaseError as error:
            raise DeploymentError(str(error)) from None
        print(f"Установлено: {release_label(APP_DIR)}")


def install(archive, manifest, signature):
    safe_file(TRUST, directory=True)
    with tempfile.TemporaryDirectory(prefix="cugam-install-", dir=TRUST) as temp:
        work = Path(temp)
        release = verify_bundle(archive, manifest, signature, work, TRUST, extract=True)
        install_host()
        with maintenance_lock():
            installed = (APP_DIR / "VERSION").read_text().strip() if (APP_DIR / "VERSION").is_file() else None
            check_floor(release, TRUST / "floor.json", installed)
            deploy(work, release, start_service=service_active(), install=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Установка подписанного выпуска")
    parser.add_argument("files", type=Path, nargs=3)
    if os.geteuid() != 0:
        raise SystemExit("Используй доверенный secure_release.py install.")
    try:
        install(*parser.parse_args().files)
    except (ReleaseError, DeploymentError, ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Ошибка установки: {error}") from None
