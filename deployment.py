"""Установка и ручное обновление бота с откатом к рабочей версии."""

import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile


APP_DIR = Path("/opt/tg-number-cleaner")
RELEASES = Path("/opt/tg-number-cleaner-releases")
CONFIG_DIR = Path("/etc/tg-number-cleaner")
SERVICE = "tg-number-cleaner.service"
UNIT_FILE = Path("/etc/systemd/system") / SERVICE
CLI_LINK = Path("/usr/local/bin/numberbot")
LOCK_FILE = CONFIG_DIR / "maintenance.lock"
REMOTE = "https://github.com/Lazarev33/cugamhelper.git"
BRANCH = "refs/heads/main"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
RELEASE_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
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


def git_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ALLOW_PROTOCOL": "https",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.followRedirects",
        "GIT_CONFIG_VALUE_0": "false",
        "GIT_TERMINAL_PROMPT": "0",
    }


def remote_revision() -> str:
    env = git_environment()
    try:
        head = checked(["git", "ls-remote", "--exit-code", REMOTE, BRANCH], env=env, output=True)
    except (subprocess.SubprocessError, OSError):
        raise DeploymentError(
            "GitHub недоступен. Проверь DNS, системное время, сертификаты и HTTPS-соединение VPS."
        ) from None
    revision = head.split("\t", 1)[0]
    if not SHA_PATTERN.fullmatch(revision):
        raise DeploymentError("GitHub вернул неизвестный формат ревизии.")
    return revision


def fetch_source(work: Path, revision: str) -> Path:
    env = git_environment()
    repo = work / "repo.git"
    checked(["git", "init", "--bare", "--quiet", "--template=", repo], env=env)
    git = ["git", "--git-dir", repo]
    checked([*git, "fetch", "--quiet", "--depth=1", "--no-tags", REMOTE, BRANCH], env=env)
    fetched = checked([*git, "rev-parse", "FETCH_HEAD"], env=env, output=True)
    if fetched != revision:
        raise DeploymentError("Ветка main изменилась после проверки. Запусти обновление ещё раз.")
    archive = work / "source.tar"
    checked([*git, "archive", "--output", archive, revision], env=env)
    source = work / "source"
    source.mkdir()
    checked(["tar", "--extract", "--file", archive, "--directory", source,
             "--no-same-owner", "--no-same-permissions"])
    return source


def stage_release(source: Path, candidate: Path, revision: str | None) -> None:
    files = (source / "deploy-files.txt").read_text(encoding="utf-8").splitlines()
    if not REQUIRED_FILES.issubset(files):
        raise DeploymentError("В манифесте отсутствуют обязательные файлы установки.")
    for name in files:
        original = source / name
        if not RELEASE_FILE.fullmatch(name) or original.is_symlink() or not original.is_file():
            raise DeploymentError(f"Некорректный файл сборки: {name!r}")
        shutil.copyfile(original, candidate / name)
        (candidate / name).chmod(0o755 if name == "cli.py" else 0o644)
    if revision:
        (candidate / "REVISION").write_text(revision + "\n", encoding="ascii")
    checked([sys.executable, "-m", "venv", candidate / ".venv"], timeout=180)
    python = candidate / ".venv/bin/python"
    checked([python, "-m", "pip", "--isolated", "install", "--disable-pip-version-check",
             "-r", candidate / "requirements.txt"], timeout=600)
    checked([python, "-m", "unittest", "discover", "-s", candidate, "-p", "test_*.py", "-q"])
    candidate.chmod(0o755)


def replace_link(path: Path, target: Path) -> None:
    with tempfile.TemporaryDirectory(prefix=".numberbot-", dir=path.parent) as work:
        pending = Path(work) / "link"
        pending.symlink_to(target)
        pending.replace(path)


def write_unit(content: bytes | None) -> None:
    if content is None:
        UNIT_FILE.unlink(missing_ok=True)
        return
    with tempfile.TemporaryDirectory(prefix=".numberbot-", dir=UNIT_FILE.parent) as work:
        pending = Path(work) / SERVICE
        pending.write_bytes(content)
        pending.chmod(0o644)
        pending.replace(UNIT_FILE)


