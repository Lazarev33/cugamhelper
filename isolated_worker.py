"""Фиксированный двоичный кадр IPC, один запрос на соединение."""
import asyncio
from contextlib import suppress
from dataclasses import asdict
import json
import os
import struct
import sys

MAX_INPUT = 12 * 1024**2
MAX_OUTPUT = 8 * 1024**2
SOCKET = "/run/cugam-document.sock"
ERROR = "Не удалось обработать файл в изолированном процессе."


def validate_job(data):
    from features import MODES

    if (type(data) is not dict or set(data) != {"items", "mode", "limit", "expected"}
            or not isinstance(data["mode"], str) or data["mode"] not in MODES
            or type(data["limit"]) is not int or not 1 <= data["limit"] <= 10000
            or type(data["items"]) is not list or not 1 <= len(data["items"]) <= 2
            or data["expected"] is not None and
            (type(data["expected"]) is not int or not 1 <= data["expected"] <= 10000)):
        raise ValueError(ERROR)
    for item in data["items"]:
        if (type(item) is not dict or set(item) != {"kind", "opts", "data"}
                or item["kind"] not in ("xlsx", "docx", "txt", "text") or type(item["opts"]) is not dict
                or len(item["opts"]) > 12 or not isinstance(item["data"], str)
                or len(item["data"]) > 6 * 1024**2):
            raise ValueError(ERROR)
    return data


def dispatch(kind, payload):
    from private_process import disable_core_dumps
    from warehouse_worker import process_bytes

    disable_core_dumps()
    if kind == b"H" and payload == b"1":
        return b'{"status":"ok"}'
    if kind == b"D":
        return process_bytes(payload, validator=validate_job)
    if kind == b"I":
        from scanner import decode_image, MAX_BYTES, MAX_RESULT_BYTES
        if not 0 < len(payload) <= MAX_BYTES:
            raise ValueError(ERROR)
        body = json.dumps({"codes": [asdict(code) for code in decode_image(payload)]}, ensure_ascii=True).encode()
        if len(body) > MAX_RESULT_BYTES:
            raise ValueError(ERROR)
        return body
    raise ValueError(ERROR)


def receive(stream):
    header = stream.read(5)
    if len(header) != 5:
        raise ValueError(ERROR)
    size = struct.unpack(">I", header[1:])[0]
    if not 0 < size <= MAX_INPUT:
        raise ValueError(ERROR)
    payload = stream.read(size)
    if len(payload) != size:
        raise ValueError(ERROR)
    return header[:1], payload


def serve(incoming, outgoing):
    try:
        result = dispatch(*receive(incoming))
        if not 0 < len(result) <= MAX_OUTPUT:
            raise ValueError(ERROR)
    except (ValueError, ArithmeticError, RecursionError, OSError, MemoryError):
        result = json.dumps({"error": ERROR}).encode()
    outgoing.write(struct.pack(">I", len(result)) + result)
    outgoing.flush()


async def exchange(kind, payload, timeout, maximum=MAX_OUTPUT):
    if not 0 < len(payload) <= MAX_INPUT:
        raise ValueError(ERROR)

    async def request():
        reader, writer = await asyncio.open_unix_connection(os.environ["CUGAM_WORKER_SOCKET"])
        try:
            writer.write(kind + struct.pack(">I", len(payload)))
            writer.write(payload)
            await writer.drain()
            length = struct.unpack(">I", await reader.readexactly(4))[0]
            if not 0 < length <= maximum:
                raise ValueError(ERROR)
            result = json.loads(await reader.readexactly(length))
            if type(result) is not dict:
                raise ValueError(ERROR)
            return result
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    try:
        return await asyncio.wait_for(request(), timeout)
    except (OSError, asyncio.IncompleteReadError, json.JSONDecodeError, RecursionError, UnicodeError):
        raise ValueError(ERROR) from None


if __name__ == "__main__":
    serve(sys.stdin.buffer, sys.stdout.buffer)
