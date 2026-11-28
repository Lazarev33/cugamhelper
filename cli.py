#!/usr/bin/env python3
"""Terminal menu for managing tg-number-cleaner via systemd on a VPS."""

import argparse
from contextlib import closing
import http.client
import json
import sqlite3
from settings import SETTINGS_PATH, LIMITS, INTEGER_FIELDS, Settings, load_settings
import getpass
import grp
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from branding import BOT_NAME
from admin_guard import approve
from build_release import isolated_build
from secure_release import MANIFEST, SIGNATURE, TRUST, ReleaseError, validate_manifest
import retention
from infra_features import common_features, feature_database, revision, write_common
from features import DEFAULT_FEATURES, FEATURES_PATH, GROUPS, load_features, parse_features
from deployment import (
    APP_DIR, SERVICE, API_SERVICE, api_probe, api_settings, start_api_and_check, check_worker, CONFIG_DIR, DeploymentError, maintenance_lock, service_active, update, start_and_check, remote_revision, release_label,
)

ENV_FILE = Path("/etc/tg-number-cleaner/bot.env")
SERVICE_GROUP = "tgnumclean"
TOKEN_PATTERN = re.compile(r"^[0-9]+:[A-Za-z0-9_-]{20,}$")


def token_is_valid(value: str) -> bool:
    """Check token syntax locally without transmitting it."""
    return TOKEN_PATTERN.fullmatch(value) is not None


def configured() -> bool:
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("BOT_TOKEN="):
                return token_is_valid(line.removeprefix("BOT_TOKEN="))
    except (OSError, UnicodeError):
        pass
    return False


def invoke(command: list[str], *, timeout=None) -> bool:
    """Run trusted commands with argv (no shell interpolation)."""
    try:
        return subprocess.run(command, check=False, timeout=timeout).returncode == 0
    except FileNotFoundError:
        print(f"Команда не найдена: {command[0]}")
        return False


def save_token(token: str, path: Path = ENV_FILE, group: str = SERVICE_GROUP) -> None:
    """Atomically store a token readable only by root and the service group."""
    if not token_is_valid(token):
        raise ValueError("Недопустимый формат токена от BotFather")
    gid = grp.getgrnam(group).gr_gid
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".bot-env-", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o640)
        os.fchown(fd, 0, gid)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"BOT_TOKEN={token}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def logs(follow: bool = False) -> None:
    command = ["journalctl", "-u", SERVICE, "--no-pager"]
    command += ["-f", "-n", "20"] if follow else ["-n", "60"]
    if follow:
        print("\nЖурнал в реальном времени. Ctrl+C — вернуться в меню.\n")
    try:
        return invoke(command)
    except KeyboardInterrupt:
        print("\nВыход из журнала.")


def run_tests() -> None:
    print("\nЗапускаю встроенные проверки без обращения к Telegram...")
    isolated_build("cugambuild", [APP_DIR / ".venv/bin/python", "-m", "unittest", "discover",
                                  "-s", APP_DIR, "-p", "test_*.py", "-v"])


def update_from_github(expected=None) -> bool:
    try:
        update(configured=configured, expected=expected)
    except (EOFError, KeyboardInterrupt):
        print("\nОбновление отменено.")
        return False
    except (ReleaseError, DeploymentError, OSError, subprocess.SubprocessError) as error:
        print(f"Обновление не завершено. {error}", file=sys.stderr)
        return False
    return True



DB = Path("/var/lib/tg-number-cleaner/catalog.sqlite3")
BACKUPS = Path("/var/backups/tg-number-cleaner")
LABELS = ["Размер документа, МиБ","Строк в документе","Одновременных обработок данных","Таймаут обработки, сек.","Срок диалога данных, сек.","Задержка выдачи ШК, сек.", "Задержка очистки, сек.", "Интервал предупреждений, сек.",
          "Интервал отправки в чат, сек.", "Отправок в секунду", "Символов во вводе",
          "Строк во вводе", "Параллельных операций", "Мест в очереди", "Повторов при 429",
          "Таймаут отправки, сек."]


