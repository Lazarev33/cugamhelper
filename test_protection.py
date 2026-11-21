"""Детерминированные проверки ограничения очереди и отправки."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, SendDocument, SendMessage, SendPhoto

from protection import Jobs, Outgoing, Protection, SendBudgetExceeded
from settings import Settings, load_settings


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


class ProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_and_parallel_work_are_bounded(self):
        pool = Jobs(2, 32)
        self.addAsyncCleanup(pool.close)
        barrier = asyncio.Event()
        for uid in range(34):
            self.assertTrue(pool.submit(uid, barrier.wait))
        self.assertFalse(pool.submit(34, barrier.wait))
        self.assertFalse(pool.submit(1, barrier.wait))
        self.assertEqual((len(pool.tasks), len(pool.queue), len(pool.keys)), (2, 32, 34))
        await pool.close()
        self.assertEqual((len(pool.tasks), len(pool.queue), len(pool.keys)), (0, 0, 0))

    async def test_exception_releases_job_and_starts_waiter(self):
        pool = Jobs(1, 1)
        self.addAsyncCleanup(pool.close)
        started = asyncio.Event()

        async def fail():
            raise RuntimeError("test")

        async def proceed():
            started.set()

        pool.submit(1, fail)
        pool.submit(2, proceed)
        with self.assertLogs("protection", level="WARNING"):
            await asyncio.gather(*pool.tasks, return_exceptions=True)
        await started.wait()
        await asyncio.sleep(0)
        self.assertNotIn(1, pool.keys)
        self.assertNotIn(2, pool.keys)

    async def test_state_and_update_cache_expire_and_stay_bounded(self):
        clock = Clock()
        guard = Protection(Settings(), clock, clock.sleep)
        for uid in range(10_000):
            guard.state(uid)
            self.assertFalse(guard.duplicate(uid))
        self.assertIsNone(guard.state(10000))
        self.assertTrue(guard.duplicate(9999))
        self.assertEqual(len(guard.users), 4096)
        self.assertEqual(len(guard.updates), 8192)
        clock.now += 181
        self.assertIsNotNone(guard.state(10000))
        self.assertFalse(guard.duplicate(9999))
        self.assertEqual((len(guard.users), len(guard.updates)), (1, 1))

    async def test_global_and_chat_spacing_in_parallel(self):
        clock = Clock()
        limiter = Outgoing(Settings(), clock, clock.sleep)
        calls = []

        async def request(bot, method, timeout):
            calls.append((clock(), method.chat_id))

        jobs = [SendMessage(chat_id=1, text="x"), SendPhoto(chat_id=1, photo="img"),
                SendDocument(chat_id=1, document="doc")]
        jobs += [SendMessage(chat_id=chat, text="x") for chat in range(2, 20)]
        await asyncio.gather(*(limiter(request, None, job) for job in jobs))
        self.assertEqual(len(calls), len(jobs))
        for (before, _), (after, _) in zip(calls, calls[1:]):
            self.assertGreaterEqual(after - before + 1e-9, 1 / 20)
        chat_times = [when for when, chat in calls if chat == 1]
        for before, after in zip(chat_times, chat_times[1:]):
            self.assertGreaterEqual(after - before + 1e-9, 1.1)

    async def test_retry_after_pauses_sends_but_not_acknowledgements(self):
        clock = Clock()
        limiter = Outgoing(Settings(), clock, clock.sleep)
        calls = []

        async def request(bot, method, timeout):
            calls.append((clock(), type(method)))
            if len(calls) == 1:
                raise TelegramRetryAfter(method=method, message="429", retry_after=3)
            return True

        with self.assertLogs("protection", level="WARNING"):
            send = asyncio.create_task(limiter(request, None, SendMessage(chat_id=1, text="x")))
            await asyncio.sleep(0)
            # Отдельный канал callback не проверяет flood_after для обычных отправок.
            self.assertTrue(await limiter(request, None, AnswerCallbackQuery(callback_query_id="a")))
            await send
        self.assertEqual([method for _, method in calls], [SendMessage, AnswerCallbackQuery, SendMessage])
        self.assertGreaterEqual(calls[2][0] - calls[0][0], 3)

    async def test_retry_budget_exhaustion_and_unknown_delivery(self):
        clock = Clock()
        limiter = Outgoing(Settings(), clock, clock.sleep)
        send = SendPhoto(chat_id=1, photo="img")
        sent = 0

        async def flood(bot, method, timeout):
            nonlocal sent
            sent += 1
            raise TelegramRetryAfter(method=method, message="429", retry_after=100)

        with self.assertLogs("protection", level="WARNING"):
            with self.assertRaises(SendBudgetExceeded):
                await limiter(flood, None, send)
        self.assertEqual(sent, 1)
        self.assertEqual(limiter.flood_after, 100)
        clock.now = 101

        async def network(bot, method, timeout):
            nonlocal sent
            sent += 1
            raise TelegramNetworkError(method=method, message="network error")

        with self.assertRaises(TelegramNetworkError):
            await limiter(network, None, send)
        self.assertEqual(sent, 2)

    async def test_retry_count_bound(self):
        clock = Clock()
        limiter = Outgoing(Settings(), clock, clock.sleep)
        attempts = []

        async def request(bot, method, timeout):
            attempts.append(clock())
            raise TelegramRetryAfter(method=method, message="429", retry_after=2)

        with self.assertLogs("protection", level="WARNING"):
            with self.assertRaises(TelegramRetryAfter):
                await limiter(request, None, SendPhoto(chat_id=1, photo="img"))
        self.assertEqual(attempts, [0, 2, 4])


class SettingsTests(unittest.TestCase):
    def test_default_and_private_configuration(self):
        with TemporaryDirectory() as work:
            path = Path(work) / "settings.json"
            self.assertEqual(load_settings(path), Settings())
            path.write_text('{"barcode_mode":"document","allowed_user_ids":[42],"queue_size":0}')
            settings = load_settings(path)
            self.assertEqual(settings.allowed_user_ids, {42})
            self.assertEqual(settings.barcode_mode, "document")
            self.assertEqual(settings.queue_size, 0)
            path.write_text('{"allowed_user_ids":[]}')
            self.assertEqual(load_settings(path).allowed_user_ids, set())

    def test_errors_do_not_echo_configuration_values(self):
        with TemporaryDirectory() as work:
            path = Path(work) / "settings.json"
            for content in ('{"unexpected":"sensitive"}', '{"queue_size":true}', '{"barcode_mode":[]}',
                            '{"global_rate":100}', '{"chat_interval":0}', '{"allowed_user_ids":[true]}'):
                path.write_text(content)
                with self.assertRaises(ValueError) as error:
                    load_settings(path)
                self.assertNotIn("sensitive", str(error.exception))
