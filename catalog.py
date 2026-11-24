"""Постоянный каталог SQLite, роли и журнал изменений."""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import tempfile

from barcodes import load_seed_values, validate_value

PAGE_SIZE = 9
SCHEMA_VERSION = 2
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
SCHEMA = (
    f"""CREATE TABLE barcodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, value TEXT NOT NULL COLLATE BINARY,
        format TEXT NOT NULL DEFAULT 'Code 128', active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT ({NOW}), updated_at TEXT NOT NULL DEFAULT ({NOW}),
        created_by INTEGER, revision INTEGER NOT NULL DEFAULT 1,
        UNIQUE(format, value), CHECK (active IN (0,1))
    )""",
    "CREATE INDEX active_barcodes ON barcodes(active, id)",
    "CREATE TABLE admins (user_id INTEGER PRIMARY KEY)",
    f"""CREATE TABLE journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL DEFAULT ({NOW}),
        actor INTEGER NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL
    )""",
)


class CatalogError(ValueError):
    pass


class CatalogDenied(CatalogError):
    pass


class DuplicateBarcode(CatalogError):
    def __init__(self, record: "Barcode"):
        super().__init__("Данный штрихкод уже существует!")
        self.record = record


@dataclass(frozen=True)
class Barcode:
    id: int
    name: str
    value: str
    format: str
    active: int
    created_at: str
    updated_at: str
    created_by: int | None
    revision: int


def validate_name(name: str) -> str:
    if not 1 <= len(name) <= 64 or not name.isprintable() or not name.strip():
        raise CatalogError("Название должно содержать от 1 до 64 печатных символов.")
    return name


