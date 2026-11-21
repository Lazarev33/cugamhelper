#!/usr/bin/env bash
# Установка на Ubuntu / Debian с systemd.
set -Eeuo pipefail

CONFIG_DIR=/etc/tg-number-cleaner
SERVICE_USER=tgnumclean
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if (( EUID != 0 )); then
  echo 'Ошибка: запусти установку через sudo bash install.sh' >&2
  exit 1
fi
if ! command -v systemctl >/dev/null || [[ "$(ps -p 1 -o comm= | tr -d ' ')" != systemd ]]; then
  echo 'Ошибка: требуется VPS с systemd (например, Ubuntu или Debian).' >&2
  exit 1
fi
if ! command -v apt-get >/dev/null; then
  echo 'Ошибка: этот установщик рассчитан на Ubuntu / Debian (apt).' >&2
  exit 1
fi

printf '\n[1/3] Проверяю системные зависимости...\n'
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y python3 python3-venv python3-pip ca-certificates git openssh-client
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit('Нужен Python 3.10 или новее')
PY

printf '\n[2/3] Готовлю отдельного пользователя службы...\n'
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/tg-number-cleaner --create-home \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -m 0750 -o root -g "$SERVICE_USER" "$CONFIG_DIR"
if [[ ! -e "$CONFIG_DIR/bot.env" ]]; then
  install -m 0640 -o root -g "$SERVICE_USER" /dev/null "$CONFIG_DIR/bot.env"
fi

printf '\n[3/3] Готовлю выпуск и подключаю службу...\n'
python3 "$SOURCE_DIR/deployment.py" "$SOURCE_DIR"

printf '\nГотово! Открыть меню: sudo numberbot\n'
printf '1 — задать токен, 2 — запустить бота.\n'
printf 'Токен хранится в %s/bot.env, не показывается в меню.\n' "$CONFIG_DIR"
