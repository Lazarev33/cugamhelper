"""Настройки Telegram-бота. Файл на VPS находится вне каталога релиза."""

from dataclasses import dataclass
import json
from pathlib import Path

SETTINGS_PATH = Path("/etc/tg-number-cleaner/settings.json")
LIMITS = {
    "data_mib": (1,4), "data_rows": (1,10000), "data_parallel": (1,4), "data_timeout": (2,30), "data_ttl": (30,600),
    "barcode_cooldown": (0.1, 60),
    "clean_cooldown": (0.1, 120),
    "warning_interval": (0.1, 120),
    "chat_interval": (1.1, 10),
    "global_rate": (1, 20),
    "max_input_chars": (1, 65536),
    "max_input_lines": (1, 4000),
    "parallel_operations": (1, 32),
    "queue_size": (0, 32),
    "retry_attempts": (0, 4),
    "send_timeout": (2, 30),
}
INTEGER_FIELDS = {
    "data_mib","data_rows","data_parallel","data_timeout","data_ttl",
    "global_rate", "max_input_chars", "max_input_lines", "parallel_operations",
    "queue_size", "retry_attempts",
}


@dataclass(frozen=True)
class Settings:
    data_mib: int = 2
    data_rows: int = 4000
    data_parallel: int = 2
    data_timeout: int = 12
    data_ttl: int = 600
    barcode_cooldown: float = 2
    clean_cooldown: float = 5
    warning_interval: float = 15
    chat_interval: float = 1.1
    global_rate: int = 20
    max_input_chars: int = 16384
    max_input_lines: int = 1500
    parallel_operations: int = 8
    queue_size: int = 32
    retry_attempts: int = 2
    send_timeout: float = 15
    barcode_mode: str = "photo"
    allowed_user_ids: frozenset[int] | None = None
    owner_telegram_id: int | None = None


def load_settings(path: Path = SETTINGS_PATH) -> Settings:
    try:
        with path.open("rb") as source:
            content = source.read(65537)
    except FileNotFoundError:
        return Settings()
    if len(content) > 65536:
        raise ValueError("файл настроек больше 64 КиБ")
    try:
        values = json.loads(content)
    except (ValueError, RecursionError):
        raise ValueError("ожидается корректный JSON") from None

    if not isinstance(values, dict) or values.keys() - Settings.__dataclass_fields__.keys():
        raise ValueError("ожидается JSON-объект с известными ключами")
    defaults = Settings()
    for name, (lower, upper) in LIMITS.items():
        if name not in values:
            continue
        value = values[name]
        types = (int,) if name in INTEGER_FIELDS else (int, float)
        if type(value) not in types or not lower <= value <= upper:
            raise ValueError(f"поле {name} должно быть числом от {lower} до {upper} нужного типа")
    if values.get("barcode_mode", defaults.barcode_mode) not in ("photo", "document"):
        raise ValueError("поле barcode_mode принимает photo или document")
    owner = values.get("owner_telegram_id")
    if owner is not None and (type(owner) is not int or not 0 < owner < 2**52):
        raise ValueError("owner_telegram_id — положительный Telegram user ID или null")
    allowed = values.get("allowed_user_ids")
    if allowed is not None:
        if not isinstance(allowed, list) or any(
            type(uid) is not int or not 0 < uid < 2**52 for uid in allowed
        ):
            raise ValueError("allowed_user_ids — null или массив положительных Telegram user ID")
        values["allowed_user_ids"] = frozenset(allowed)
    return Settings(**values)
