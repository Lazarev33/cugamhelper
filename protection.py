"""Ограниченное состояние, приём операций и регулирование Bot API."""

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field
import logging
from math import ceil
from secrets import token_hex
from time import monotonic

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, EditMessageCaption, EditMessageText, SendDocument, SendMessage, SendPhoto

from settings import Settings

LOG = logging.getLogger(__name__)
CHAT_REQUESTS = (SendMessage, SendPhoto, SendDocument, EditMessageText, EditMessageCaption)


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


@dataclass
class WaitingJob:
    operation: object
    long: bool
    timer: asyncio.TimerHandle | None = None


class Jobs:
    """Одна работа на пользователя, ограниченная очередь и резерв для коротких задач."""

    def __init__(self, parallel: int, waiting: int, long_parallel: int | None = None):
        self.parallel = parallel
        self.waiting = waiting
        self.long_parallel = parallel if long_parallel is None else long_parallel
        self.queue = OrderedDict()
        self.tasks = set()
        self.running = {}
        self.keys = set()
        self.tickets = {}
        self.long_running = set()
        self.closing = False
        self.log_after = 0.0

    def has_slot(self, long):
        return len(self.tasks) < self.parallel and (not long or len(self.long_running) < self.long_parallel)

    def submit(self, key, operation, *, long=False, expires=None, on_expire=None) -> bool:
        if self.closing or key in self.keys:
            return False
        immediate = self.has_slot(long)
        if not immediate and len(self.queue) >= self.waiting:
            return False
        self.keys.add(key)
        self.tickets[key] = token_hex(6)
        if immediate:
            self.start(key, operation, long)
        else:
            job = self.queue[key] = WaitingJob(operation, long)
            if expires is not None:
                job.timer = asyncio.get_running_loop().call_at(expires, self.expire, key, on_expire)
        return True

    def expire(self, key, on_expire):
        if key in self.queue:
            self.revoke(key)
            if on_expire:
                on_expire()

    def start(self, key, operation, long) -> None:
        async def run():
            await operation()

        task = asyncio.create_task(run())
        self.tasks.add(task)
        self.running[key] = task
        if long:
            self.long_running.add(key)
        task.add_done_callback(lambda completed: self.finished(key, completed))

    def finished(self, key, task) -> None:
        self.running.pop(key, None)
        self.tasks.discard(task)
        self.long_running.discard(key)
        self.keys.discard(key)
        self.tickets.pop(key, None)
        if not task.cancelled() and (error := task.exception()):
            now = monotonic()
            if now >= self.log_after:
                self.log_after = now + 15
                LOG.warning("Операция Telegram завершилась ошибкой: %s", type(error).__name__)
        if self.closing:
            return
        for waiting_key, job in self.queue.items():
            if self.has_slot(job.long):
                del self.queue[waiting_key]
                if job.timer:
                    job.timer.cancel()
                self.start(waiting_key, job.operation, job.long)
                break

    def ticket(self, key):
        return self.tickets.get(key)

    def revoke(self, key):
        if self.tickets.pop(key, None) is None:
            return
        if key in self.running:
            self.running[key].cancel()
        else:
            job = self.queue.pop(key, None)
            if job and job.timer:
                job.timer.cancel()
            self.keys.discard(key)

    async def close(self) -> None:
        self.closing = True
        for key in tuple(self.keys):
            self.revoke(key)
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.keys.clear()


@dataclass
class UserState:
    expires: float
    after: dict[str, float] = field(default_factory=dict)
    warnings: dict[str, float] = field(default_factory=dict)
    # Последний экран для нескольких сообщений с меню, включая старые фото.
    screens: OrderedDict = field(default_factory=OrderedDict)
    repeats: OrderedDict = field(default_factory=OrderedDict)
    staff_views: OrderedDict = field(default_factory=OrderedDict)
    service_at: float = 0
    service_credits: float = 4


def remember(items, value):
    token = token_hex(6)
    items[token] = value
    if len(items) > 8:
        items.popitem(last=False)
    return token


class Protection:
    def __init__(self, settings: Settings, clock=monotonic, sleep=asyncio.sleep):
        self.settings = settings
        self.clock = clock
        self.outgoing = Outgoing(settings, clock, sleep)
        long_parallel = min(settings.data_parallel, max(1, settings.parallel_operations - 1))
        self.heavy = Jobs(settings.parallel_operations, settings.queue_size, long_parallel)
        self.light = Jobs(8, 0)
        self.edits = Jobs(8, 0)
        self.priority = Jobs(2, 8)
        self.control = Jobs(2, 8)
        self.pools = (self.heavy, self.light, self.edits, self.priority, self.control)
        self.visitor_writes = deque()
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
        for pool in self.pools:
            pool.revoke(uid)
        self.users.pop(uid,None)
        self.scans.discard(uid)

    def busy(self, uid: int) -> bool:
        return any(uid in pool.keys for pool in (self.heavy, self.light, self.priority, self.control))

    def service(self, uid, operation, *, priority=False, control=False, essential=False):
        state = self.state(uid)
        if state is None:
            return False
        if essential or control:
            pool = self.control
            if essential and uid in pool.keys:
                pool = self.light
        else:
            pool = self.priority if priority else self.light
        if not (essential or control) and (uid in self.light.keys or uid in self.priority.keys):
            return False
        now = self.clock()
        state.service_credits = min(4, state.service_credits + max(0, now - state.service_at))
        state.service_at = now
        if state.service_credits < 1 and not essential:
            return False
        if pool.submit(uid, operation):
            if not essential:
                state.service_credits -= 1
            return True
        return False

    def remember_visitor(self, repo, user):
        now = self.clock()
        while self.visitor_writes and now - self.visitor_writes[0] >= 60:
            self.visitor_writes.popleft()
        if len(self.visitor_writes) < 12 and repo.remember_start(user.id, user.full_name, user.username):
            self.visitor_writes.append(now)

    async def cancel_operation(self, uid, state):
        # Отзыв задачи и сброс диалога происходят до подтверждения в Telegram.
        self.heavy.revoke(uid)
        self.scans.discard(uid)
        for pool in (self.light, self.edits, self.priority):
            pool.revoke(uid)
        await state.clear()

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
        for pool in self.pools:
            await pool.close()
