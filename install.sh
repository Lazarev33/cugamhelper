#!/usr/bin/env bash
# Simple installation for Ubuntu / Debian VPS with systemd.
set -Eeuo pipefail

APP_DIR=/opt/tg-number-cleaner
CONFIG_DIR=/etc/tg-number-cleaner
SERVICE=tg-number-cleaner.service
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

printf '\n[1/5] Проверяю системные зависимости...\n'
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y python3 python3-venv python3-pip ca-certificates
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit('Нужен Python 3.10 или новее')
PY

printf '\n[2/5] Готовлю отдельного пользователя службы...\n'
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/tg-number-cleaner --create-home \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -m 0750 -o root -g "$SERVICE_USER" "$CONFIG_DIR"
if [[ ! -e "$CONFIG_DIR/bot.env" ]]; then
  install -m 0640 -o root -g "$SERVICE_USER" /dev/null "$CONFIG_DIR/bot.env"
fi

printf '\n[3/5] Устанавливаю бота и зависимости...\n'
WAS_RUNNING=0
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
  WAS_RUNNING=1
fi
install -d -m 0755 -o root -g root "$APP_DIR"
for file in bot.py branding.py parser.py cli.py requirements.txt test_parser.py test_cli.py; do
  install -m 0644 -o root -g root "$SOURCE_DIR/$file" "$APP_DIR/$file"
done
chmod 0755 "$APP_DIR/cli.py"
if [[ ! -d "$APP_DIR/.venv" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"

printf '\n[4/5] Создаю сервис systemd...\n'
cat > "/etc/systemd/system/$SERVICE" <<'UNIT'
[Unit]
Description=Помощник 2 смены (Telegram Bot)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=tgnumclean
Group=tgnumclean
WorkingDirectory=/opt/tg-number-cleaner
EnvironmentFile=/etc/tg-number-cleaner/bot.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/tg-number-cleaner/.venv/bin/python /opt/tg-number-cleaner/bot.py
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
RestrictSUIDSGID=true
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
ln -sfn "$APP_DIR/cli.py" /usr/local/bin/numberbot

printf '\n[5/5] Проверяю установку...\n'
"$APP_DIR/.venv/bin/python" -m unittest discover -s "$APP_DIR" -p 'test_*.py' -q
if (( WAS_RUNNING == 1 )); then
  systemctl restart "$SERVICE"
fi
printf '\nГотово! Открыть меню: sudo numberbot\n'
printf '1 — задать токен, 2 — запустить бота.\n'
printf 'Токен хранится в %s/bot.env, не показывается в меню.\n' "$CONFIG_DIR"
