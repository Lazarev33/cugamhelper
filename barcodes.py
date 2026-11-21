"""Загрузка каталога и подготовка PNG Code 128 для Telegram."""

from io import BytesIO
import json
from pathlib import Path
import re

from barcode import Code128
from barcode.writer import ImageWriter


DEFAULT_CATALOG = Path(__file__).with_name("barcodes.json")
LOCAL_CATALOG = Path("/etc/tg-number-cleaner/barcodes.json")
BARCODE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,47}")


def render_png(value: str) -> bytes:
    image = BytesIO()
    # 254 dpi: модуль 0,4 мм занимает 4 пикселя, белое поле — 20 модулей.
    Code128(value, writer=ImageWriter(dpi=254)).write(image, {
        "module_width": 0.4,
        "module_height": 24,
        "quiet_zone": 8,
        "font_size": 13,
        "text_distance": 3,
    })
    return image.getvalue()


def load_catalog(local: Path = LOCAL_CATALOG) -> dict[str, bytes]:
    path = local if local.exists() else DEFAULT_CATALOG
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list) or not values:
        raise ValueError(f"{path}: ожидается непустой JSON-массив кодов")
    catalog: dict[str, bytes] = {}
    for value in values:
        if not isinstance(value, str) or not BARCODE_VALUE.fullmatch(value):
            raise ValueError(f"{path}: недопустимое значение штрихкода")
        if value in catalog:
            raise ValueError(f"{path}: повторяющийся код {value}")
        catalog[value] = render_png(value)
    return catalog
