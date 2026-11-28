#!/bin/bash
set -euo pipefail
test "${GITHUB_ACTIONS:-}" = true
test "${RUNNER_ENVIRONMENT:-}" = github-hosted
test "$(ps -p 1 -o comm=)" = systemd
for path in /opt/tg-number-cleaner /etc/tg-number-cleaner /var/lib/tg-number-cleaner /var/backups/tg-number-cleaner /run/tg-number-cleaner; do
    test ! -e "$path"
done
for user in tgnumclean cugamwork cugambuild; do
    sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin "$user"
done
sudo install -d -m 0755 /opt/tg-number-cleaner /opt/tg-number-cleaner/.venv/bin
sudo ln -s "$(command -v python)" /opt/tg-number-cleaner/.venv/bin/python
sudo install -m 0644 tooling/isolation_probe.py /opt/tg-number-cleaner/isolated_worker.py
for spec in "/etc/tg-number-cleaner bot.env" "/var/lib/tg-number-cleaner catalog.sqlite3" "/var/backups/tg-number-cleaner synthetic" "/run/tg-number-cleaner admin-session"; do
    read -r dir file <<< "$spec"
    sudo install -d -m 0755 "$dir"
    printf '%s\n' 'SYNTHETIC-DENIAL-PROBE' | sudo tee "$dir/$file" >/dev/null
    sudo chmod 0644 "$dir/$file"
done
sudo install -m 0644 cugam-document.socket cugam-document@.service /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/cugam-document@.service.d
printf '[Service]\nStandardError=journal\n' | sudo tee /etc/systemd/system/cugam-document@.service.d/diagnostic.conf >/dev/null
trap 'sudo journalctl --no-pager -n 35 -g "cugam-document|OS_ISOLATION|Traceback" || true' ERR
sudo systemctl daemon-reload
sudo systemctl start cugam-document.socket
sudo -u tgnumclean python - <<'PY'
import socket
with socket.socket(socket.AF_UNIX) as connection:
    connection.settimeout(10)
    connection.connect("/run/cugam-document.sock")
    with connection.makefile("rb") as reader:
        assert reader.readline().strip() == b"OS_ISOLATION_OK"
PY
sudo systemctl stop cugam-document.socket
sudo install -m 0644 isolated_worker.py warehouse_worker.py warehouse_files.py warehouse.py private_process.py features.py /opt/tg-number-cleaner/
sudo systemctl start cugam-document.socket
sudo -u tgnumclean python - <<'PY'
import base64
import json
import socket
import struct

payload = json.dumps({"items":[{"kind":"text","opts":{},"data":base64.b64encode(b"009\n001").decode()}],
                      "mode":"asc","limit":10,"expected":None}).encode()
with socket.socket(socket.AF_UNIX) as connection:
    connection.settimeout(10)
    connection.connect("/run/cugam-document.sock")
    connection.sendall(b"D"+struct.pack(">I", len(payload))+payload)
    with connection.makefile("rb") as result:
        size = struct.unpack(">I", result.read(4))[0]
        message = json.loads(result.read(size))
        assert message["outputs"][0]["data"] == [["001", "009"]], message
print("WORKER_SOCKET_IPC_OK")
PY
sudo systemctl stop cugam-document.socket
sudo systemd-run --wait --pipe --collect --quiet --uid=cugambuild --gid=cugambuild \
    --property=ProtectSystem=strict --property=NoNewPrivileges=yes \
    --property=InaccessiblePaths='/etc/tg-number-cleaner /var/lib/tg-number-cleaner /var/backups/tg-number-cleaner /run/tg-number-cleaner' \
    /bin/sh -ec 'test "$(id -u)" -ne 0 && test ! -r /etc/tg-number-cleaner/bot.env && test ! -r /var/lib/tg-number-cleaner/catalog.sqlite3 && printf "BUILD_USER_ISOLATION_OK\n"'