def start_and_check() -> None:
    subprocess.run(["systemctl", "reset-failed", SERVICE], check=False, timeout=10,
                   stderr=subprocess.DEVNULL)
    systemctl("restart", SERVICE)
    if not service_active():
        raise DeploymentError("Служба завершилась до проверки готовности.")


def activate(candidate: Path, *, start_service: bool, install: bool) -> None:
    old_unit = UNIT_FILE.read_bytes() if UNIT_FILE.exists() else None
    new_unit = (candidate / SERVICE).read_bytes()
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
            if old_unit is not None:
                systemctl("stop", SERVICE)
            if old_target is None and APP_DIR.exists():
                legacy = Path(tempfile.mkdtemp(prefix="legacy-", dir=RELEASES))
                APP_DIR.rename(legacy / "app")
            replace_link(APP_DIR, candidate)
            switched = True
            if new_unit != old_unit:
                write_unit(new_unit)
                unit_changed = True
                systemctl("daemon-reload")
            if start_service:
                start_and_check()
            if install:
                replace_link(CLI_LINK, APP_DIR / "cli.py")
                cli_changed = True
                systemctl("enable", SERVICE)
                enabled = True
            if legacy is not None or old_target is not None:
                replace_link(previous, legacy / "app" if legacy else old_target)
        except (OSError, subprocess.SubprocessError, DeploymentError) as error:
            print(f"Сбой переключения версии: {error}", file=sys.stderr)
            try:
                if switched and (start_service or unit_changed):
                    systemctl("stop", SERVICE)
            except (OSError, subprocess.SubprocessError):
                print("systemd не подтвердил остановку новой версии.", file=sys.stderr)
            if enabled and old_unit is None:
                systemctl("disable", SERVICE)
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
                systemctl("daemon-reload")
            if was_active:
                start_and_check()
            if old_target is not None or APP_DIR.exists():
                raise DeploymentError("Восстановлена предыдущая установка. Проверь журнал службы.") from None
            raise DeploymentError("Установка не завершена. Проверь журнал службы.") from None

    if obsolete and obsolete != old_target and obsolete != candidate:
        if obsolete.parent == RELEASES and obsolete.name.startswith("release-"):
            shutil.rmtree(obsolete, ignore_errors=True)


def deploy(source: Path, revision: str | None, *, start_service: bool, install=False) -> None:
    RELEASES.mkdir(mode=0o755, exist_ok=True)
    candidate = Path(tempfile.mkdtemp(prefix="release-", dir=RELEASES))
    try:
        stage_release(source, candidate, revision)
        activate(candidate, start_service=start_service, install=install)
    finally:
        if APP_DIR.resolve() != candidate:
            shutil.rmtree(candidate, ignore_errors=True)


def update(*, configured) -> None:
    with maintenance_lock():
        print(f"Установлено: {release_label(APP_DIR)}")
        revision = remote_revision()
        if revision == revision_at(APP_DIR):
            print("Установлена актуальная ревизия. Служба продолжает работать в прежнем режиме.")
            return
        print(f"Доступна ревизия main: {revision[:12]}")
        if input("Установить обновление? [д/Н]: ").strip().lower() not in {"д", "да", "y", "yes"}:
            print("Отменено.")
            return
        with tempfile.TemporaryDirectory(prefix="numberbot-") as temp:
            source = fetch_source(Path(temp), revision)
            start_service = configured()
            deploy(source, revision, start_service=start_service)
        print(f"Установлено: {release_label(APP_DIR)}")
        if start_service:
            print("Служба запущена, Telegram подтвердил начало опроса.")
        else:
            print("Задай токен и запусти службу через sudo numberbot.")


def install(source: Path) -> None:
    revision = revision_at(source)
    if revision is None and (source / ".git").exists():
        revision = checked(
            ["git", "-c", f"safe.directory={source}", "-C", source, "rev-parse", "HEAD"],
            output=True,
        )
    with maintenance_lock():
        deploy(source, revision, start_service=service_active(), install=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Установка «Помощника 2 смены»")
    parser.add_argument("source", type=Path)
    if os.geteuid() != 0:
        raise SystemExit("Запусти sudo bash install.sh.")
    try:
        install(parser.parse_args().source)
    except (DeploymentError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Ошибка установки: {error}") from None