class Catalog:
    def __init__(self, path: Path, owner: int | None = None, *, seed=load_seed_values, legacy_ids=None):
        self.owner = owner
        existed = path.is_file() and path.stat().st_size > 0
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=0.15)
        self.db.row_factory = sqlite3.Row
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, SCHEMA_VERSION):
                raise CatalogError("Версия схемы каталога не поддерживается этим выпуском.")
            if version < SCHEMA_VERSION:
                # Перед изменением существующей схемы сохраняем согласованный снимок SQLite.
                if existed:
                    self.backup(path.with_name(f"{path.name}.schema-{version}.bak"))
                with self.transaction():
                    if self.db.execute("PRAGMA user_version").fetchone()[0] == 0:
                        for statement in SCHEMA:
                            self.db.execute(statement)
                        self.db.executemany(
                            "INSERT INTO barcodes(name, value) VALUES (?, ?)",
                            ((value, value) for value in seed()),
                        )
                    if self.db.execute("PRAGMA user_version").fetchone()[0] < 2:
                        self.db.execute(f"""CREATE TABLE trusted_users (
                            user_id INTEGER PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
                            note TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
                            created_at TEXT NOT NULL DEFAULT ({NOW}), created_by INTEGER,
                            updated_at TEXT NOT NULL DEFAULT ({NOW}), revoked_at TEXT, revoked_by INTEGER,
                            revision INTEGER NOT NULL DEFAULT 1)""")
                        self.db.executemany("INSERT OR IGNORE INTO trusted_users(user_id) VALUES (?)",
                                            ((uid,) for uid in legacy_ids or ()))
                        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.execute("""CREATE TABLE IF NOT EXISTS start_visitors(
                user_id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT NOT NULL,
                seen INTEGER NOT NULL)""")
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def backup(self, target: Path) -> None:
        with tempfile.TemporaryDirectory(dir=target.parent) as work:
            staged = Path(work) / "catalog.sqlite3"
            snapshot = sqlite3.connect(staged)
            try:
                self.db.backup(snapshot)
            finally:
                snapshot.close()
            staged.chmod(0o600)
            staged.replace(target)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def is_admin(self, uid: int) -> bool:
        return self.owner is not None and (
            uid == self.owner or self.db.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None
        )

    def require_admin(self, uid: int) -> None:
        if not self.is_admin(uid):
            raise CatalogDenied("Доступ к админ-панели закрыт.")

    def get(self, record_id: int) -> Barcode | None:
        row = self.db.execute("SELECT * FROM barcodes WHERE id=?", (record_id,)).fetchone()
        return Barcode(**row) if row else None

    def by_value(self, value: str) -> Barcode | None:
        row = self.db.execute(
            "SELECT * FROM barcodes WHERE format='Code 128' AND value=?", (value,),
        ).fetchone()
        return Barcode(**row) if row else None

    def page(self, page: int, *, active: bool = False) -> tuple[list[Barcode], bool]:
        where = "WHERE active=1" if active else ""
        rows = self.db.execute(
            f"SELECT * FROM barcodes {where} ORDER BY id LIMIT ? OFFSET ?",
            (PAGE_SIZE + 1, page * PAGE_SIZE),
        ).fetchall()
        return [Barcode(**row) for row in rows[:PAGE_SIZE]], len(rows) > PAGE_SIZE

    def admins(self, page: int) -> tuple[list[int], bool]:
        rows = self.db.execute(
            "SELECT user_id FROM admins WHERE user_id!=? ORDER BY user_id LIMIT ? OFFSET ?",
            (self.owner, PAGE_SIZE + 1, page * PAGE_SIZE),
        ).fetchall()
        return [row[0] for row in rows[:PAGE_SIZE]], len(rows) > PAGE_SIZE

    def recent(self):
        return self.db.execute("SELECT at, actor, action, subject FROM journal ORDER BY id DESC LIMIT 12").fetchall()

    def log(self, uid: int, action: str, subject: str) -> None:
        self.db.execute("INSERT INTO journal(actor, action, subject) VALUES (?, ?, ?)", (uid, action, subject))
        self.db.execute("DELETE FROM journal WHERE id <= last_insert_rowid() - 2000")

    def save(self, uid: int, name: str, value: str, old: Barcode | None = None) -> Barcode:
        with self.transaction():
            self.require_admin(uid)
            validate_name(name)
            validate_value(value)
            if old:
                self.require_current(old)
            duplicate = self.by_value(value)
            if duplicate and (old is None or duplicate.id != old.id):
                raise DuplicateBarcode(duplicate)
            if old is None:
                record_id = self.db.execute(
                    "INSERT INTO barcodes(name,value,created_by) VALUES (?,?,?)", (name, value, uid),
                ).lastrowid
            else:
                record_id = old.id
                self.db.execute(
                    f"""UPDATE barcodes SET name=?, value=?, revision=revision+1, updated_at={NOW}
                    WHERE id=?""", (name, value, record_id),
                )
            self.log(uid, "Добавлен ШК" if old is None else "Изменён ШК", f"#{record_id} {name}")
        return self.get(record_id)

    def require_current(self, old: Barcode) -> None:
        current = self.get(old.id)
        if current is None or current.revision != old.revision:
            raise CatalogError("Запись изменилась. Открой её заново.")

    def change(self, uid: int, old: Barcode, *, delete: bool = False) -> None:
        with self.transaction():
            self.require_admin(uid)
            self.require_current(old)
            if delete:
                self.db.execute("DELETE FROM barcodes WHERE id=?", (old.id,))
                action = "Удалён ШК"
            else:
                self.db.execute(
                    f"UPDATE barcodes SET active=1-active, revision=revision+1, updated_at={NOW} WHERE id=?",
                    (old.id,),
                )
                action = "Отключён ШК" if old.active else "Включён ШК"
            self.log(uid, action, f"#{old.id} {old.name}")

    def set_admin(self, uid: int, target: int, enabled: bool) -> None:
        with self.transaction():
            if self.owner is None or uid != self.owner:
                raise CatalogDenied("Назначать и отзывать права может только владелец.")
            if target == self.owner:
                raise CatalogError("Роль владельца задаётся в конфигурации VPS.")
            if not 0 < target < 2**52:
                raise CatalogError("Некорректный Telegram user ID.")
            if enabled:
                result = self.db.execute("INSERT OR IGNORE INTO admins VALUES (?)", (target,))
            else:
                result = self.db.execute("DELETE FROM admins WHERE user_id=?", (target,))
            if not result.rowcount:
                raise CatalogError("Администратор уже назначен." if enabled else "Права уже отозваны.")
            if enabled:
                self.db.execute("DELETE FROM start_visitors WHERE user_id=?",(target,))
            self.log(uid, "Назначен администратор" if enabled else "Отозваны права", str(target))

    def remember_start(self, uid, name, username):
        import time
        now = int(time.time())
        row = self.db.execute("SELECT seen FROM start_visitors WHERE user_id=?", (uid,)).fetchone()
        if row and now-row[0]<60:
            return False
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO start_visitors VALUES(?,?,?,?)",
                            (uid,name[:160],username or "",now))
            self.db.execute("""DELETE FROM start_visitors WHERE user_id IN
                (SELECT user_id FROM start_visitors ORDER BY seen DESC,user_id DESC LIMIT -1 OFFSET 500)""")
        return True

    def visitors(self, actor, page):
        self.require_admin(actor)
        return self.db.execute("""SELECT * FROM start_visitors ORDER BY seen DESC,user_id DESC
            LIMIT ? OFFSET ?""",(PAGE_SIZE+1,page*PAGE_SIZE)).fetchall()

    def employee(self, target):
        return self.db.execute("SELECT * FROM trusted_users WHERE user_id=?", (target,)).fetchone()

    def allowed(self, target):
        if self.owner is None:
            return False
        if self.is_admin(target):
            return True
        row = self.employee(target)
        return bool(row and row["active"])

    def employees(self, query, page):
        self.db.create_function("fold", 1, str.casefold)
        return self.db.execute("""SELECT * FROM trusted_users WHERE
            instr(fold(display_name),?) OR instr(fold(note),?) OR instr(CAST(user_id AS TEXT),?)
            ORDER BY active DESC,user_id LIMIT ? OFFSET ?""",
            (query.casefold(),query.casefold(),query,PAGE_SIZE+1,page*PAGE_SIZE)).fetchall()

    def change_employee(self, actor, target, action, revision=0, name="", note=""):
        with self.transaction():
            self.require_admin(actor)
            if type(target) is not int or not 0 < target < 2**52:
                raise CatalogError("Некорректный Telegram ID.")
            if self.is_admin(target):
                raise CatalogError("Доступ предоставлен административной ролью.")
            if len(name)>64 or len(note)>200 or any(v and not v.isprintable() for v in (name,note)):
                raise CatalogError("Максимум 64 символа для имени и 200 для примечания.")
            row = self.employee(target)
            if action=="give":
                if row:
                    raise CatalogError("Данный пользователь уже имеет доступ к боту." if row["active"] else "Открой карточку и восстанови доступ.")
                self.db.execute("INSERT INTO trusted_users(user_id,display_name,note,created_by) VALUES (?,?,?,?)",
                                (target,name,note,actor))
            else:
                if not row or row["revision"]!=revision:
                    raise CatalogError("Карточка устарела. Открой её заново.")
                if action in ("disable","restore") and bool(row["active"])==(action=="restore"):
                    raise CatalogError("Статус уже изменился.")
                fields={"disable":(f"active=0,revoked_at={NOW},revoked_by=?", [actor]),
                        "restore":("active=1,revoked_at=NULL,revoked_by=NULL", []),
                        "name":("display_name=?", [name]), "note":("note=?", [note])}
                clause,args=fields[action]
                self.db.execute(f"UPDATE trusted_users SET {clause},revision=revision+1,updated_at={NOW} WHERE user_id=?",(*args,target))
            labels={"give":"Предоставлен доступ","restore":"Восстановлен доступ","disable":"Отозван доступ",
                    "name":"Изменено имя сотрудника","note":"Изменено примечание"}
            if action in ("give","restore"):
                self.db.execute("DELETE FROM start_visitors WHERE user_id=?",(target,))
            self.log(actor,labels[action],str(target))
