"""Отделённая от владельца VPS подготовка Python-окружения."""

from pathlib import Path
import os
import stat
import subprocess

from secure_release import CLEAN_ENV


def isolated_build(user, args, *, writable=None, timeout=480):
    # Все дочерние процессы принадлежат transient unit, включая процессы тестов.
    unit = [
        "/usr/bin/systemd-run", "--wait", "--pipe", "--collect", "--quiet",
        f"--uid={user}", f"--gid={user}",
        "--property=NoNewPrivileges=yes", "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes", "--property=PrivateDevices=yes",
        "--property=RestrictSUIDSGID=yes", "--property=CapabilityBoundingSet=",
        "--property=RuntimeMaxSec=420", "--property=LimitCORE=0",
        "--property=InaccessiblePaths=-/etc/tg-number-cleaner -/var/lib/tg-number-cleaner -/var/backups/tg-number-cleaner -/run/tg-number-cleaner",
        "--property=TemporaryFileSystem=/tmp:rw,nodev,nosuid,noexec,size=256M,mode=1777",
        "--property=TemporaryFileSystem=/var/tmp:rw,nodev,nosuid,noexec,size=32M,mode=1777",
        "--setenv=HOME=/tmp", "--setenv=TMPDIR=/tmp", "--setenv=PYTHONDONTWRITEBYTECODE=1",
    ]
    if writable:
        unit.append(f"--property=ReadWritePaths={writable}")
    subprocess.run([*unit, "--", *map(str, args)], check=True, timeout=timeout, env=CLEAN_ENV)


def freeze_tree(root):
    for base, dirs, files in os.walk(root, topdown=False, followlinks=False):
        for name in (*files, *dirs):
            path = Path(base) / name
            meta = path.lstat()
            if stat.S_ISLNK(meta.st_mode):
                if path != root / "lib64" or os.readlink(path) != "lib":
                    raise ValueError("Недопустимая ссылка в сборке.")
            elif not (stat.S_ISDIR(meta.st_mode) or stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1):
                raise ValueError("Недопустимый объект в сборке.")
            else:
                path.chmod(0o755 if meta.st_mode & 0o111 or path.is_dir() else 0o644)
            os.chown(path, 0, 0, follow_symlinks=False)
    root.chmod(0o755)
    os.chown(root, 0, 0)
