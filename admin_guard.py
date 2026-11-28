"""Одноразовое VPS-подтверждение конкретного критического действия."""

from contextlib import closing
from hashlib import sha256
import json
import re
import sqlite3
from time import time

SCHEMA = """CREATE TABLE IF NOT EXISTS infra_approvals (
    actor INTEGER NOT NULL, operation TEXT NOT NULL, fingerprint TEXT NOT NULL,
    expires INTEGER NOT NULL, PRIMARY KEY(actor,operation,fingerprint))"""


def fingerprint(value):
    normalized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(normalized.encode()).hexdigest()


def approve(db, actor, operation, target):
    if not re.fullmatch(r"role|features|devices", operation) or not re.fullmatch(r"[a-f0-9]{64}", target):
        raise ValueError("Некорректное действие или отпечаток.")
    with closing(sqlite3.connect(db, timeout=2)) as connection, connection:
        connection.execute(SCHEMA)
        connection.execute("DELETE FROM infra_approvals WHERE expires<=?", (int(time()),))
        if connection.execute("SELECT count(*) FROM infra_approvals").fetchone()[0] >= 64:
            raise ValueError("Слишком много ожидающих подтверждений.")
        connection.execute("INSERT OR REPLACE INTO infra_approvals VALUES (?,?,?,?)",
                           (actor, operation, target, int(time()) + 180))


def consume(db, actor, operation, target):
    with db:
        db.execute(SCHEMA)
        # DELETE — проверка и списание в одной SQL-операции.
        return bool(db.execute("DELETE FROM infra_approvals WHERE actor=? AND operation=? "
                               "AND fingerprint=? AND expires>?",
                               (actor, operation, target, int(time()))).rowcount)
