#!/usr/bin/env python3
"""Terminal menu for managing tg-number-cleaner via systemd on a VPS."""

import argparse
import getpass
import grp
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from branding import BOT_NAME

SERVICE = "tg-number-cleaner.service"
APP_DIR = Path("/opt/tg-number-cleaner")
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


def service_active() -> bool:
    return subprocess.run(
        ["systemctl", "is-active", "--quiet", SERVICE],
        check=False,
    ).returncode == 0


def invoke(command: list[str]) -> bool:
    """Run trusted commands with argv (no shell interpolation)."""
    try:
        return subprocess.run(command, check=False).returncode == 0
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


def edit_token() -> None:
    print("\nТокен можно получить у @BotFather. Символы ввода скрыты.")
    try:
        value = getpass.getpass("Новый BOT_TOKEN (Enter — отмена): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nОтменено.")
        return
    if not value:
        print("Отменено.")
        return
    try:
        save_token(value)
    except (OSError, ValueError, KeyError) as error:
        print(f"Не удалось сохранить токен: {error}")
        return
    print("Токен сохранён, значение не выводится.")
    if service_active():
        print("Применяю новый токен перезапуском службы...")
        invoke(["systemctl", "restart", SERVICE])
    else:
        print("Для запуска выбери пункт 2.")


def display_status(full: bool = False) -> None:
    active = service_active()
    print(f"\nСлужба: {'РАБОТАЕТ' if active else 'ОСТАНОВЛЕНА'}")
    print(f"Токен: {'настроен' if configured() else 'не задан / неверный формат'}")
    if full:
        invoke(["systemctl", "status", SERVICE, "--no-pager", "--full"])


def start() -> None:
    if not configured():
        print("Сначала задай токен через пункт 1.")
        return
    if invoke(["systemctl", "start", SERVICE]):
        print("Команда запуска выполнена.")
        display_status()
    else:
        print("Ошибка запуска. Проверь журнал через пункт 6.")


def stop() -> None:
    if invoke(["systemctl", "stop", SERVICE]):
        print("Бот остановлен.")


def restart() -> None:
    if not configured():
        print("Сначала задай токен через пункт 1.")
        return
    if invoke(["systemctl", "restart", SERVICE]):
        print("Команда перезапуска выполнена.")
        display_status()
    else:
        print("Не удалось перезапустить. Проверь журнал.")


def logs(follow: bool = False) -> None:
    command = ["journalctl", "-u", SERVICE, "--no-pager"]
    command += ["-f", "-n", "20"] if follow else ["-n", "60"]
    if follow:
        print("\nЖурнал в реальном времени. Ctrl+C — вернуться в меню.\n")
    try:
        invoke(command)
    except KeyboardInterrupt:
        print("\nВыход из журнала.")


def run_tests() -> None:
    print("\nЗапускаю встроенные проверки без обращения к Telegram...")
    invoke([
        sys.executable, "-m", "unittest", "discover", "-s", str(APP_DIR),
        "-p", "test_*.py", "-v",
    ])


def menu() -> None:
    actions = {
        "1": edit_token,
        "2": start,
        "3": stop,
        "4": restart,
        "5": lambda: display_status(full=True),
        "6": logs,
        "7": lambda: logs(follow=True),
        "8": run_tests,
    }
    while True:
        print("\n" + "═" * 45)
        print(f"       {BOT_NAME} · VPS CLI")
        print("═" * 45)
        print(f"  Бот:   {'● работает' if service_active() else '○ остановлен'}")
        print(f"  Токен: {'настроен' if configured() else 'не настроен'}")
        print("─" * 45)
        print("  1. Установить / заменить токен")
        print("  2. Запустить бота")
        print("  3. Остановить бота")
        print("  4. Перезапустить бота")
        print("  5. Подробный статус")
        print("  6. Последние 60 строк журнала")
        print("  7. Журнал в реальном времени")
        print("  8. Проверка кода (тесты)")
        print("  0. Выход")
        try:
            choice = input("\nВыбор: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nВыход.")
            return
        if choice in ("0", "q", "Q"):
            return
        action = actions.get(choice)
        if action is None:
            print("Неизвестный пункт меню.")
            continue
        action()
        try:
            input("\nEnter — продолжить...")
        except (EOFError, KeyboardInterrupt):
            print()
            return


def main() -> int:
    parser = argparse.ArgumentParser(description="Управление Telegram-ботом на VPS")
    parser.add_argument(
        "action", nargs="?", default="menu",
        choices=["menu", "token", "start", "stop", "restart", "status", "logs", "follow", "test"],
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        print("Нужны права администратора. Используй: sudo numberbot")
        return 1
    if not APP_DIR.joinpath("bot.py").exists():
        print("Бот не установлен. Запусти: sudo bash install.sh")
        return 1
    actions = {
        "menu": menu,
        "token": edit_token,
        "start": start,
        "stop": stop,
        "restart": restart,
        "status": lambda: display_status(full=True),
        "logs": logs,
        "follow": lambda: logs(follow=True),
        "test": run_tests,
    }
    actions[args.action]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