def confirm(prompt):
    return input(prompt + " [д/Н] ").strip().lower() in {"да", "д", "y", "yes"}


def verify_token(token):
    if not token_is_valid(token):
        raise ValueError("Проверь формат токена от @BotFather.")
    conn = http.client.HTTPSConnection("api.telegram.org", timeout=10)
    try:
        conn.request("GET", "/bot" + token + "/getMe")
        response = conn.getresponse()
        if response.status == 401 or response.status == 404:
            raise ValueError("Telegram отклонил токен. Проверь его в @BotFather.")
        if response.status != 200:
            raise ValueError("Telegram временно недоступен. Повтори проверку позже.")
        result = json.loads(response.read(65536))
        if result.get("ok") is not True or not result.get("result", {}).get("is_bot"):
            raise ValueError("Telegram не подтвердил токен.")
    finally:
        conn.close()


def atomic(path, content):
    if content is None:
        path.unlink(missing_ok=True)
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            os.fchown(stream.fileno(), 0, grp.getgrnam(SERVICE_GROUP).gr_gid)
            os.fchmod(stream.fileno(), 0o640)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def apply_settings(changes, token=None, launch=False, *, report=print):
    # Caller holds the shared maintenance lock throughout read, edit and activation.
    previous = {}
    writes = {}
    was_active = service_active()
    if changes or not SETTINGS_PATH.exists():
        load_settings(SETTINGS_PATH)
        values = json.loads(SETTINGS_PATH.read_bytes()) if SETTINGS_PATH.exists() else {}
        values.update(changes)
        if "allowed_user_ids" in changes:
            raise ValueError("Управляй сотрудниками через /admin.")
        content = json.dumps(values, indent=2, ensure_ascii=False).encode() + b"\n"
        with tempfile.NamedTemporaryFile(dir=SETTINGS_PATH.parent) as candidate:
            candidate.write(content)
            candidate.flush()
            load_settings(Path(candidate.name))
        writes[SETTINGS_PATH] = content
    if token is not None:
        if not token_is_valid(token):
            raise ValueError("Недопустимый токен.")
        lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
        lines = [line for line in lines if not line.startswith("BOT_TOKEN=")]
        writes[ENV_FILE] = ("\n".join([*lines, "BOT_TOKEN=" + token]) + "\n").encode()
    try:
        for path, content in writes.items():
            previous[path] = path.read_bytes() if path.exists() else None
            atomic(path, content)
        if launch or was_active:
            start_and_check()
    except BaseException:
        for path, old in previous.items():
            atomic(path, old)
        if was_active:
            start_and_check()
        elif launch:
            invoke(["systemctl", "stop", SERVICE])
        report("Возвращены предыдущие настройки.")
        raise
    report("Настройки сохранены." + (" Служба запущена." if launch or was_active else ""))


def read_feature_policy():
    with feature_database(DB) as conn:
        return common_features(conn, lambda: load_features(FEATURES_PATH)) if conn else load_features(FEATURES_PATH)


def save_features(options, baseline):
    candidate = parse_features(options)
    with maintenance_lock(), feature_database(DB) as conn:
        if conn:
            return write_common(conn, options, revision(baseline.options()), lambda: load_features(FEATURES_PATH))
        if load_features(FEATURES_PATH) != baseline:
            raise DeploymentError("Настройки функций изменились. Открой раздел заново.")
        encoded = json.dumps(candidate.options(), indent=2).encode("utf-8") + b"\n"
        atomic(FEATURES_PATH, encoded)
    return candidate


