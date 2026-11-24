"""Ограниченное состояние, приём операций и регулирование Bot API."""

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field
import logging
from math import ceil
from time import monotonic

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument, SendMessage, SendPhoto

from settings import Settings

LOG = logging.getLogger(__name__)
CHAT_REQUESTS = (SendMessage, SendPhoto, SendDocument, EditMessageText)


class SendBudgetExceeded(TelegramAPIError):
    """Запрос не отправлен в пределах времени, отведённого ему."""


class Outgoing:
    def __init__(self, settings: Settings, clock=monotonic, sleep=asyncio.sleep):
        self.settings = settings
        self.clock = clock
        self.sleep = sleep
        self.chat_after = OrderedDict()
        self.global_after = self.flood_after = self.ack_after = self.log_after = 0.0
        self.ack_credits = 32.0
        self.ack_tick = clock()
        self.ack_active = 0
        self.allowed = None

    async def reserve(self, method, deadline: float) -> float:
        while True:
            now = self.clock()
            while self.chat_after and next(iter(self.chat_after.values())) <= now:
                self.chat_after.popitem(last=False)
            due = max(self.global_after, self.flood_after, self.chat_after.get(method.chat_id, 0))
            if max(now, due) >= deadline:
                raise SendBudgetExceeded(method=method, message="Время ожидания отправки истекло")
            if due <= now:
                # Между проверкой и записью нет await: слот принадлежит этой отправке.
                self.global_after = now + 1 / self.settings.global_rate
                self.chat_after[method.chat_id] = now + self.settings.chat_interval
                self.chat_after.move_to_end(method.chat_id)
                return deadline - now
            await self.sleep(due - now)

    async def acknowledge(self, request, bot, method):
        now = self.clock()
        self.ack_credits = min(32, self.ack_credits + (now - self.ack_tick) * 50)
        self.ack_tick = now
        if now < self.ack_after or self.ack_credits < 1 or self.ack_active >= 16:
            return False
        self.ack_credits -= 1
        self.ack_active += 1
        deadline = now + 2
        try:
            for attempt in range(2):
                return_in = deadline - self.clock()
                if return_in <= 0:
                    break
                try:
                    return await request(bot, method, timeout=return_in)
                except TelegramRetryAfter as error:
                    now = self.clock()
                    self.ack_after = max(self.ack_after, now + error.retry_after)
                    self.log_flood(error.retry_after, "callback")
                    if attempt or self.ack_after >= deadline:
                        break
                    await self.sleep(self.ack_after - now)
                    if self.ack_after > self.clock():
                        break
            return False
        except TelegramAPIError:
            # Истёкший callback или недоступная сеть не отменяют уже принятую операцию.
            return False
        finally:
            self.ack_active -= 1

    def log_flood(self, delay: int, channel: str) -> None:
        now = self.clock()
        if now >= self.log_after:
            self.log_after = now + 15
            LOG.warning("Telegram 429 (%s), пауза %s сек.", channel, delay)

    async def __call__(self, request, bot, method):
        if isinstance(method, AnswerCallbackQuery):
            return await self.acknowledge(request, bot, method)
        if not isinstance(method, CHAT_REQUESTS):
            return await request(bot, method)

        deadline = self.clock() + self.settings.send_timeout
        for attempt in range(self.settings.retry_attempts + 1):
            remaining = await self.reserve(method, deadline)
            if self.allowed and not self.allowed(method.chat_id):
                raise asyncio.CancelledError
            try:
                return await request(bot, method, timeout=min(8, remaining))
            except TelegramRetryAfter as error:
                # Telegram отклонил именно этот запрос. При 429 область лимита
                # неизвестна, поэтому останавливаем общий поток, кроме callback.
                self.flood_after = max(self.flood_after, self.clock() + error.retry_after)
                self.log_flood(error.retry_after, "отправки")
                if attempt == self.settings.retry_attempts:
                    raise


