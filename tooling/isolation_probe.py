"""Наблюдаемые отказы ОС на одноразовом GitHub runner. Только синтетические файлы."""
from pathlib import Path
import mmap
import os
import resource
import socket

assert os.geteuid() != 0, "Неверный UID worker"
for where in (
        "/etc/tg-number-cleaner/bot.env", "/var/lib/tg-number-cleaner/catalog.sqlite3",
        "/var/backups/tg-number-cleaner/synthetic", "/run/tg-number-cleaner/admin-session"):
    try:
        Path(where).read_bytes()
    except PermissionError:
        continue
    raise AssertionError("Файл доступен worker: " + where)
try:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM)
except PermissionError:
    pass
else:
    raise AssertionError("Worker может открыть сетевой сокет")
for where in ("/opt/tg-number-cleaner/forbidden", "/var/lib/payload-synthetic"):
    try:
        Path(where).write_text("SYNTHETIC")
    except OSError as exc:
        assert exc.errno in {1, 13, 30}
    else:
        raise AssertionError("Worker может писать в постоянный каталог")
try:
    os.setuid(0)
except PermissionError:
    pass
else:
    raise AssertionError("Worker повысил UID")
assert resource.getrlimit(resource.RLIMIT_AS)[0] <= 576 * 1024**2
try:
    mmap.mmap(-1, 768 * 1024**2)
except OSError as exc:
    assert exc.errno == 12
else:
    raise AssertionError("RLIMIT_AS не ограничивает адресное пространство")
with Path("/tmp/worker-synthetic").open("w") as f:
    f.write("SYNTHETIC")
mounts = Path("/proc/self/mountinfo").read_text().splitlines()
assert any(" /tmp " in row and " - tmpfs " in row for row in mounts), "Рабочий /tmp не tmpfs"
print("OS_ISOLATION_OK", flush=True)
