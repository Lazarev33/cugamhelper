"""Рабочие результаты удерживаются только в ограниченной очереди в памяти."""

import asyncio
import sqlite3
from collections import OrderedDict, deque
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic

import aiohttp
from aiohttp import web
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter

from infrastructure import InfraError, SOCKET
from infra_features import common_features
from protection import SendBudgetExceeded, send_permission

MAX_BODY = 512 * 1024
MAX_PART = 4000
TTL = 120
STATUS_TTL = 600


def split_lines(lines):
    parts, current, size = [], [], 0
    for line in lines:
        length = len(line.encode("utf-16-le")) // 2
        if length > MAX_PART:
            raise InfraError(422, "line_limit")
        if current and size + 1 + length > MAX_PART:
            parts.append("\n".join(current))
            current, size = [], 0
        size += length + bool(current)
        current.append(line)
    if current:
        parts.append("\n".join(current))
    return parts


@dataclass
class Export:
    session: str
    uid: int
    fingerprint: str
    parts: list
    total: int
    deadline: float
    expires: float
    status: str = "queued"
    confirmed: int = 0
    finished: bool = False

    def view(self, request_id):
        return {"request_id": request_id, "status": self.status, "confirmed_parts": self.confirmed,
                "total_parts": self.total}


class Exports:
    def __init__(self, authorize, transport, clock=monotonic, metrics=None):
        self.authorize, self.transport, self.clock = authorize, transport, clock
        self.records, self.pending, self.after = {}, deque(), OrderedDict()
        self.event = asyncio.Event()
        self.metrics = metrics

    def submit(self, identity, key, parts):
        now = self.clock()
        record_key = identity.device, key
        self.sweep()
        fingerprint = sha256("\0".join(parts).encode()).hexdigest()
        if current := self.records.get(record_key):
            if current.fingerprint != fingerprint:
                raise InfraError(409, "request_conflict")
            return current
        if len(parts) > 36:
            raise InfraError(422, "part_limit")
        if now < self.after.get(identity.uid, 0):
            raise InfraError(429, "export_frequency")
        active = [r for r in self.records.values() if not r.finished]
        if any(r.uid == identity.uid for r in active):
            raise InfraError(429, "export_in_progress")
        if len(active) >= 8 or len(self.records) >= 512:
            raise InfraError(429, "export_capacity")
        record = Export(identity.id, identity.uid, fingerprint, parts, len(parts), now + TTL, now + STATUS_TTL)
        self.after[identity.uid] = now + 8
        self.records[record_key] = record
        self.pending.append(record_key)
        self.event.set()
        return record

    def get(self, identity, key):
        self.sweep()
        record = self.records.get((identity.device, key))
        if not record:
            raise InfraError(404, "export_not_found")
        return record

    def sweep(self):
        now = self.clock()
        while self.after and next(iter(self.after.values())) <= now:
            self.after.popitem(last=False)
        for key, record in list(self.records.items()):
            if record.finished and record.expires <= now:
                del self.records[key]
            if not record.finished and record.status == "queued":
                if record.deadline <= now:
                    self.finish(record, "expired")
                elif not self.authorize(record.session):
                    self.finish(record, "cancelled")
        self.pending = deque(key for key in self.pending if not self.records[key].finished)

    def finish(self, record, status):
        if self.metrics and not record.finished:
            self.metrics.add("delivery_" + status)
        record.parts.clear()
        record.finished = True
        record.status = status

    async def drain(self):
        self.sweep()
        if not self.pending:
            return
        record = self.records[self.pending.popleft()]
        status = "cancelled"
        try:
            record.status = "sending"
            while record.parts:
                remaining = record.deadline - self.clock()
                if not self.authorize(record.session):
                    status = "partially_delivered" if record.confirmed else "cancelled"
                    break
                if remaining <= 0:
                    status = "partially_delivered" if record.confirmed else "expired"
                    break
                try:
                    result = await asyncio.wait_for(self.transport(record.session, record.parts[0]), remaining)
                except (TimeoutError, asyncio.TimeoutError, OSError, ValueError, aiohttp.ClientError):
                    result = "delivery_unknown"
                if result != "delivered":
                    status = ("partially_delivered" if record.confirmed and result != "delivery_unknown" else result)
                    break
                record.parts.pop(0)
                record.confirmed += 1
                if self.metrics:
                    self.metrics.add("delivery_confirmed")
            else:
                status = "delivered"
        finally:
            self.finish(record, status)

    async def worker(self):
        while True:
            await self.drain()
            if self.pending:
                continue
            self.event.clear()
            try:
                await asyncio.wait_for(self.event.wait(), 0.5)
            except asyncio.TimeoutError:
                pass


