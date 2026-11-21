"""Локальное декодирование изображений в ограниченном дочернем процессе."""

import asyncio
from dataclasses import dataclass, asdict
from io import BytesIO
import json
import os
from pathlib import Path
import resource
import sys

from PIL import Image, ImageOps
import zxingcpp

MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 8_000_000
MAX_CODES = 16
SCAN_SECONDS = 6
MAX_RESULT_BYTES = 128 * 1024


class ScanError(ValueError):
    pass


@dataclass(frozen=True)
class Decoded:
    format: str
    value: str
    addable: bool


def decode_image(data: bytes) -> list[Decoded]:
    if len(data) > MAX_BYTES:
        raise ScanError("Изображение больше 5 МиБ.")
    try:
        with Image.open(BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as source:
            if source.width * source.height > MAX_PIXELS or getattr(source, "n_frames", 1) != 1:
                raise ScanError("Нужен один кадр до 8 мегапикселей.")
            with ImageOps.exif_transpose(source) as oriented:
                gray = oriented.convert("L")
    except (OSError, Image.DecompressionBombError):
        raise ScanError("Не удалось открыть изображение JPEG, PNG или WebP.") from None

    found = {}

    def find(sample):
        for code in zxingcpp.read_barcodes(sample, text_mode=zxingcpp.TextMode.Plain):
            format_name = str(code.format)
            value = code.text
            normal_128 = code.format == zxingcpp.BarcodeFormat.Code128 and code.symbology_identifier == "]C0"
            if normal_128:
                try:
                    value = code.bytes.decode("ascii")
                except UnicodeDecodeError:
                    normal_128 = False
            if str(code.content_type) in {"Binary", "Mixed"} and not normal_128:
                value = "Байты (hex) " + code.bytes.hex()
            found[(format_name, code.bytes, code.symbology_identifier)] = Decoded(format_name, value, normal_128)
            if len(found) > MAX_CODES:
                raise ScanError("Найдено больше 16 кодов. Сними нужную область крупнее.")

    find(gray)
    # Второй полный проход может найти соседний слабоконтрастный код.
    find(ImageOps.autocontrast(gray))
    if not found:
        # Для неудачных кадров отсекаем шум у краёв и добавляем белое поле.
        width, height = gray.size
        center = gray.crop((width // 20, height // 20, width * 19 // 20, height * 19 // 20))
        find(ImageOps.expand(center, border=40, fill=255))
    return list(found.values())


class BoundedImage(BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_BYTES:
            raise ScanError("Изображение больше 5 МиБ.")
        return super().write(data)


async def scan_local(data: bytes) -> list[Decoded]:
    if len(data) > MAX_BYTES:
        raise ScanError("Изображение больше 5 МиБ.")
    worker = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve()),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env={"LANG": "C.UTF-8", "PATH": os.defpath},
        limit=MAX_RESULT_BYTES,
    )
    try:
        output, _ = await asyncio.wait_for(worker.communicate(data), SCAN_SECONDS)
    except (asyncio.TimeoutError, asyncio.CancelledError) as error:
        if worker.returncode is None:
            worker.kill()
        await worker.wait()
        if isinstance(error, asyncio.CancelledError):
            raise
        raise ScanError("Сканирование заняло слишком много времени. Приблизь код и повтори.") from None
    if worker.returncode:
        raise ScanError("Не удалось обработать изображение. Попробуй снять код крупнее.")
    result = json.loads(output)
    if "error" in result:
        raise ScanError(result["error"])
    return [Decoded(**code) for code in result["codes"]]


async def scan_telegram(bot, file_id: str, size: int | None) -> list[Decoded]:
    if size is not None and size > MAX_BYTES:
        raise ScanError("Изображение больше 5 МиБ.")

    async def download():
        remote = await bot.get_file(file_id, request_timeout=6)
        if not remote.file_path:
            raise ScanError("Telegram не предоставил файл. Отправь его заново.")
        if remote.file_size is not None and remote.file_size > MAX_BYTES:
            raise ScanError("Изображение больше 5 МиБ.")
        with BoundedImage() as target:
            await bot.download_file(remote.file_path, destination=target, timeout=6, seek=False)
            return target.getvalue()

    return await scan_local(await asyncio.wait_for(download(), 10))


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_AS, (384 * 1024**2, 384 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (4, 5))
    try:
        result = {"codes": [asdict(code) for code in decode_image(sys.stdin.buffer.read(MAX_BYTES + 1))]}
    except ScanError as error:
        result = {"error": str(error)}
    encoded = json.dumps(result, ensure_ascii=True)
    if len(encoded) > MAX_RESULT_BYTES:
        encoded = json.dumps({"error": "Слишком много данных в кодах. Сними нужную область крупнее."})
    print(encoded)