def feature_menu():
    baseline = read_feature_policy()
    options = baseline.options()
    items = [item for _, pairs in GROUPS for item in pairs]
    while True:
        for index, (key, label) in enumerate(items, 1):
            status = "ВКЛ" if options[key] else "ВЫКЛ"
            print(f"{index:2} · {status} · {label}")
        if not sys.stdin.isatty():
            return
        choice = input("Номер — переключить, с — сохранить, в — включить всё, 0 — назад: ").strip().lower()
        if choice == "0":
            return
        if choice == "в":
            options = DEFAULT_FEATURES.options()
        elif choice == "с" and confirm("Применить доступность функций?"):
            save_features(options, baseline)
            print("Настройки применяются без перезапуска.")
            return
        elif choice in {str(index) for index in range(1, len(items) + 1)}:
            key = items[int(choice) - 1][0]
            options[key] = not options[key]


def uid(text):
    if not text.isascii() or not text.isdigit() or len(text) > 16 or not 0 < int(text) < 2**52:
        raise ValueError("Введи положительный числовой Telegram ID.")
    return int(text)


def setup(mode):
    settings = load_settings(SETTINGS_PATH)
    token = None
    changes = {}
    if mode == "token" or (mode == "setup" and not configured()):
        token = getpass.getpass("Токен от @BotFather (Enter — отмена): ").strip()
        if not token:
            return
        verify_token(token)
    if mode == "owner" or (mode == "setup" and settings.owner_telegram_id is None):
        print("Текущий владелец:", settings.owner_telegram_id or "не назначен")
        raw = input("Telegram ID владельца (число, Enter — оставить без изменений): ").strip()
        if raw:
            owner = uid(raw)
            changes["owner_telegram_id"] = owner
        elif mode == "owner":
            return
    print("Токен:", "проверен" if token else "сохранённый",
          "· Владелец:", changes.get("owner_telegram_id", settings.owner_telegram_id) or "не назначен")
    if confirm("Применить настройки" + (" и запустить бота?" if mode == "setup" else "?")):
        apply_settings(changes, token, launch=mode == "setup")


def database():
    return sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True, timeout=2)


def backup():
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(database()) as source:
        with tempfile.NamedTemporaryFile(prefix="каталог-", suffix=".sqlite3", dir=BACKUPS, delete=False) as f:
            target = Path(f.name)
        try:
            with closing(sqlite3.connect(target)) as dest:
                source.backup(dest)
                if dest.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ValueError("Ошибка проверки копии базы.")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    print("Резервная копия:", target)


def settings_menu():
    while True:
        print("\nНастройки\n1  Telegram-токен\n2  Назначить / изменить владельца\n3  Доступ пользователей"
              "\n4  Отправка ШК\n5  Антиспам и ограничения\n6  Сбросить настройки выдачи и защиты\n0  Назад")
        choice = input("Выбор: ").strip()
        if choice == "0":
            return
        with maintenance_lock():
            current = load_settings(SETTINGS_PATH)
            if choice in {"1", "2"}:
                setup("token" if choice == "1" else "owner")
                continue
            if choice == "3":
                print("Закрытый режим. Управление сотрудниками: /admin → Доверенные пользователи.")
                continue
            elif choice == "4":
                mode = input("1  Фото   2  Оригинальный PNG   0  Назад: ")
                if mode not in {"1", "2"}:
                    continue
                changes = {"barcode_mode": "photo" if mode == "1" else "document"}
            elif choice == "5":
                names = list(LIMITS)
                for i, name in enumerate(names):
                    if i == 3:
                        print("Расширенные настройки")
                    print(f"{i+1:2}  {LABELS[i]}: {getattr(current, name)}")
                index = input("Параметр (0 — назад): ")
                if not index.isascii() or not index.isdigit() or len(index) > 2 or not 1 <= int(index) <= len(names):
                    continue
                name = names[int(index)-1]
                raw = input(f"Новое значение {LIMITS[name]}: ")
                changes = {name: int(raw) if name in INTEGER_FIELDS else float(raw.replace(",", "."))}
            elif choice == "6":
                changes = {k: getattr(Settings(), k) for k in (*LIMITS, "barcode_mode")}
            else:
                continue
            if confirm("Сохранить и применить к работающей службе?"):
                apply_settings(changes)


