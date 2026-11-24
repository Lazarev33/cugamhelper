"""Загрузка каталога и подготовка PNG Code 128 для Telegram."""

from io import BytesIO
import json
from pathlib import Path
import re

from barcode import Code128
from barcode.writer import ImageWriter


DEFAULT_CATALOG = Path(__file__).with_name("barcodes.json")
LOCAL_CATALOG = Path("/etc/tg-number-cleaner/barcodes.json")
BARCODE_VALUE = re.compile(r"[ -~]{1,48}")


def validate_value(value: str) -> str:
    if not isinstance(value, str) or not BARCODE_VALUE.fullmatch(value) or not value.strip():
        raise ValueError("Значение Code 128 — от 1 до 48 печатных ASCII-символов.")
    return value


def render_png(value: str) -> bytes:
    validate_value(value)
    image = BytesIO()
    # 508 dpi: модуль 0,4 мм занимает 8 пикселей, белое поле — 20 модулей.
    Code128(value, writer=ImageWriter(dpi=508)).write(image, {
        "module_width": 0.4,
        "module_height": 24,
        "quiet_zone": 8,
        "font_size": 13,
        "text_distance": 6,
    })
    return image.getvalue()


def seed_file(path: Path) -> list[str]:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list) or not values:
        raise ValueError(f"{path}: ожидается непустой JSON-массив кодов")
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not BARCODE_VALUE.fullmatch(value):
            raise ValueError(f"{path}: недопустимое значение штрихкода")
        if value in seen:
            raise ValueError(f"{path}: повторяющийся код")
        if not value.strip():
            raise ValueError(f"{path}: пустое значение штрихкода")
        seen.add(value)
    return values


def load_seed_values(local: Path = LOCAL_CATALOG) -> list[str]:
    defaults = seed_file(DEFAULT_CATALOG)
    if not local.exists():
        return defaults
    return list(dict.fromkeys([*seed_file(local), *defaults]))
