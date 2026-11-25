"""Одноразовый административный код, доступный с локальной консоли."""

import os
import sys

from infrastructure import Infrastructure, InfraError


def main():
    if os.geteuid() != 0 or not sys.stdout.isatty():
        print("Для получения кода открой интерактивный терминал VPS.")
        return 1
    try:
        print(Infrastructure().recovery_issue())
    except InfraError:
        print("Сначала назначь владельца через sudo numberbot owner.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