class Jobs:
    """Принимает ограниченное число работ без ожидания места в очереди."""

    def __init__(self, parallel: int, waiting: int):
        self.parallel = parallel
        self.waiting = waiting
        self.queue = deque()
        self.tasks = set()
        self.running = {}
        self.keys = set()
        self.closing = False
        self.log_after = 0.0

    def submit(self, key, operation) -> bool:
        if self.closing or key in self.keys:
            return False
        if len(self.tasks) >= self.parallel:
            if len(self.queue) >= self.waiting:
                return False
            self.queue.append((key, operation))
        else:
            self.start(key, operation)
        self.keys.add(key)
        return True

    def start(self, key, operation) -> None:
        async def run():
            await operation()

        task = asyncio.create_task(run())
        self.tasks.add(task)
        self.running[key] = task
        task.add_done_callback(lambda completed: self.finished(key, completed))

    def finished(self, key, task) -> None:
        self.running.pop(key, None)
        self.tasks.discard(task)
        self.keys.discard(key)
        if not task.cancelled() and (error := task.exception()):
            now = monotonic()
            if now >= self.log_after:
                self.log_after = now + 15
                LOG.warning("Операция Telegram завершилась ошибкой: %s", type(error).__name__)
        if not self.closing and self.queue:
            self.start(*self.queue.popleft())

    def revoke(self, key):
        if key in self.running:
            self.running[key].cancel()
        else:
            self.queue = deque(pair for pair in self.queue if pair[0]!=key)
            self.keys.discard(key)

    async def close(self) -> None:
        self.closing = True
        self.queue.clear()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.keys.clear()


@dataclass
class UserState:
    expires: float
    after: dict[str, float] = field(default_factory=dict)
    warnings: dict[str, float] = field(default_factory=dict)
    # Последний экран для нескольких сообщений с меню, включая старые фото.
    screens: OrderedDict = field(default_factory=OrderedDict)


class Protection:
    def __init__(self, settings: Settings, clock=monotonic, sleep=asyncio.sleep):
        self.settings = settings
        self.clock = clock
        self.outgoing = Outgoing(settings, clock, sleep)
        self.heavy = Jobs(settings.parallel_operations, settings.queue_size)
        self.light = Jobs(8, 0)
        self.edits = Jobs(8, 0)
        self.users = OrderedDict()
        self.updates = OrderedDict()
        self.scans: set[int] = set()
        self.handlers = {}
        self.log_after = {}

    def note(self, reason: str) -> None:
        now = self.clock()
        if now >= self.log_after.get(reason, 0):
            self.log_after[reason] = now + 15
            LOG.info("Защита Telegram: %s", reason)

    def duplicate(self, update_id: int) -> bool:
        now = self.clock()
        while self.updates and next(iter(self.updates.values())) <= now:
            self.updates.popitem(last=False)
        if update_id in self.updates:
            return True
        if len(self.updates) >= 8192:
            self.updates.popitem(last=False)
        self.updates[update_id] = now + 120
        return False

    def state(self, uid: int) -> UserState | None:
        now = self.clock()
        while self.users and next(iter(self.users.values())).expires <= now:
            self.users.popitem(last=False)
        if uid not in self.users and len(self.users) >= 4096:
            self.note("достигнут предел пользовательских состояний")
            return None
        if uid not in self.users:
            self.users[uid] = UserState(now + 180)
        state = self.users[uid]
        state.expires = now + 180
        self.users.move_to_end(uid)
        return state

    def revoke(self, uid):
        for task in self.handlers.get(uid, ()):
            task.cancel()
        for pool in (self.heavy,self.light,self.edits):
            pool.revoke(uid)
        self.users.pop(uid,None)
        self.scans.discard(uid)

    def busy(self, uid: int) -> bool:
        return uid in self.heavy.keys or uid in self.light.keys

    def remaining(self, state: UserState, kind: str) -> int:
        return max(0, ceil(state.after.get(kind, 0) - self.clock()))

    def warning(self, state: UserState, kind: str, interval: float) -> bool:
        now = self.clock()
        if now < state.warnings.get(kind, 0):
            return False
        state.warnings[kind] = now + interval
        return True

    def limit(self, state: UserState, kind: str, delay: float) -> None:
        state.after[kind] = self.clock() + delay

    async def close(self) -> None:
        for pool in (self.heavy, self.light, self.edits):
            await pool.close()
