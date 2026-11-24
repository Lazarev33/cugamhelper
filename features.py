"""Единая политика доступности пользовательских инструментов."""

from dataclasses import dataclass
import json
import logging
from pathlib import Path

FEATURES_PATH = Path("/etc/tg-number-cleaner/features.json")

GROUPS = (
    ("ШК", (
        ("barcode_catalog", "Каталог и выдача ШК"),
        ("barcode_scan", "Сканирование ШК"),
    )),
    ("Обработка данных", (
        ("clean_numbers", "Очистка номеров"),
        ("data_sort", "Сортировка"),
        ("data_quick_reconcile", "Сверка двух списков"),
        ("data_pair_reconcile", "Сверка пар"),
        ("data_reference_reconcile", "Сверка с эталоном"),
        ("data_table_reconcile", "Сверка таблиц"),
        ("data_find_duplicates", "Поиск дубликатов"),
        ("data_remove_duplicates", "Удаление дубликатов"),
    )),
    ("Входные форматы", (
        ("format_xlsx", "Excel XLSX"),
        ("format_docx", "Word DOCX"),
        ("format_txt", "Файлы TXT"),
        ("format_text", "Текстовые сообщения"),
    )),
)
LABELS = {key: label for _, items in GROUPS for key, label in items}
FORMATS = frozenset({"text", "txt", "xlsx", "docx"})
MODES = {
    "clean": ("clean_numbers", {"text"}),
    "asc": ("data_sort", FORMATS),
    "desc": ("data_sort", FORMATS),
    "check": ("data_quick_reconcile", {"text", "txt", "docx"}),
    "quick": ("data_quick_reconcile", {"text", "txt", "docx"}),
    "pairs": ("data_pair_reconcile", FORMATS),
    "ref": ("data_reference_reconcile", FORMATS),
    "align": ("data_table_reconcile", {"xlsx", "docx"}),
    "dups": ("data_find_duplicates", FORMATS),
    "unique": ("data_remove_duplicates", FORMATS),
}
DATA_ACTIONS = (
    ("asc", "Сортировка", "Сортировать"),
    ("check", "Сверка данных", "Сверить два списка"),
    ("pairs", "Сверка пар", "Проверить пары"),
    ("ref", "Сверка с эталоном", "Использовать как эталон"),
    ("align", "Сверка таблиц", "Сопоставить столбцы"),
    ("dups", "Поиск дубликатов", "Найти дубликаты"),
    ("unique", "Удалить дубликаты", "Удалить дубликаты"),
)


@dataclass(frozen=True)
class Features:
    disabled: frozenset[str] = frozenset()

    def enabled(self, key):
        return key in LABELS and key not in self.disabled

    def input_enabled(self, kind):
        return kind in FORMATS and self.enabled("format_" + kind)

    def permits(self, mode, *kinds):
        key, supported = MODES[mode]
        if not self.enabled(key):
            return False
        if kinds:
            return all(kind in supported and self.input_enabled(kind) for kind in kinds)
        return any(self.input_enabled(kind) for kind in supported)

    @property
    def data(self):
        return any(self.permits(mode) for mode, _, _ in DATA_ACTIONS)

    @property
    def barcodes(self):
        return self.enabled("barcode_catalog") or self.enabled("barcode_scan")

    def available(self, key):
        modes = [mode for mode, (feature, _) in MODES.items() if feature == key]
        return self.enabled(key) and (not modes or any(self.permits(mode) for mode in modes))

    def options(self):
        return {key: self.enabled(key) for key in LABELS}

    def offers(self, kind):
        order = ("align", "check", "asc", "dups", "unique", "pairs", "ref")
        labels = {mode: label for mode, _, label in DATA_ACTIONS}
        actions = [(labels[mode], "quick" if mode == "check" else mode)
                   for mode in order if self.permits(mode, kind)
                   and (mode != "unique" or not self.permits("dups", kind))]
        if self.permits("clean", kind):
            actions.append(("Очистить номера", "clean"))
        return actions

    def allows_dialog(self, stage, data):
        if stage == "clean":
            return self.permits("clean", "text")
        if (stage or "").startswith("Flow:") and data.get("kind") == "scan":
            return self.enabled("barcode_scan")
        if stage == "Data:offer" and data.get("source"):
            return bool(self.offers(data["source"]["kind"]))
        if stage == "Data:input" and data.get("mode") in MODES:
            kinds = data.get("kinds", ())
            if data.get("first"):
                kinds = (data["first"]["kind"],)
            return self.permits(data["mode"], *kinds)
        return True


DEFAULT_FEATURES = Features()
ALL_DISABLED = Features(frozenset(LABELS))


def parse_features(payload):
    if not isinstance(payload, dict) or payload.keys() - LABELS.keys() or any(
        type(value) is not bool for value in payload.values()
    ):
        raise ValueError("В файле функций ожидаются известные переключатели со значениями true или false.")
    return Features(frozenset(key for key, value in payload.items() if not value))


def load_features(path=FEATURES_PATH):
    try:
        with path.open("rb") as stream:
            raw = stream.read(8193)
    except FileNotFoundError:
        return DEFAULT_FEATURES
    if len(raw) > 8192:
        raise ValueError("Файл функций превышает допустимый размер.")
    try:
        return parse_features(json.loads(raw))
    except (UnicodeError, RecursionError, json.JSONDecodeError):
        raise ValueError("Файл функций содержит некорректный JSON.") from None


class FeatureStore:
    """Читает новый снимок только при изменении атомарно заменяемого файла."""

    def __init__(self, path=FEATURES_PATH):
        self.path = path
        self.signature = object()
        self.snapshot = ALL_DISABLED

    def current(self):
        try:
            info = self.path.stat()
            signature = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
        except FileNotFoundError:
            self.signature = None
            self.snapshot = DEFAULT_FEATURES
            return self.snapshot
        except OSError:
            return self.snapshot
        if signature != self.signature:
            self.signature = signature
            try:
                self.snapshot = load_features(self.path)
            except (OSError, ValueError):
                logging.getLogger(__name__).warning("Некорректная конфигурация функций. Сохранён предыдущий снимок.")
        return self.snapshot
