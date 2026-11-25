"""Клавиатурная панель управления VPS поверх существующего CLI."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
import curses
import http.client
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from textwrap import wrap
from time import monotonic
from typing import Callable

import cli
from branding import BOT_NAME
from deployment import APP_DIR, SERVICE, DeploymentError, maintenance_lock, remote_revision, release_label, revision_at
from features import DEFAULT_FEATURES, GROUPS, LABELS, load_features, parse_features
from settings import INTEGER_FIELDS, LIMITS, Settings, load_settings


ROOT_ITEMS = (
    ("overview", "Обзор системы", "Версия, состояние службы и ресурсы VPS"),
    ("service", "Управление службой", "Запуск, остановка и автозапуск systemd"),
    ("features", "Функциональность бота", "Рабочие инструменты и форматы в Telegram"),
    ("telegram", "Настройки Telegram", "Подключение, владелец и способ выдачи ШК"),
    ("users", "Пользователи и доступ", "Закрытый доступ и число сотрудников"),
    ("limits", "Безопасность и Anti-Abuse", "Пределы нагрузки, очередей и диалогов"),
    ("storage", "Хранилище и резервные копии", "Данные SQLite и согласованное копирование"),
    ("diagnostics", "Диагностика и журналы", "Состояние компонентов и журнал службы"),
    ("update", "Обновление", "Проверка и установка релиза по HTTPS"),
    ("infrastructure", "API и веб-панель", "Состояние API, домен, порт и доступ владельца"),
)

LIMIT_GROUPS = (
    ("Ограничения пользователя", ("barcode_cooldown", "clean_cooldown", "warning_interval")),
    ("Нагрузка и отправка", ("parallel_operations", "queue_size", "global_rate", "chat_interval",
                             "retry_attempts", "send_timeout")),
    ("Обработка данных", ("data_mib", "data_rows", "data_parallel", "data_timeout", "data_ttl",
                          "max_input_chars", "max_input_lines")),
)
LIMIT_LABELS = dict(zip(LIMITS, cli.LABELS))
LIMIT_LABELS["clean_cooldown"] = "Интервал тяжёлых задач, сек."
LIMIT_HINTS = {
    "barcode_cooldown": "Пауза между выдачами ШК одному сотруднику.",
    "clean_cooldown": "Общая пауза для сканирования, очистки и обработки данных.",
    "warning_interval": "Пауза между однотипными предупреждениями.",
    "parallel_operations": "Активные тяжёлые задачи на весь бот.",
    "queue_size": "Сколько тяжёлых задач может ждать запуска.",
    "global_rate": "Суммарная скорость отправки и редактирования сообщений.",
    "chat_interval": "Пауза между исходящими запросами в один чат.",
    "retry_attempts": "Число повторов после явного ограничения Telegram.",
    "send_timeout": "Общий срок одной отправки, включая ожидание и повторы.",
    "data_mib": "Предел загрузки одного рабочего файла.",
    "data_rows": "Предел записей в одном входном документе.",
    "data_parallel": "Одновременно выполняющиеся длительные обработки.",
    "data_timeout": "Предел времени работы дочернего процесса.",
    "data_ttl": "Срок хранения незавершённого диалога в памяти.",
    "max_input_chars": "Предел длины входного рабочего сообщения.",
    "max_input_lines": "Предел строк при очистке номеров.",
}
VERSION_LABELS = {
    "active": "работает", "inactive": "остановлена", "failed": "ошибка",
    "activating": "запускается", "deactivating": "останавливается",
    "unknown": "статус недоступен",
}
REDACT = re.compile(r"\b[0-9]+:[A-Za-z0-9_-]{20,}\b")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def safe_text(value):
    text = REDACT.sub("[токен скрыт]", ANSI.sub("", str(value)))
    return "".join(char if char.isprintable() or char in "\n\t" else " " for char in text)


def capture(argv, timeout=5):
    return subprocess.run(argv, check=False, capture_output=True, text=True, errors="replace", timeout=timeout)


def system_info():
    fields = ("ActiveState", "UnitFileState", "MainPID", "ActiveEnterTimestampMonotonic")
    try:
        result = capture(["systemctl", "show", SERVICE, "--no-pager", *(f"--property={name}" for name in fields)])
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    except (OSError, subprocess.SubprocessError):
        values = {}
    active = values.get("ActiveState", "unknown")
    uptime = rss = None
    try:
        since = int(values.get("ActiveEnterTimestampMonotonic", "0"))
        if active == "active" and since > 0:
            uptime = max(0, int(monotonic() - since / 1_000_000))
        pid = int(values.get("MainPID", "0"))
        if pid:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                    break
    except (OSError, ValueError, IndexError):
        pass
    return active, values.get("UnitFileState"), uptime, rss


def size_label(n):
    return f"{n / 1024**2:.1f} МиБ" if n is not None else "—"


def uptime_label(seconds):
    if seconds is None:
        return "—"
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours} ч {minutes:02d} мин" if hours else f"{minutes} мин {seconds:02d} сек"


@dataclass
class Snapshot:
    release: str = "—"
    revision: str | None = None
    active: str = "unknown"
    startup: str | None = None
    uptime: int | None = None
    rss: int | None = None
    token: bool = False
    owner: int | None = None
    enabled_features: int | None = None
    database: bool = False
    admin_count: int | None = None
    active_staff: int | None = None
    disabled_staff: int | None = None
    pending_staff: int | None = None
    barcode_count: int | None = None
    active_barcodes: int | None = None
    schema: int | None = None
    db_size: int | None = None
    backups: tuple = ()
    cpu: int | None = None
    available_memory: int | None = None
    free_disk: int | None = None


def snapshot():
    active, startup, uptime, rss = system_info()
    data = Snapshot(release_label(APP_DIR), revision_at(APP_DIR), active, startup, uptime, rss,
                    token=cli.configured(), cpu=os.cpu_count())
    try:
        data.owner = load_settings(cli.SETTINGS_PATH).owner_telegram_id
    except (OSError, ValueError):
        pass
    try:
        data.enabled_features = sum(cli.read_feature_policy().options().values())
    except (OSError, ValueError):
        pass
    try:
        data.db_size = cli.DB.stat().st_size
        with closing(cli.database()) as db:
            data.schema = db.execute("PRAGMA user_version").fetchone()[0]
            data.admin_count = db.execute("SELECT count(*) FROM admins").fetchone()[0]
            counts = dict(db.execute("SELECT active, count(*) FROM trusted_users GROUP BY active"))
            data.active_staff, data.disabled_staff = counts.get(1, 0), counts.get(0, 0)
            data.barcode_count, data.active_barcodes = db.execute(
                "SELECT count(*), count(CASE WHEN active=1 THEN 1 END) FROM barcodes"
            ).fetchone()
            try:
                data.pending_staff = db.execute("SELECT count(*) FROM start_visitors").fetchone()[0]
            except sqlite3.Error:
                pass
            data.database = True
    except (OSError, sqlite3.Error):
        pass
    try:
        recent = sorted(cli.BACKUPS.glob("*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
        data.backups = tuple((p.name, p.stat().st_size) for p in recent[:8])
    except OSError:
        pass
    try:
        for row in Path("/proc/meminfo").read_text().splitlines():
            if row.startswith("MemAvailable:"):
                data.available_memory = int(row.split()[1]) * 1024
                break
        data.free_disk = shutil.disk_usage(cli.DB.parent if cli.DB.parent.exists() else Path("/")).free
    except (OSError, ValueError):
        pass
    return data


def saved_token():
    with cli.ENV_FILE.open(encoding="utf-8") as config:
        for row in config:
            if row.startswith("BOT_TOKEN="):
                return row[10:].strip()
    raise ValueError("Сначала задай токен в настройках Telegram.")


def command_args(action, *options):
    return [sys.executable, "-u", str(Path(cli.__file__).resolve()), action, *options]


def run_command(action, *options, emit):
    if action == "update":
        with subprocess.Popen(command_args(action, *options), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, text=True, errors="replace") as child:
            for text in child.stdout:
                emit(text.rstrip())
            return child.wait() == 0
    result = subprocess.run(command_args(action, *options), capture_output=True, text=True, errors="replace",
                            stdin=subprocess.DEVNULL, timeout=160)
    for text in (result.stdout + result.stderr).splitlines():
        emit(text)
    return result.returncode == 0


@dataclass
class Entry:
    label: str
    action: Callable | None = None
    hint: str = ""
    tone: str = "plain"


@dataclass
class Page:
    name: str
    cursor: int = 0
    offset: int = 0
    detail_offset: int = 0
    detail_focused: bool = False


@dataclass
class Dialog:
    title: str
    prompt: str
    accept: Callable
    kind: str = "confirm"
    value: str = ""
    yes: bool = False


@dataclass
class Work:
    label: str
    future: object
    on_done: Callable | None
    quiet: bool
    lines: deque = field(default_factory=lambda: deque(maxlen=120))


class ControlPanel:
    """Отрисовка, клавиши и один ограниченный исполнитель задач."""

    def __init__(self, window, *, snapshot_loader=snapshot, initial=None):
        self.window = window
        self.snapshot_loader = snapshot_loader
        self.info = initial or Snapshot()
        self.pages = [Page("root")]
        self.options = {}
        self.baseline = DEFAULT_FEATURES
        self.detail = ""
        self.dialog = None
        self.work = None
        self.notice = ""
        self.notice_tone = "muted"
        self.output = []
        self.detail_lines = []
        self.live_due = 0
        self.stopping = False
        self.colors = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="numberbot-tui")
        self.setup_terminal()

    @property
    def page(self):
        return self.pages[-1]

    def setup_terminal(self):
        self.window.keypad(True)
        self.window.timeout(100)
        curses.set_escdelay(40)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if not curses.has_colors():
            return
        curses.start_color()
        palette = {
            "plain": (curses.COLOR_WHITE, curses.COLOR_BLACK),
            "muted": (curses.COLOR_CYAN, curses.COLOR_BLACK),
            "accent": (curses.COLOR_CYAN, curses.COLOR_BLACK),
            "good": (curses.COLOR_GREEN, curses.COLOR_BLACK),
            "warn": (curses.COLOR_YELLOW, curses.COLOR_BLACK),
            "failure": (curses.COLOR_RED, curses.COLOR_BLACK),
            "focus": (curses.COLOR_BLACK, curses.COLOR_CYAN),
        }
        for index, (name, (fg, bg)) in enumerate(palette.items(), 1):
            curses.init_pair(index, fg, bg)
            self.colors[name] = curses.color_pair(index)
        self.window.bkgd(" ", self.colors["plain"])

    def tell(self, text, tone="good"):
        self.notice, self.notice_tone = safe_text(text), tone

    def operation(self, title, action, on_done=None, *, quiet=False):
        if self.work:
            self.tell("Дождись завершения текущего действия.", "warn")
            return False
        lines = deque(maxlen=120)

        def emit(text):
            for line in safe_text(text).splitlines():
                lines.append(line[:500])

        future = self.executor.submit(action, emit)
        self.work = Work(title, future, on_done, quiet, lines)
        return True

    def observe_work(self):
        job = self.work
        if not job or not job.future.done():
            return
        self.work = None
        self.output = list(job.lines)
        try:
            result = job.future.result()
            if job.on_done is not None:
                job.on_done(result)
            if not job.quiet:
                self.tell(self.output[-1] if self.output else ("Готово." if result is not False else "Операция завершилась с ошибкой."),
                          "failure" if result is False else "good")
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError, http.client.HTTPException) as error:
            details = safe_text(error)
            self.tell(f"{job.label}. {details}", "failure")
            self.output.append(self.notice)

    def refresh(self):
        self.operation("Считываю состояние", lambda emit: self.snapshot_loader(), self.set_info, quiet=True)

    def set_info(self, data):
        self.info = data

    def open(self, name):
        if name == "features":
            self.baseline = cli.read_feature_policy()
            self.options = self.baseline.options()
        elif name == "live":
            self.live_due = 0
        self.pages.append(Page(name, cursor=1 if name in {"features", "limits"} else 0))
        self.output = []
        self.notice = ""

    def back(self):
        if self.dialog:
            self.dialog = None
        elif self.page.name == "features" and self.options != self.baseline.options():
            self.confirm("Закрыть настройки", "Отбросить изменения переключателей?", self.pop_page)
        else:
            self.pop_page()

    def pop_page(self):
        if len(self.pages) == 1:
            if self.work and not self.work.quiet:
                self.tell("Дождись завершения операции перед выходом.", "warn")
                return
            self.stopping = True
        else:
            self.pages.pop()
            self.output = []
            self.notice = ""

    def confirm(self, title, prompt, action):
        self.dialog = Dialog(title, prompt, action)

    def field(self, title, prompt, action, *, secret=False):
        self.dialog = Dialog(title, prompt, action, "secret" if secret else "text")

    def invoke(self, action, title, *, confirmed=False):
        def execute(emit):
            result = run_command(action, emit=emit)
            if action in {"start", "stop", "restart", "status", "backup"}:
                self.set_info(self.snapshot_loader())
            return result

        def start():
            self.operation(title, execute)
        if confirmed:
            self.confirm(title, "Выполнить действие?", start)
        else:
            start()

    def apply(self, title, changes=None, token=None, launch=False):
        def execute(emit):
            if token is not None:
                cli.verify_token(token)
            with maintenance_lock():
                cli.apply_settings(changes or {}, token, launch, report=emit)
            return self.snapshot_loader()

        self.confirm(title, "Сохранить настройки и применить их к службе?",
                     lambda: self.operation(title, execute, self.set_info))

    def edit_limit(self, name):
        settings = load_settings(cli.SETTINGS_PATH)
        low, high = LIMITS[name]

        def accept(raw):
            value = int(raw) if name in INTEGER_FIELDS else float(raw.replace(",", "."))
            if not low <= value <= high:
                raise ValueError(f"Допустимый диапазон {low}–{high}.")
            self.apply("Изменение параметра защиты", {name: value})

        self.field(LIMIT_LABELS[name],
                   f"{LIMIT_HINTS[name]} Сейчас {getattr(settings, name)} · диапазон {low}–{high} · "
                   f"стандарт {getattr(Settings(), name)}",
                   accept)

    def save_features(self):
        if self.options == self.baseline.options():
            self.tell("Изменений нет.", "muted")
            return

        def commit():
            self.baseline = cli.save_features(self.options, self.baseline)
            self.info.enabled_features = sum(self.baseline.options().values())
            self.tell("Сохранено. Меню обновятся без перезапуска.")

        self.confirm("Применить переключатели", "Сохранить изменения доступности для сотрудников?", commit)

    def update_check(self):
        def check(emit):
            return remote_revision()

        def ready(revision):
            if revision == revision_at(APP_DIR):
                self.tell("Установлена актуальная ревизия.")
                return
            def update_now():
                def execute(emit):
                    result = run_command("update", "--expected-revision", revision, emit=emit)
                    self.set_info(self.snapshot_loader())
                    return result

                self.operation("Установка обновления", execute)
            self.confirm("Обновление по HTTPS", f"Установить ревизию {revision[:12]}?", update_now)

        self.operation("Проверка GitHub", check, ready, quiet=True)

    def refresh_live(self):
        if self.work or monotonic() < self.live_due:
            return
        self.live_due = monotonic() + 2

        def logs(emit):
            result = capture(["journalctl", "-u", SERVICE, "--no-pager", "-n", "60"])
            for line in (result.stdout + result.stderr).splitlines():
                emit(line)
            return result.returncode == 0

        self.operation("Обновление журнала", logs, quiet=True)

    def system_detail(self):
        info = self.info
        return [
            "СЛУЖБА И ДОСТУП",
            f"Состояние  {VERSION_LABELS.get(info.active, info.active)}",
            f"Время работы  {uptime_label(info.uptime)}",
            f"Токен  {'настроен' if info.token else 'не задан'}",
            f"Владелец  {info.owner or 'не назначен'}",
            "Режим доступа  закрытый",
            f"Администраторы  {number(info.admin_count)}",
            f"Допущенные сотрудники  {number(info.active_staff)}",
            "",
            "ХРАНИЛИЩЕ И РЕСУРСЫ",
            f"SQLite  {'доступна' if info.database else 'недоступна'}",
            f"Активные ШК  {number(info.active_barcodes)}",
            f"Включённые функции  {number(info.enabled_features)} / {len(LABELS)}",
            f"Память процесса  {size_label(info.rss)}",
            f"Доступная ОЗУ  {size_label(info.available_memory)}",
            f"Свободно на диске  {size_label(info.free_disk)}",
            f"Логические CPU  {number(info.cpu)}",
            f"Ревизия  {info.revision or 'не указана'}",
        ]

    def page_content(self):
        name, info = self.page.name, self.info

        def act(label, command, hint="", confirmed=False):
            return Entry(label, lambda: self.invoke(command, label, confirmed=confirmed), hint)

        if name == "root":
            return ([Entry(label, lambda target=target: self.open(target), hint) for target, label, hint in ROOT_ITEMS]
                    + [Entry("Выход", self.pop_page)], self.system_detail())
        if name == "overview":
            return [Entry("Обновить показатели", self.refresh), *(Entry(line) for line in self.system_detail())], [
                "Показатели считываются локально.", "Соединение с Telegram и GitHub проверяется по запросу."
            ]
        if name == "service":
            def startup():
                action = "disable" if info.startup == "enabled" else "enable"
                def change(emit):
                    with maintenance_lock():
                        result = capture(["systemctl", action, SERVICE], timeout=20)
                    for line in (result.stdout + result.stderr).splitlines():
                        emit(line)
                    self.set_info(self.snapshot_loader())
                    return result.returncode == 0
                self.confirm("Автозапуск", "Изменить запуск службы при загрузке VPS?",
                             lambda: self.operation("Автозапуск", change))
            return [
                act("Запустить бота", "start"), act("Остановить бота", "stop", confirmed=True),
                act("Перезапустить бота", "restart", confirmed=True), act("Проверить статус", "status"),
                Entry("Управление автозапуском", startup, "Меняет включение службы при загрузке VPS"),
            ], [f"Служба  {VERSION_LABELS.get(info.active, info.active)}", f"Время работы  {uptime_label(info.uptime)}",
                f"Автозапуск  {info.startup or '—'}"]
        if name == "infrastructure":
            host, port = cli.api_settings()
            def domain():
                self.field("Домен API", f"Текущий адрес: {host}", lambda value: self.operation(
                    "Настройка API", lambda emit: run_command("api-config", "--host", value, "--port", str(port), emit=emit)))
            def endpoint():
                self.field("Порт API", f"Текущий порт: {port}", lambda value: self.operation(
                    "Настройка API", lambda emit: run_command("api-config", "--host", host, "--port", value, emit=emit)))
            return ([act("Проверить API", "api-status"), act("Запустить API", "api-start"),
                     act("Остановить API", "api-stop", confirmed=True),
                     Entry("Настроить домен", domain), Entry("Настроить порт", endpoint),
                     act("Показать конфигурацию Caddy", "proxy")],
                    ["API слушает только loopback.", "Код входа владельца: sudo numberbot web-access.",
                     "Список устройств доступен в Control Center и через /devices."])
        if name == "features":
            draft = parse_features(self.options)
            entries = []
            for group, items in GROUPS:
                entries.append(Entry(group.upper(), tone="accent"))
                for key, label in items:
                    enabled = self.options[key]
                    status = "ВКЛ" if enabled else "ВЫКЛ"
                    dependent = enabled and not draft.available(key)
                    description = ("Включи один из поддерживаемых форматов входных данных." if dependent else
                                   "Enter переключает настройку. Сохранение выполняется отдельно.")
                    suffix = " · нужен формат" if dependent else ""
                    entries.append(Entry(f"[{status}] {label}{suffix}", lambda key=key: self.flip(key), description,
                                         "warn" if dependent else "good" if enabled else "muted"))
            entries.extend([
                Entry("Применить изменения", self.save_features),
                Entry("Отменить изменения", self.discard),
                Entry("Включить все функции", lambda: self.restore_features("Включить все функции")),
                Entry("Восстановить стандартные настройки", lambda: self.restore_features("Стандартные настройки")),
            ])
            return entries, [f"Включено в черновике  {sum(self.options.values())} / {len(self.options)}",
                             "Данные каталога и права сохраняются. Изменения вступают в силу без перезапуска.",
                             "При отключении прекращается приём новых операций. Открытые меню могут обновиться при следующем входе."]
        if name == "telegram":
            def verify():
                self.operation("Проверка Telegram API", lambda emit: cli.verify_token(saved_token()))
            def owner():
                self.field("Telegram ID владельца", "Введи положительный числовой ID.",
                           lambda value: self.apply("Назначение владельца", {"owner_telegram_id": cli.uid(value)}))
            return [
                Entry("Установить или заменить токен", lambda: self.field("Токен от @BotFather", "Ввод скрыт.",
                                                                         lambda value: self.apply("Сохранение токена", token=value), secret=True)),
                Entry("Проверить токен и соединение", verify, "Проверяет Telegram методом getMe"),
                Entry("Назначить или изменить владельца", owner),
                Entry("Отправлять ШК как фото", lambda: self.apply("Отправка ШК", {"barcode_mode": "photo"})),
                Entry("Отправлять оригинальный PNG", lambda: self.apply("Отправка ШК", {"barcode_mode": "document"})),
                Entry("Первоначальная настройка", self.setup),
            ], [f"Токен  {'настроен' if info.token else 'не задан'}", f"Владелец  {info.owner or 'не назначен'}",
                f"Отправка ШК  {load_settings(cli.SETTINGS_PATH).barcode_mode}"]
        if name == "users":
            return [
                Entry(f"Владелец  {info.owner or 'не назначен'}"), Entry("Доступ  закрытый"),
                Entry(f"Администраторы  {number(info.admin_count)}"),
                Entry(f"Допущенные сотрудники  {number(info.active_staff)}"),
                Entry(f"Отключённые сотрудники  {number(info.disabled_staff)}"),
                Entry(f"Ожидают допуска  {number(info.pending_staff)}"),
                Entry("Обновить показатели", self.refresh),
            ], ["Сотрудники и администраторы управляются через /admin в Telegram."]
        if name == "limits":
            settings = load_settings(cli.SETTINGS_PATH)
            rows = []
            for group, names in LIMIT_GROUPS:
                rows.append(Entry(group.upper(), tone="accent"))
                for key in names:
                    low, high = LIMITS[key]
                    rows.append(Entry(f"{LIMIT_LABELS[key]}  {getattr(settings, key)}",
                                      lambda key=key: self.edit_limit(key),
                                      f"Диапазон {low}–{high}. Стандарт {getattr(Settings(), key)}."))
            return rows, ["Служебные ответы  4 сразу, затем одно разрешение в секунду",
                          "Дедлайн обработки данных  45 сек. вместе с очередью и доставкой",
                          "Записи /start  до 12 в минуту"]
        if name == "storage":
            return [act("Создать согласованную копию", "backup", confirmed=True),
                    Entry("Обновить показатели", self.refresh),
                    *(Entry(f"{label}  {value}") for label, value in [
                        ("Путь", cli.DB), ("SQLite", "доступна" if info.database else "недоступна"),
                        ("Размер", size_label(info.db_size)), ("Схема", number(info.schema)),
                        ("Записи каталога", number(info.barcode_count)),
                        ("Записи сотрудников", number(info.active_staff + info.disabled_staff
                                                       if info.active_staff is not None else None)),
                    ]),
                    Entry("ПОСЛЕДНИЕ РЕЗЕРВНЫЕ КОПИИ", tone="accent"),
                    *(Entry(f"{filename}  {size_label(size)}") for filename, size in info.backups)
                    ], [f"Копии  {cli.BACKUPS}"]
        if name == "diagnostics":
            return [
                act("Общая проверка", "diagnose", "Настройки, systemd, SQLite, зависимости, GitHub и Telegram"),
                act("Последние события службы", "logs"), Entry("Журнал в реальном времени", lambda: self.open("live")),
                act("Запустить встроенные тесты", "test"),
            ], ["Сетевые проверки запускаются по запросу. Результаты появятся в правой панели или ниже."]
        if name == "live":
            return [Entry(line) for line in self.output] or [Entry("Ожидаю события журнала…")], [
                "Последние события обновляются каждые две секунды."
            ]
        return [Entry("Проверить обновление", self.update_check)], [
            f"Установлено  {info.release}", f"Git-ревизия  {info.revision or '—'}",
            "Источник  GitHub · main · HTTPS", "Будут показаны этапы подготовки, проверки и переключения релиза."
        ]

    def setup(self):
        def token_received(token):
            self.field("Владелец", "Введи Telegram ID владельца или оставь поле пустым.",
                       lambda value: self.apply("Первоначальная настройка",
                                                {"owner_telegram_id": cli.uid(value)} if value else {}, token, True))
        if cli.configured():
            token_received(None)
        else:
            self.field("Первоначальная настройка", "Введи токен @BotFather. Ввод скрыт.", token_received, secret=True)

    def flip(self, key):
        self.options[key] = not self.options[key]

    def discard(self):
        self.options = self.baseline.options()
        self.tell("Черновик отменён.", "muted")

    def restore_features(self, title):
        def restore():
            self.options = DEFAULT_FEATURES.options()
            self.tell("Черновик обновлён. Примени изменения для сохранения.")
        self.confirm(title, "Включить все переключатели в черновике?", restore)

    def paint(self, entries, detail):
        win = self.window
        height, width = win.getmaxyx()
        win.erase()

        def write(y, x, text, tone="plain", bold=False):
            if not (0 <= y < height - 1 and 0 <= x < width - 1):
                return
            try:
                win.addnstr(y, x, safe_text(text).replace("\t", " ").replace("\n", " "), width - x - 1,
                            self.colors.get(tone, curses.A_REVERSE if tone == "focus" else 0)
                            | (curses.A_BOLD if bold else 0))
            except curses.error:
                pass

        if height < 9 or width < 28:
            write(0, 0, "Терминал слишком мал. Escape — выход.")
            win.refresh()
            return
        x = 2 if width >= 50 else 1
        write(0, x, BOT_NAME, "accent", True)
        active = VERSION_LABELS.get(self.info.active, self.info.active)
        overview = (f"{self.info.release.split()[0]} · {active} · Закрытый доступ" if width < 58 else
                    f"{self.info.release}  ·  Служба {active}  ·  Доступ закрытый")
        write(1, x, overview,
              "good" if self.info.active == "active" else "failure" if self.info.active == "failed" else "warn")
        write(2, x, "─" * (width - 2*x - 1), "muted")
        labels = {item[0]: item[1] for item in ROOT_ITEMS}
        labels.update(root="Панель VPS", live="Журнал в реальном времени")
        write(3, x, labels[self.page.name], "accent", True)

        wide = width >= 90
        if len(self.output) and self.page.name != "live":
            detail = [*detail, "", "РЕЗУЛЬТАТ", *self.output]
        if self.work and self.work.lines and self.page.name != "live":
            detail = [*detail, "", "ХОД ОПЕРАЦИИ", *list(self.work.lines.copy())[-6:]]
        if not wide and detail:
            entries = [*entries, Entry(""), *(Entry(line, tone="muted") for paragraph in detail
                                              for line in (wrap(paragraph, width - x - 5) or [""]))]
        visible = max(1, height - 9)
        page = self.page
        page.cursor = min(page.cursor, max(0, len(entries) - 1))
        page.offset = max(0, min(page.offset, page.cursor))
        if page.cursor >= page.offset + visible:
            page.offset = page.cursor - visible + 1
        split = min(48, width // 2) if wide else width - x - 1
        self.detail_lines = []
        if not wide:
            page.detail_focused = False
        for row, entry in enumerate(entries[page.offset:page.offset + visible], 5):
            selected = page.offset + row - 5 == page.cursor and not page.detail_focused
            line = ("› " if selected else "  ") + entry.label
            tone = "focus" if selected else entry.tone
            write(row, x, line[:split-x-1].ljust(split-x-1) if selected else line[:split-x-1], tone, selected)
        if wide:
            lines = [line for paragraph in detail for line in (wrap(paragraph, width - split - 5) or [""])]
            self.detail_lines = lines
            page.detail_offset = min(page.detail_offset, max(0, len(lines) - visible))
            if page.detail_focused:
                write(4, split + 2, "Подробности", "focus", True)
            for row, line in enumerate(lines[page.detail_offset:page.detail_offset + visible], 5):
                write(row, split + 2, line, "accent" if line.isupper() else "plain")
        entry = entries[page.cursor] if entries else Entry("")
        if entry.hint:
            write(height - 4, x, entry.hint, "muted")
        if self.work and not self.work.quiet:
            write(height - 3, x, f"Выполняется · {self.work.label}", "warn", True)
        elif self.notice:
            write(height - 3, x, self.notice, self.notice_tone, True)
        footer = "↑↓ выбор   Enter открыть   Esc назад"
        if width >= 52:
            footer += "   F5 обновить"
        if self.detail_lines:
            footer += "   Tab подробности"
        write(height - 2, x, footer, "muted")
        if self.dialog:
            dialog = self.dialog
            modal_width = min(width - 4, 64)
            left = (width - modal_width) // 2
            lines = wrap(dialog.prompt, modal_width - 4)
            lines = lines[:max(1, height - 8)]
            top = max(1, (height - len(lines) - 6) // 2)
            for row in range(top, top + len(lines) + 6):
                write(row, left, " " * modal_width, "focus")
            write(top + 1, left + 2, dialog.title[:modal_width - 4], "focus", True)
            for row, line in enumerate(lines, top + 2):
                write(row, left + 2, line, "focus")
            bottom = top + len(lines) + 3
            if dialog.kind == "confirm":
                answer = "[  Да  ]    Нет" if dialog.yes else "   Да     [ Нет ]"
            else:
                value = "•" * len(dialog.value) if dialog.kind == "secret" else dialog.value
                answer = "› " + value[-(modal_width - 6):]
            write(bottom, left + 2, answer, "focus", True)
            write(bottom + 1, left + 2, "Enter · подтвердить   Esc · отмена"[:modal_width-4], "focus")
        win.refresh()
        return entries

    def handle(self, key, entries):
        if self.dialog:
            dialog = self.dialog
            if key == "\x1b":
                self.dialog = None
            elif key in ("\n", "\r", curses.KEY_ENTER):
                self.dialog = None
                if dialog.kind != "confirm" or dialog.yes:
                    dialog.accept() if dialog.kind == "confirm" else dialog.accept(dialog.value.strip())
            elif dialog.kind == "confirm" and key in (curses.KEY_LEFT, curses.KEY_RIGHT, "\t", " "):
                dialog.yes = not dialog.yes
            elif dialog.kind != "confirm":
                if key in ("\x7f", "\b", curses.KEY_BACKSPACE):
                    dialog.value = dialog.value[:-1]
                elif isinstance(key, str) and key.isprintable() and len(dialog.value) < 256:
                    dialog.value += key
            return

        if key == "\x1b":
            self.back()
        elif key == curses.KEY_F5:
            self.refresh()
        elif key == "\t" and self.detail_lines:
            self.page.detail_focused = not self.page.detail_focused
        elif key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE,
                     curses.KEY_HOME, curses.KEY_END) and entries:
            page_size = max(1, self.window.getmaxyx()[0] - 9)
            limit = len(self.detail_lines) if self.page.detail_focused else len(entries)
            movement = {curses.KEY_UP: -1, curses.KEY_DOWN: 1, curses.KEY_PPAGE: -page_size, curses.KEY_NPAGE: page_size,
                        curses.KEY_HOME: -limit, curses.KEY_END: limit}[key]
            if self.page.detail_focused:
                self.page.detail_offset = max(0, min(limit - page_size, self.page.detail_offset + movement))
            else:
                index = max(0, min(limit - 1, self.page.cursor + movement))
                if entries[index].tone == "accent" and key in (curses.KEY_UP, curses.KEY_DOWN):
                    next_index = index + movement
                    index = next_index if 0 <= next_index < limit else self.page.cursor
                self.page.cursor = index
        elif key in ("\n", "\r", curses.KEY_ENTER) and entries:
            entry = entries[self.page.cursor]
            if entry.action and not self.page.detail_focused and (not self.work or self.work.quiet):
                entry.action()

    def run(self):
        self.refresh()
        try:
            while not self.stopping:
                self.observe_work()
                if self.page.name == "live":
                    self.refresh_live()
                try:
                    entries, detail = self.page_content()
                except (OSError, ValueError, sqlite3.Error) as error:
                    entries, detail = [Entry("Назад", self.back)], []
                    self.tell(error, "failure")
                entries = self.paint(entries, detail)
                try:
                    key = self.window.get_wch()
                except curses.error:
                    continue
                try:
                    self.handle(key, entries or [])
                except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
                    self.tell(error, "failure")
        finally:
            self.dialog = None
            self.executor.shutdown(wait=True)


def number(value):
    return "—" if value is None else str(value)


def launch():
    if os.environ.get("TERM") in {None, "", "dumb"}:
        raise RuntimeError("Терминал не поддерживает полноэкранный режим.")
    try:
        curses.wrapper(lambda window: ControlPanel(window).run())
    except curses.error as error:
        raise RuntimeError("Не удалось открыть полноэкранный режим.") from error
