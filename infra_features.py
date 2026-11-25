"""Хранение общего набора переключателей, доступное также системному CLI."""

from contextlib import contextmanager
from hashlib import sha256
import json
import sqlite3

from features import parse_features


class FeatureConflict(ValueError):
    pass


def revision(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def feature_database(path):
    if not path.is_file():
        yield None
        return
    db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=1)
    try:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='infra_meta'").fetchone()
        yield db if exists else None
    finally:
        db.close()


def common_features(db, legacy):
    row = db.execute("SELECT value FROM infra_meta WHERE key='common_features'").fetchone()
    return parse_features(json.loads(row[0])) if row else legacy()


def write_common(db, values, previous, legacy, audit=None):
    draft = parse_features(values)
    db.execute("BEGIN IMMEDIATE")
    try:
        old = common_features(db, legacy)
        if revision(old.options()) != previous:
            raise FeatureConflict("Настройки функций изменились. Открой раздел заново.")
        db.execute("""INSERT INTO infra_meta VALUES ('common_features',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (json.dumps(draft.options()),))
        if audit:
            audit()
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return draft