def dashboard():
    print(f"\n{BOT_NAME}  ·  {release_label(APP_DIR)}")
    print("Служба:", "работает" if service_active() else "остановлена или недоступна",
          " · Токен:", "настроен" if configured() else "не задан")
    try:
        print("Владелец:", load_settings(SETTINGS_PATH).owner_telegram_id or "не назначен")
    except ValueError as e:
        print("Проверь настройки:", e)
    try:
        with closing(database()) as conn:
            print("Закрытый режим · Администраторы:", conn.execute("SELECT count(*) FROM admins").fetchone()[0])
            counts=dict(conn.execute("SELECT active,count(*) FROM trusted_users GROUP BY active"))
            print("Допущено сотрудников:",counts.get(1,0),"· Отключено:",counts.get(0,0))
            print("SQLite доступна · Активных ШК:", conn.execute("SELECT count(*) FROM barcodes WHERE active=1").fetchone()[0])
    except sqlite3.Error:
        print("SQLite недоступна. При первой установке запусти бота для создания каталога.")
    print("GitHub: HTTPS без авторизации")


def diagnostics():
    def libraries():
        isolated_build("cugambuild", [APP_DIR / ".venv/bin/python", "-c",
                      "import aiogram, barcode, PIL, zxingcpp, dotenv, openpyxl, docx, lxml"])
        return True
    def settings_check():
        load_settings()
        read_feature_policy()
        return configured()
    def db_check():
        with closing(database()) as conn:
            return conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
    def telegram():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("BOT_TOKEN="):
                verify_token(line[10:])
                return True
        return False
    checks = [
        ("Настройки и токен (открой мастер)", settings_check),
        ("Служба (посмотри журнал)", service_active),
        ("SQLite (проверь запуск службы)", db_check),
        ("API на loopback (проверь службу cugam-api)", api_probe),
        ("Файлы релиза (переустанови выпуск)", lambda: all((APP_DIR / p).is_file() for p in (APP_DIR / "deploy-files.txt").read_text().splitlines())),
        ("Python и библиотеки (переустанови выпуск)", libraries),
        ("GitHub (проверь HTTPS)", remote_revision),
        ("Telegram getMe (проверь сеть и токен)", telegram),
    ]
    success = True
    for label, check in checks:
        try:
            ok = bool(check())
        except (OSError, ValueError, sqlite3.Error, RuntimeError, http.client.HTTPException):
            ok = False
        print("✓" if ok else "Ошибка:", label)
        success &= ok
    return success


def service(action):
    if action != "stop" and not configured():
        raise ValueError("Сначала настрой токен в мастере.")
    if not invoke(["systemctl", action, SERVICE], timeout=110):
        return False
    return service_active() == (action != "stop")


def api_config(host, port):
    if (host != "localhost" and ("." not in host or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in host.split(".")))) or not 1024 <= port <= 65535:
        raise ValueError("Укажи корректный домен или localhost и порт 1024–65535.")
    path = CONFIG_DIR / "api.env"
    with maintenance_lock():
        before = path.read_bytes() if path.exists() else None
        try:
            atomic(path, f"CUGAM_HOST={host}\nCUGAM_PORT={port}\n".encode("ascii"))
            start_api_and_check()
        except (OSError, subprocess.SubprocessError, DeploymentError):
            atomic(path, before)
            start_api_and_check()
            raise
    print("API запущен. Конфигурация сохранена.")


def infra_action(action):
    if action == "api-status":
        host, port = api_settings()
        ready = api_probe()
        print("API доступен" if ready else "API не ответил", f"· {host}, внутренний порт {port}")
        return ready
    if action == "proxy":
        host, port = api_settings()
        if host == "localhost":
            raise ValueError("Сначала задай внешний домен через sudo numberbot api-config.")
        print(f"""{host} {{
    request_body {{
        max_size 524288
    }}
    header Strict-Transport-Security "max-age=31536000"
    reverse_proxy 127.0.0.1:{port}
}}""")
        return True
    if action == "web-access":
        return invoke(["/usr/sbin/runuser", "--pty", "-u", "tgnumclean", "--",
                       str(APP_DIR / ".venv/bin/python"), "-Es", str(APP_DIR / "infra_admin.py")], timeout=10)
    with maintenance_lock():
        if action == "api-start":
            start_api_and_check()
            print("API отвечает.")
            return True
        return invoke(["systemctl", "stop", API_SERVICE], timeout=20)


