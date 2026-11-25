"""Общие данные инфраструктуры. В SQLite хранятся только учётные метаданные."""

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path
from secrets import token_urlsafe
from time import time
import json
import sqlite3

from catalog import Catalog, CatalogDenied
from settings import load_settings
from infra_features import common_features, revision

ACCESS_TTL = 600
REFRESH_TTL = 30 * 86400
WEB_TTL = 8 * 3600
LINK_TTL = 300
MAX_DEVICES = 12
DATABASE = Path("/var/lib/tg-number-cleaner/catalog.sqlite3")
SOCKET = Path("/run/tg-number-cleaner/bridge.sock")

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS infra_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS infra_revocations (seq INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS infra_links (
      id TEXT PRIMARY KEY, proof TEXT NOT NULL, uid INTEGER NOT NULL, scope TEXT NOT NULL,
      name TEXT NOT NULL, app_version TEXT NOT NULL, expires INTEGER NOT NULL, decision INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS infra_devices (
      id TEXT PRIMARY KEY, uid INTEGER NOT NULL, name TEXT NOT NULL, app_version TEXT NOT NULL,
      created INTEGER NOT NULL, seen INTEGER NOT NULL, revoked INTEGER
    )""",
    """CREATE INDEX IF NOT EXISTS infra_device_users ON infra_devices(uid, revoked)""",
    """CREATE TABLE IF NOT EXISTS infra_sessions (
      id TEXT PRIMARY KEY, uid INTEGER NOT NULL, scope TEXT NOT NULL, device TEXT UNIQUE
      REFERENCES infra_devices(id) ON DELETE CASCADE, access TEXT NOT NULL UNIQUE, refresh TEXT UNIQUE,
      expiry INTEGER NOT NULL, renewal INTEGER NOT NULL, csrf TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE INDEX IF NOT EXISTS infra_session_users ON infra_sessions(uid, scope)""",
    """CREATE TRIGGER IF NOT EXISTS infra_revoke_employee AFTER UPDATE OF active ON trusted_users
    WHEN OLD.active=1 AND NEW.active=0 BEGIN
      INSERT INTO infra_revocations(uid) VALUES (NEW.user_id);
      DELETE FROM infra_revocations WHERE seq<=last_insert_rowid()-4096;
      UPDATE infra_devices SET revoked=CAST(strftime('%s','now') AS INTEGER) WHERE uid=NEW.user_id AND revoked IS NULL;
      DELETE FROM infra_sessions WHERE uid=NEW.user_id;
      DELETE FROM infra_links WHERE uid=NEW.user_id;
    END""",
    """CREATE TRIGGER IF NOT EXISTS infra_revoke_admin AFTER DELETE ON admins BEGIN
      INSERT INTO infra_revocations(uid) VALUES (OLD.user_id);
      DELETE FROM infra_revocations WHERE seq<=last_insert_rowid()-4096;
      DELETE FROM infra_sessions WHERE uid=OLD.user_id AND
      (scope='web' OR NOT EXISTS (SELECT 1 FROM trusted_users WHERE user_id=OLD.user_id AND active=1));
      UPDATE infra_devices SET revoked=CAST(strftime('%s','now') AS INTEGER) WHERE uid=OLD.user_id AND revoked IS NULL AND
      NOT EXISTS (SELECT 1 FROM trusted_users WHERE user_id=OLD.user_id AND active=1);
      DELETE FROM infra_links WHERE uid=OLD.user_id AND
      (scope='web' OR NOT EXISTS (SELECT 1 FROM trusted_users WHERE user_id=OLD.user_id AND active=1));
    END""",
)

from features import LABELS

TELEGRAM_OVERRIDES = tuple(key for key in LABELS if not key.startswith("format_"))
CLIENT_FEATURES = (
    "telegram_link", "telegram_export", "android_nsu", "android_catalog",
    "android_camera", "android_hid", "android_clean", "android_sort", "android_reconcile",
    "android_documents", "android_duplicates", "android_export", "android_catalog_sync",
) + tuple("telegram_" + key for key in TELEGRAM_OVERRIDES)


class InfraError(Exception):
    def __init__(self, status, code):
        self.status = status
        self.code = code


def digest(secret):
    return sha256(secret.encode("ascii")).hexdigest()


def migrate(repo, path):
    # Схема v2.7.x сохраняется, откат оставляет новые таблицы и триггеры.
    if not repo.db.execute("SELECT 1 FROM sqlite_master WHERE name='infra_meta'").fetchone():
        with repo.transaction():
            if not repo.db.execute("SELECT 1 FROM sqlite_master WHERE name='infra_meta'").fetchone():
                # backup() использует отдельное соединение: источник не держит write transaction.
                source = sqlite3.connect(path, timeout=5)
                destination = path.with_name(path.name + ".pre-infrastructure.bak")
                try:
                    view = object.__new__(Catalog)
                    view.db = source
                    view.backup(destination)
                finally:
                    source.close()
                for statement in SCHEMA:
                    repo.db.execute(statement)


@dataclass(frozen=True)
class Identity:
    id: str
    uid: int
    scope: str
    device: str | None
    csrf: str


class Infrastructure:
    def __init__(self, path=DATABASE, *, settings=load_settings, clock=time):
        self.path = Path(path)
        self.settings = settings
        self.clock = clock

    @contextmanager
    def repository(self):
        settings = self.settings()
        repo = Catalog(self.path, settings.owner_telegram_id, legacy_ids=settings.allowed_user_ids)
        repo.db.execute("PRAGMA foreign_keys=ON")
        repo.db.execute("PRAGMA busy_timeout=1000")
        try:
            yield repo
        finally:
            repo.close()

    def prepare(self):
        with self.repository() as repo:
            migrate(repo, self.path)

    def now(self):
        return int(self.clock())

    def policy(self, repo):
        raw = self.meta(repo, "features")
        saved = json.loads(raw) if raw else {}
        return {key: saved.get(key, True) for key in CLIENT_FEATURES}

    def set_policy(self, actor, values, previous):
        if any(key not in CLIENT_FEATURES or type(value) is not bool for key, value in values.items()):
            raise InfraError(422, "invalid_features")
        with self.repository() as repo, repo.transaction():
            if actor != repo.owner:
                raise InfraError(403, "owner_required")
            old = self.policy(repo)
            if revision(old) != previous:
                raise InfraError(409, "features_changed")
            old.update(values)
            self.put_meta(repo, "features", json.dumps(old))
            repo.log(actor, "Настройки интеграции", "Переключатели")
            return old

    @staticmethod
    def meta(repo, key):
        row = repo.db.execute("SELECT value FROM infra_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def put_meta(repo, key, value):
        repo.db.execute("INSERT INTO infra_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def cleanup(self, repo):
        cutoff = self.now()
        repo.db.execute("DELETE FROM infra_links WHERE expires<=?", (cutoff,))
        repo.db.execute("DELETE FROM infra_sessions WHERE renewal<=?", (cutoff,))

    def link_start(self, uid, name, app_version, proof, scope):
        with self.repository() as repo, repo.transaction():
            self.cleanup(repo)
            if scope == "android" and not self.policy(repo)["telegram_link"]:
                raise InfraError(403, "link_disabled")
            count = repo.db.execute("SELECT count(*) FROM infra_links").fetchone()[0]
            if count >= 256:
                raise InfraError(429, "link_capacity")
            username = self.meta(repo, "bot_username")
            if not username:
                raise InfraError(503, "telegram_not_configured")
            challenge = token_urlsafe(18)
            expires = self.now() + LINK_TTL
            repo.db.execute("INSERT INTO infra_links VALUES (?,?,?,?,?,?,?,0)",
                            (challenge, proof, uid, scope, name, app_version, expires))
            return challenge, expires, username

    def link_for_telegram(self, uid, challenge, decision=None):
        with self.repository() as repo:
            if decision is None:
                return self._eligible_link(repo, uid, challenge)
            with repo.transaction():
                row = self._eligible_link(repo, uid, challenge)
                if not row:
                    return None
                repo.db.execute("UPDATE infra_links SET decision=? WHERE id=?",
                                (1 if decision else -1, challenge))
                return row

    def _eligible_link(self, repo, uid, challenge):
        row = repo.db.execute("SELECT * FROM infra_links WHERE id=? AND uid=? AND expires>? AND decision=0",
                              (challenge, uid, self.now())).fetchone()
        if not row or not repo.allowed(uid):
            return None
        if row["scope"] == "web" and not repo.is_admin(uid):
            return None
        if row["scope"] == "android" and not self.policy(repo)["telegram_link"]:
            return None
        return dict(row)

    def exchange(self, challenge, verifier, scope):
        with self.repository() as repo, repo.transaction():
            row = repo.db.execute("SELECT * FROM infra_links WHERE id=? AND expires>? AND scope=?",
                                  (challenge, self.now(), scope)).fetchone()
            if not row or not compare_digest(row["proof"], digest(verifier)):
                raise InfraError(404, "link_unavailable")
            if row["decision"] == 0:
                raise InfraError(409, "link_pending")
            if row["decision"] < 0:
                raise InfraError(403, "link_declined")
            uid = row["uid"]
            if not repo.allowed(uid) or (scope == "web" and not repo.is_admin(uid)):
                raise InfraError(403, "access_revoked")
            device = None
            if scope == "android":
                if not self.policy(repo)["telegram_link"]:
                    raise InfraError(403, "link_disabled")
                count = repo.db.execute("SELECT count(*) FROM infra_devices WHERE uid=? AND revoked IS NULL", (uid,)).fetchone()[0]
                if count >= MAX_DEVICES:
                    raise InfraError(409, "device_limit")
                device = token_urlsafe(18)
                repo.db.execute("INSERT INTO infra_devices VALUES (?,?,?,?,?,?,NULL)",
                                (device, uid, row["name"], row["app_version"], self.now(), self.now()))
                repo.db.execute("""DELETE FROM infra_devices WHERE id IN (
                    SELECT id FROM infra_devices WHERE uid=? AND revoked IS NOT NULL
                    ORDER BY revoked DESC,id LIMIT -1 OFFSET 60)""", (uid,))
                repo.log(uid, "Подключено устройство", device)
            repo.db.execute("DELETE FROM infra_links WHERE id=?", (challenge,))
            return self._new_session(repo, uid, scope, device)

    def _new_session(self, repo, uid, scope, device=None):
        access = token_urlsafe(32)
        mobile = scope == "android"
        refresh = token_urlsafe(32) if mobile else None
        csrf = "" if mobile else token_urlsafe(32)
        age = ACCESS_TTL if mobile else WEB_TTL
        sid = token_urlsafe(18)
        if not mobile:
            repo.db.execute("""DELETE FROM infra_sessions WHERE id IN (
                SELECT id FROM infra_sessions WHERE uid=? AND scope='web'
                ORDER BY expiry DESC,id LIMIT -1 OFFSET 7)""", (uid,))
        repo.db.execute("INSERT INTO infra_sessions VALUES (?,?,?,?,?,?,?,?,?)",
                        (sid, uid, scope, device, digest(access), digest(refresh) if refresh else None,
                         self.now() + age, self.now() + (REFRESH_TTL if mobile else WEB_TTL), csrf))
        return {"access_token": access, "refresh_token": refresh, "expires_in": age, "device_id": device}

    def identify(self, repo, secret, scope):
        if not secret or len(secret) != 43 or not secret.isascii():
            raise InfraError(401, "authentication_required")
        row = repo.db.execute("SELECT * FROM infra_sessions WHERE access=? AND scope=? AND expiry>?",
                              (digest(secret), scope, self.now())).fetchone()
        return self._identity(repo, row)

    def by_session(self, repo, session_id, scope="android"):
        row = repo.db.execute("SELECT * FROM infra_sessions WHERE id=? AND scope=? AND renewal>?",
                              (session_id, scope, self.now())).fetchone()
        return self._identity(repo, row)

    def _identity(self, repo, row):
        if not row:
            raise InfraError(401, "session_expired")
        if row["scope"] == "web" and not repo.is_admin(row["uid"]):
            raise InfraError(403, "admin_required")
        if not repo.allowed(row["uid"]):
            raise InfraError(403, "access_revoked")
        if row["device"] and not repo.db.execute(
                "SELECT 1 FROM infra_devices WHERE id=? AND uid=? AND revoked IS NULL",
                (row["device"], row["uid"])).fetchone():
            raise InfraError(401, "device_revoked")
        return Identity(row["id"], row["uid"], row["scope"], row["device"], row["csrf"])

    def refresh(self, secret):
        with self.repository() as repo, repo.transaction():
            row = repo.db.execute("SELECT * FROM infra_sessions WHERE refresh=? AND renewal>? AND scope='android'",
                                  (digest(secret), self.now())).fetchone()
            identity = self._identity(repo, row)
            access, refresh = token_urlsafe(32), token_urlsafe(32)
            repo.db.execute("UPDATE infra_sessions SET access=?,refresh=?,expiry=? WHERE id=?",
                            (digest(access), digest(refresh), self.now() + ACCESS_TTL, identity.id))
            repo.db.execute("UPDATE infra_devices SET seen=? WHERE id=?", (self.now(), identity.device))
            return {"access_token": access, "refresh_token": refresh, "expires_in": ACCESS_TTL, "device_id": identity.device}

    def devices(self, repo, uid, page=0):
        return [dict(row) for row in repo.db.execute("""SELECT id,uid,name,app_version,created,seen,revoked
            FROM infra_devices WHERE uid=? ORDER BY revoked IS NOT NULL,created DESC,id LIMIT 21 OFFSET ?""",
            (uid, page * 20))]

    def revoke(self, actor, target, device=None, *, admin=False):
        with self.repository() as repo, repo.transaction():
            if admin:
                repo.require_admin(actor)
                if actor != target and actor != repo.owner and repo.is_admin(target):
                    raise InfraError(403, "protected_role")
            elif actor != target or not repo.allowed(actor):
                raise InfraError(403, "access_revoked")
            args = (target, device) if device else (target,)
            condition = "uid=? AND id=?" if device else "uid=?"
            n = repo.db.execute("UPDATE infra_devices SET revoked=? WHERE revoked IS NULL AND " + condition,
                                (self.now(), *args)).rowcount
            if not n:
                raise InfraError(404, "device_not_found")
            repo.db.execute("DELETE FROM infra_sessions WHERE device IN (SELECT id FROM infra_devices WHERE " +
                            condition + ")", args)
            repo.log(actor, "Отключены устройства", f"{target}: {n}")

    def recovery_issue(self):
        code = token_urlsafe(32)
        with self.repository() as repo, repo.transaction():
            if repo.owner is None:
                raise InfraError(409, "owner_not_configured")
            self.put_meta(repo, "recovery", json.dumps([repo.owner, digest(code), self.now() + LINK_TTL]))
        return code

    def recovery_exchange(self, code):
        with self.repository() as repo, repo.transaction():
            saved = self.meta(repo, "recovery")
            if not saved:
                raise InfraError(401, "recovery_invalid")
            uid, proof, expiry = json.loads(saved)
            if uid != repo.owner or expiry <= self.now() or not compare_digest(proof, digest(code)):
                raise InfraError(401, "recovery_invalid")
            repo.db.execute("DELETE FROM infra_meta WHERE key='recovery'")
            repo.db.execute("DELETE FROM infra_sessions WHERE uid=? AND scope='web'", (uid,))
            repo.log(uid, "Восстановлен доступ к панели", str(uid))
            return self._new_session(repo, uid, "web")


class IntegrationFeatures:
    """Общие переключатели после перехода читаются из той же БД всеми клиентами."""

    def __init__(self, service):
        from features import FeatureStore
        self.service = service
        self.legacy = FeatureStore()

    def snapshot(self):
        with self.service.repository() as repo:
            repo.db.execute("BEGIN")
            return common_features(repo.db, self.legacy.current), self.service.policy(repo)

    def current(self):
        return self.snapshot()[0]

    def telegram(self):
        return TelegramFeatures(self)


class TelegramFeatures:
    def __init__(self, mapping):
        self.mapping = mapping

    def current(self):
        from features import Features
        current, client = self.mapping.snapshot()
        disabled = frozenset(key for key in TELEGRAM_OVERRIDES if not client["telegram_" + key])
        return Features(current.disabled | disabled)
