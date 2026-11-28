"""Сроки хранения, пакетная очистка и проверка резервных копий."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from time import time

POLICY = Path("/etc/tg-number-cleaner/retention.json")
DB = Path("/var/lib/tg-number-cleaner/catalog.sqlite3")
BACKUPS = Path("/var/backups/tg-number-cleaner")
BATCH = 250
PROPOSED = {"visitors": 7, "employees_revoked": None, "journal": 90,
            "devices_revoked": None, "backups": None}
RULES = {
    "visitors": ("start_visitors", "user_id", "seen < ?"),
    "employees_revoked": ("trusted_users", "user_id",
                          "active=0 AND CAST(strftime('%s', revoked_at) AS INTEGER) < ?"),
    "journal": ("journal", "id", "CAST(strftime('%s', at) AS INTEGER) < ?"),
    "devices_revoked": ("infra_devices", "id", "revoked < ?"),
}


def settings(path=POLICY):
    if not path.is_file():
        return False, PROPOSED.copy()
    with path.open("rb") as source:
        config = json.loads(source.read(4096))
    if (not isinstance(config, dict) or set(config) != {"enabled", *PROPOSED}
            or type(config["enabled"]) is not bool
            or any(config[key] is not None and (type(config[key]) is not int or not 1 <= config[key] <= 3650)
                   for key in PROPOSED)):
        raise ValueError("Некорректная политика хранения.")
    enabled = config.pop("enabled")
    return enabled, config


def connection(db, mode="ro"):
    return sqlite3.connect(db.as_uri() + "?mode=" + mode, uri=True, timeout=2)


def inventory(folder=BACKUPS, *, verify=False):
    items = []
    if not folder.is_dir():
        return items
    for entry in sorted(folder.iterdir()):
        if entry.is_symlink() or not entry.is_file() or not entry.name.startswith("каталог-") or entry.suffix != ".sqlite3":
            continue
        info = entry.stat()
        row = {"name": entry.name, "bytes": info.st_size, "created": int(info.st_mtime)}
        if verify:
            try:
                with closing(connection(entry)) as db:
                    row.update(schema=db.execute("PRAGMA user_version").fetchone()[0],
                               valid=db.execute("PRAGMA quick_check").fetchone() == ("ok",))
                checksum = sha256()
                with entry.open("rb") as file:
                    for chunk in iter(lambda: file.read(65536), b""):
                        checksum.update(chunk)
                row["sha256"] = checksum.hexdigest()
            except (OSError, sqlite3.Error):
                row["valid"] = False
        items.append(row)
    return sorted(items, key=lambda row: (row["created"], row["name"]), reverse=True)


def rotate(folder, days, now, apply):
    if days is None:
        return 0
    checked = inventory(folder, verify=True)
    newest = next((row["name"] for row in checked if row["valid"]), None)
    if newest is None:
        return 0
    selected = [row["name"] for row in checked
                if row["name"] != newest and row["created"] < now - days * 86400]
    if apply:
        selected = selected[:BATCH]
        for name in selected:
            (folder / name).unlink()
    return len(selected)


def sweep(db_path=DB, policy_path=POLICY, backups=BACKUPS, *, apply=False, now=None):
    enabled, durations = settings(policy_path)
    mutate = apply and enabled
    now = int(time()) if now is None else now
    counts = {}
    with closing(connection(db_path, "rw" if mutate else "ro")) as db:
        db.execute("PRAGMA foreign_keys=ON")
        if mutate:
            db.execute("BEGIN IMMEDIATE")
        available = {entry[0] for entry in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for key, (table, column, condition) in RULES.items():
            days = durations[key]
            if days is None or table not in available:
                counts[key] = 0
                continue
            args = (now - days * 86400,)
            if mutate:
                counts[key] = db.execute(f"DELETE FROM {table} WHERE {column} IN "
                                         f"(SELECT {column} FROM {table} WHERE {condition} LIMIT {BATCH})", args).rowcount
            else:
                counts[key] = db.execute(f"SELECT count(*) FROM {table} WHERE {condition}", args).fetchone()[0]
        for key, table, expiry in (("sessions", "infra_sessions", "renewal"), ("links", "infra_links", "expires")):
            if table in available:
                if mutate:
                    counts[key] = db.execute(f"DELETE FROM {table} WHERE id IN "
                                             f"(SELECT id FROM {table} WHERE {expiry}<=? LIMIT {BATCH})", (now,)).rowcount
                else:
                    counts[key] = db.execute(f"SELECT count(*) FROM {table} WHERE {expiry}<=?", (now,)).fetchone()[0]
        if mutate:
            if any(counts.values()):
                db.execute("INSERT INTO journal(actor,action,subject) VALUES(0,?,?)",
                           ("Очистка по политике", json.dumps(counts)))
                db.execute("DELETE FROM journal WHERE id<=last_insert_rowid()-2000")
            db.commit()
    if backups is not None:
        counts["backups"] = rotate(backups, durations["backups"], now, mutate)
    return {"mode": "очистка" if mutate else "предпросмотр", "enabled": enabled,
            "worker_ttl": "до 600 секунд", "days": durations, "counts": counts}


def prepare_restore(backup, destination):
    """Восстанавливает рабочий каталог и историю с закрытием всех прежних допусков."""
    if backup.is_symlink() or not backup.is_file():
        raise ValueError("Некорректная резервная копия.")
    with closing(connection(backup)) as source, closing(sqlite3.connect(destination)) as target:
        if source.execute("PRAGMA quick_check").fetchone() != ("ok",) or source.execute(
                "PRAGMA user_version").fetchone() != (2,):
            raise ValueError("Повреждённая копия или неподдерживаемая схема.")
        source.backup(target)
        with target:
            existing = {row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"admins", "trusted_users", "barcodes", "journal"} <= existing:
                raise ValueError("В резервной копии отсутствуют обязательные данные.")
            for table in ("admins", "trusted_users", "start_visitors", "infra_sessions", "infra_links",
                          "infra_devices", "infra_revocations", "infra_approvals"):
                if table in existing:
                    target.execute(f"DELETE FROM {table}")
            if "infra_meta" in existing:
                target.execute("DELETE FROM infra_meta WHERE key='recovery'")
            target.execute("INSERT INTO journal(actor,action,subject) VALUES(0,?,?)",
                           ("Восстановлена БД", "Прежние допуски, устройства и сессии сброшены"))
        if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("Проверка восстановленной базы не прошла.")