def worker_status():
    check_worker()
    print("Worker ответил через IPC. Проверки полномочий см. в отчёте испытаний.")


def storage_action(action):
    if action == "trust-status":
        release = validate_manifest(APP_DIR / MANIFEST, APP_DIR / SIGNATURE, TRUST)
        if release.revision != (APP_DIR / "REVISION").read_text().strip():
            raise ValueError("Квитанция относится к другой ревизии.")
        print(f"Подпись манифеста действительна: {release.version}, Git {release.revision}, ключ {release.key}.")
        return
    if action == "worker-status":
        return worker_status()
    if action == "backups":
        print(json.dumps(retention.inventory(BACKUPS, verify=True), indent=2, ensure_ascii=False))
        return
    with maintenance_lock():
        result = retention.sweep(DB, CONFIG_DIR / "retention.json", BACKUPS, apply=action == "retention-apply")
        print(json.dumps(result, indent=2, ensure_ascii=False))


def restore_database(name):
    from deployment import systemctl
    import pwd
    if Path(name).name != name or not name.startswith("каталог-") or not name.endswith(".sqlite3"):
        raise ValueError("Укажи имя копии из sudo numberbot backups.")
    if not sys.stdin.isatty() or input("Будут сброшены допуски и сессии. Для восстановления введи RESTORE: ") != "RESTORE":
        print("Восстановление отменено.")
        return
    with maintenance_lock(), tempfile.TemporaryDirectory(dir=DB.parent) as folder:
        pending = Path(folder) / "restored.db"
        retention.prepare_restore(BACKUPS / name, pending)
        bot_running = service_active()
        api_running = api_probe()
        saved = Path(folder) / "previous.db"
        swapped = False
        try:
            systemctl("stop", SERVICE, API_SERVICE)
            if any(Path(str(DB) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise ValueError("Закрой открытые транзакции SQLite перед восстановлением.")
            account = pwd.getpwnam("tgnumclean")
            os.chown(pending, account.pw_uid, account.pw_gid)
            pending.chmod(0o600)
            if DB.exists():
                os.link(DB, saved)
            pending.replace(DB)
            swapped = True
            if api_running:
                start_api_and_check()
            if bot_running:
                start_and_check()
        except BaseException:
            if swapped:
                systemctl("stop", SERVICE, API_SERVICE)
                if saved.exists():
                    saved.replace(DB)
                else:
                    DB.unlink(missing_ok=True)
            if bot_running:
                start_and_check()
            if api_running:
                start_api_and_check()
            raise
    print("Копия восстановлена. Владелец может заново выдать допуски.")


def dispatch(action):
    if action in {"trust-status", "worker-status", "backups", "retention-preview", "retention-apply"}:
        return storage_action(action)
    if action in {"api-status", "api-start", "api-stop", "web-access", "proxy"}:
        return infra_action(action)
    if action in {"start", "restart", "stop", "setup", "owner", "token", "backup"}:
        with maintenance_lock():
            if action == "backup":
                backup()
            elif action in {"setup", "owner", "token"}:
                setup(action)
            else:
                ok = service(action)
                print("Выполнено." if ok else "Служба не подтвердила действие. Проверь журнал.")
                return ok
    else:
        return {"update": update_from_github, "settings": settings_menu, "features": feature_menu, "logs": logs,
                "follow": lambda: logs(True), "test": run_tests, "diagnose": diagnostics,
                "status": lambda: invoke(["systemctl", "status", SERVICE, "--no-pager"])}[action]()


def run(action):
    try:
        return dispatch(action) is not False
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error, http.client.HTTPException,
            subprocess.SubprocessError) as error:
        print("Не удалось выполнить действие:", error)
        return False


def menu():
    if not configured() and confirm("Начать первоначальную настройку?"):
        run("setup")
    actions = ["setup", "start", "stop", "restart", "settings", "logs", "backup", "diagnose", "update", "features"]
    while True:
        dashboard()
        print("\nУправление   1 Мастер настройки   2 Запустить   3 Остановить   4 Перезапустить"
              "\nНастройки    5 Токен, владелец, доступ и ограничения"
              "\nОбслуживание 6 Журнал   7 Резервная копия   8 Диагностика\n             9 Обновить из GitHub   10 Функциональность   0 Выход")
        choice = input("Выбор: ").strip()
        if choice == "0":
            return
        if choice in [str(index) for index in range(1, 11)]:
            run(actions[int(choice)-1])


def main():
    parser = argparse.ArgumentParser(description=BOT_NAME)
    parser.add_argument("action", nargs="?", default="menu", choices="menu setup owner settings features token start stop restart status logs follow test diagnose backup update api-status api-start api-stop api-config web-access proxy trust-status worker-status backups retention-preview retention-apply retention-policy restore approve".split())
    parser.add_argument("--expected-revision", help=argparse.SUPPRESS)
    parser.add_argument("extra", nargs="*")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    action = args.action
    if os.geteuid() != 0:
        print("Используй sudo numberbot.")
        return 1
    try:
        if action == "menu":
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                print("Используй интерактивный терминал или прямую команду sudo numberbot --help.")
                return 1
            try:
                from tui import launch
                launch()
            except (ImportError, RuntimeError) as error:
                print(f"Панель недоступна в этом терминале. Открываю текстовое меню. {error}")
                menu()
            return 0
        if action in {"approve", "retention-policy", "restore"}:
            try:
                if action == "approve" and len(args.extra) == 2:
                    owner = load_settings(SETTINGS_PATH).owner_telegram_id
                    if owner is None:
                        raise ValueError("Владелец не настроен.")
                    with maintenance_lock():
                        approve(DB, owner, *args.extra)
                    print("Действие подтверждено на три минуты.")
                elif action == "restore" and len(args.extra) == 1:
                    restore_database(args.extra[0])
                elif action == "retention-policy" and len(args.extra) == 1:
                    path = Path(args.extra[0])
                    enabled, durations = retention.settings(path)
                    if not path.is_file() or not sys.stdin.isatty() or not confirm(
                            "Применить сроки " + json.dumps(durations) + f", очистка {'включена' if enabled else 'выключена'}?"):
                        return 1
                    with maintenance_lock():
                        atomic(CONFIG_DIR / "retention.json", json.dumps({"enabled": enabled, **durations}).encode())
                        if enabled:
                            invoke(["systemctl", "enable", "--now", "cugam-retention.timer"], timeout=30)
                        else:
                            invoke(["systemctl", "disable", "--now", "cugam-retention.timer"], timeout=30)
                else:
                    parser.error("Проверь аргументы команды.")
                return 0
            except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
                print("Операция отклонена.", error, file=sys.stderr)
                return 1
        if action == "api-config":
            try:
                old_host, old_port = api_settings()
                host = args.host or (input(f"Домен [{old_host}]: ").strip() or old_host if sys.stdin.isatty() else old_host)
                port = args.port if args.port is not None else (int(input(f"Порт [{old_port}]: ").strip() or old_port)
                                                               if sys.stdin.isatty() else old_port)
                api_config(host.lower(), port)
                return 0
            except (OSError, ValueError, subprocess.SubprocessError, DeploymentError) as error:
                print("Настройка API не применена.", error, file=sys.stderr)
                return 1
        if args.expected_revision is not None:
            if action != "update" or not re.fullmatch(r"[0-9a-f]{40}", args.expected_revision):
                parser.error("Некорректная ревизия обновления.")
            return 0 if update_from_github(args.expected_revision) else 1
        return 0 if run(action) else 1
    except (EOFError, KeyboardInterrupt):
        print("\nОтменено.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