def permitted(service, session_id, policy=None):
    try:
        with service.repository() as repo:
            identity = service.by_session(repo, session_id)
            settings = service.policy(repo)
            if (settings["telegram_export"] and settings["android_export"] and settings["android_nsu"]
                    and common_features(repo.db, getattr(policy, "mapping", policy).legacy.current).enabled("format_text")):
                return identity
    except (InfraError, OSError, sqlite3.Error):
        pass
    return None


async def dispatch(socket, session_id, text):
    connector = aiohttp.UnixConnector(path=str(socket))
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=20)) as client:
        try:
            async with client.post("http://localhost/send", json={"session": session_id, "text": text}) as response:
                if response.status != 200:
                    return "failed"
                state = (await response.json()).get("status")
                return state if state in {"delivered", "failed", "delivery_unknown", "cancelled"} else "delivery_unknown"
        except aiohttp.ClientConnectorError:
            return "failed"


async def deliver(service, bot, features, sid, value, identity=None):
    identity = identity or permitted(service, sid, features)
    if not identity:
        return "cancelled"
    context = send_permission.set(lambda: bool(permitted(service, sid, features)))
    try:
        await bot.send_message(identity.uid, value, parse_mode=None, disable_web_page_preview=True)
    except asyncio.CancelledError:
        return "cancelled"
    except (TelegramNetworkError, asyncio.TimeoutError):
        return "delivery_unknown"
    except (TelegramRetryAfter, SendBudgetExceeded):
        return "failed"
    except TelegramAPIError as error:
        return "delivery_unknown" if type(error).__name__ in {"TelegramServerError", "ClientDecodeError"} else "failed"
    finally:
        send_permission.reset(context)
    return "delivered"


async def telegram_check(session_id, socket=SOCKET):
    connector = aiohttp.UnixConnector(path=str(socket))
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=7)) as client:
            async with client.post("http://localhost/diagnose", json={"session": session_id}) as response:
                if response.status == 200:
                    return (await response.json())["status"]
    except (OSError, asyncio.TimeoutError, aiohttp.ClientError):
        pass
    return "unavailable"


async def listen(service, bot, features, socket=SOCKET):
    if not socket.parent.is_dir():
        return None
    reserved = False

    async def send(request):
        nonlocal reserved
        if reserved:
            return web.json_response({"status": "failed"})
        reserved = True
        try:
            try:
                payload = await asyncio.wait_for(request.json(), 2)
                sid, value = payload["session"], payload["text"]
                if not isinstance(sid, str) or len(sid) != 24 or not isinstance(value, str) or not 0 < len(value) <= MAX_PART:
                    raise ValueError
            except (KeyError, TypeError, ValueError, asyncio.TimeoutError):
                return web.json_response({"status": "failed"})
            identity = permitted(service, sid, features)
            if not identity:
                return web.json_response({"status": "cancelled"})
            return web.json_response({"status": await deliver(service, bot, features, sid, value, identity)})
        finally:
            reserved = False

    diagnostic = asyncio.Lock()

    async def diagnose(request):
        try:
            data = await asyncio.wait_for(request.json(), 2)
            with service.repository() as repo:
                service.by_session(repo, data["session"], "web")
        except (InfraError, KeyError, TypeError, ValueError, asyncio.TimeoutError):
            return web.json_response({"status": "unavailable"}, status=403)
        if diagnostic.locked():
            return web.json_response({"status": "busy"})
        async with diagnostic:
            try:
                await bot.get_me(request_timeout=4)
            except (TelegramAPIError, asyncio.TimeoutError):
                return web.json_response({"status": "failed"})
            return web.json_response({"status": "ok"})

    application = web.Application(client_max_size=40 * 1024)
    application.router.add_post("/send", send)
    application.router.add_post("/diagnose", diagnose)
    runner = web.AppRunner(application, access_log=None, handler_cancellation=True, shutdown_timeout=2)
    await runner.setup()
    try:
        socket.unlink(missing_ok=True)
        await web.UnixSite(runner, str(socket)).start()
        socket.chmod(0o600)
    except BaseException:
        await runner.cleanup()
        raise
    return runner
