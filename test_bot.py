"""Проверки меню и запросов, которые aiogram отправляет в Telegram."""

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import unittest

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument, SendMessage, SendPhoto
from aiogram.types import BufferedInputFile, Message, Update

from barcodes import load_catalog
from bot import dp
from protection import Protection
from settings import Settings
from test_protection import Clock


CHAT = {"id": 42, "type": "private"}
USER = {"id": 42, "is_bot": False, "first_name": "Сотрудник"}


class TelegramStub(AiohttpSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.not_modified = False
        self.retry_once = False
        self.block_report = None
        self.block_photo = None

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, SendMessage) and method.text.startswith("Обработано:") and self.block_report:
            await self.block_report.wait()
        if isinstance(method, SendPhoto) and self.block_photo:
            await self.block_photo.wait()
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, EditMessageText) and self.not_modified:
            raise TelegramBadRequest(method=method, message=self.not_modified)
        if isinstance(method, SendMessage) and self.retry_once and method.text == "0001":
            self.retry_once = False
            raise TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=2)
        return Message.model_validate({
            "message_id": 99, "date": 1, "chat": CHAT, "text": getattr(method, "text", None),
        }, context={"bot": bot})


class BotTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog(Path(__file__).with_name("no-test-catalog.json"))

    async def asyncSetUp(self):
        self.session = TelegramStub()
        self.bot = Bot(token="123456:unit_test_token", session=self.session)
        self.addAsyncCleanup(self.bot.session.close)
        self.clock = Clock()
        self.update_id = 0
        self.configure(Settings())

    def configure(self, settings):
        self.guard = Protection(settings, self.clock, self.clock.sleep)
        self.addAsyncCleanup(self.guard.close)
        self.session.middleware._middlewares.clear()
        self.session.middleware.register(self.guard.outgoing)

    async def settle(self):
        pools = (self.guard.heavy, self.guard.light, self.guard.edits)
        while tasks := set().union(*(pool.tasks for pool in pools)):
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)

    async def feed(self, duplicate=False, settle=True, **payload):
        if not duplicate:
            self.update_id += 1
        await dp.feed_update(
            self.bot, Update.model_validate({"update_id": self.update_id, **payload}),
            catalog=self.catalog, guard=self.guard,
        )
        if settle:
            await self.settle()

    async def send(self, text, uid=42, chat_type="private", **kwargs):
        await self.feed(message={
            "message_id": 2, "date": 1, "chat": {"id": uid, "type": chat_type},
            "from": {**USER, "id": uid}, "text": text,
        }, **kwargs)

    async def click(self, data, uid=42, chat_type="private", date=1, text="Меню", **kwargs):
        message = {"message_id": 3, "date": date, "chat": {"id": uid, "type": chat_type}}
        if date != 0:
            message["text"] = text
        await self.feed(callback_query={
            "id": str(self.update_id), "chat_instance": "test", "from": {**USER, "id": uid}, "data": data,
            "message": message,
        }, **kwargs)

    def assert_plain(self, message):
        self.assertIsInstance(message, SendMessage)
        self.assertIsNone(message.parse_mode)
        self.assertIsNone(message.entities)
        self.assertIsNone(message.reply_markup)
        form = self.session.build_form_data(self.bot, message)
        fields = {params["name"] for params, _, _ in form._fields}
        self.assertTrue({"parse_mode", "entities", "reply_markup"}.isdisjoint(fields))

    async def test_exact_copy_of_100_ids(self):
        ids = ["00000001", "12345678", "00000001", "00001234"] * 25
        await self.send("\n".join(f"{value}-03.11.2026" for value in ids))
        report, result = self.session.requests
        self.assertEqual(report.text, "Обработано: 100\nОшибок: 0\nПотерь: 0")
        self.assertEqual(result.text, "\n".join(ids))
        self.assert_plain(result)

    async def test_large_result_and_telegram_limit(self):
        ids = ["123456789", "000000001", "123456789"] * 400
        await self.send("\n".join(ids))
        report, *results = self.session.requests
        self.assertEqual(report.text.splitlines()[-1], f"Частей: {len(results)}")
        self.assertGreater(len(results), 1)
        for result in results:
            self.assert_plain(result)
            self.assertLessEqual(len(result.text), 4096)
        self.assertEqual("\n".join(result.text for result in results).splitlines(), ids)
        self.session.requests.clear()
        self.clock.now += 5
        await self.send("0" * 4096)
        self.assertEqual(len(self.session.requests), 2)

    async def test_invalid_and_oversized_lines_have_no_result(self):
        await self.send("00023-03.11.2026\nошибка\n\n0009-31.02.2026")
        error, = self.session.requests
        self.assertIn("Строка 2\nСтрока 4", error.text)
        self.session.requests.clear()
        self.clock.now += 5
        await self.send("0001\n" + "0" * 4097)
        error, = self.session.requests
        self.assertIn("4096", error.text)

    async def test_rate_limit_retries_only_rejected_message(self):
        self.session.retry_once = True
        await self.send("0001")
        self.assertEqual([req.text for req in self.session.requests][1:], ["0001", "0001"])

    async def test_menu_and_repeated_navigation(self):
        for command in ("/start", "/menu"):
            await self.send(command)
            request = self.session.requests[-1]
            self.assertIn("Помощник 2 смены", request.text)
            self.assertEqual([button.text for row in request.reply_markup.inline_keyboard for button in row],
                             ["ШК", "Очистка номеров", "Помощь"])
            self.clock.now += 1.1
        await self.click("menu:barcodes")
        menu = self.session.requests[-1]
        keyboard = [button for row in menu.reply_markup.inline_keyboard for button in row]
        self.assertEqual([button.text for button in keyboard], [*self.catalog, "Назад", "Главное меню"])
        await self.click("menu:barcodes")
        self.assertIsInstance(self.session.requests[-1], AnswerCallbackQuery)
        for key in ("menu:back", "menu:clean", "menu:help", "menu:main"):
            await self.click(key)
            self.assertIsInstance(self.session.requests[-1], EditMessageText)

    async def test_each_code_uses_photo_by_default(self):
        for code in self.catalog:
            await self.click(f"barcode:{code}")
            ack, photo = self.session.requests[-2:]
            self.assertIsInstance(ack, AnswerCallbackQuery)
            self.assertIsInstance(photo, SendPhoto)
            self.assertIsInstance(photo.photo, BufferedInputFile)
            self.assertEqual(photo.caption, code)
            self.assertEqual(photo.photo.filename, f"{code}.png")
            self.assertEqual(photo.photo.data, self.catalog[code])
            self.assertIsNotNone(photo.reply_markup)
            self.clock.now += 2
        await self.click("menu:back", text=None)
        self.assertIsInstance(self.session.requests[-1], SendMessage)

    async def test_document_has_same_limits_and_original_png(self):
        self.configure(replace(Settings(), barcode_mode="document"))
        code = next(iter(self.catalog))
        await self.click(f"barcode:{code}")
        result = self.session.requests[-1]
        self.assertIsInstance(result, SendDocument)
        self.assertEqual(result.document.data, self.catalog[code])
        self.assertEqual(result.caption, code)
        await self.click(f"barcode:{code}")
        self.assertIsInstance(self.session.requests[-1], AnswerCallbackQuery)
        self.assertIn("Не спамь!", self.session.requests[-1].text)

    async def test_barcode_cooldown_shared_across_buttons_warns_once(self):
        first, second, *_ = self.catalog
        await self.click(f"barcode:{first}")
        self.clock.now += .5
        for _ in range(20):
            await self.click(f"barcode:{second}")
        acks = [request for request in self.session.requests if isinstance(request, AnswerCallbackQuery)]
        self.assertEqual([ack.text for ack in acks if ack.text], ["Не спамь! Подожди 2 сек."])
        self.assertFalse(acks[1].show_alert)
        self.assertEqual(sum(isinstance(req, SendPhoto) for req in self.session.requests), 1)
        await self.click("menu:barcodes")
        self.assertIsInstance(self.session.requests[-1], EditMessageText)
        self.clock.now = 2
        await self.click(f"barcode:{second}")
        self.assertIsInstance(self.session.requests[-1], SendPhoto)

    async def test_busy_cleaning_no_parallel_and_independent_users(self):
        blocker = asyncio.Event()
        self.session.block_report = blocker
        await self.send("0001", settle=False)
        await asyncio.sleep(0)
        self.clock.now = 6
        await self.send("0002", settle=False)
        await self.send("0003", uid=43, settle=False)
        await asyncio.sleep(0)
        self.assertEqual(self.guard.heavy.keys, {42, 43})
        self.assertEqual(sum(isinstance(req, SendMessage) for req in self.session.requests), 2)
        blocker.set()
        await self.settle()
        self.assertEqual([r.text for r in self.session.requests if r.text.isdigit()], ["0001", "0003"])

    async def test_photo_stays_exclusive_even_after_cooldown_expires(self):
        first, second, *_ = self.catalog
        self.session.block_photo = asyncio.Event()
        await self.click(f"barcode:{first}", settle=False)
        await asyncio.sleep(0)
        self.clock.now = 3
        await self.click(f"barcode:{second}", settle=False)
        self.assertEqual(sum(isinstance(req, SendPhoto) for req in self.session.requests), 1)
        self.session.block_photo.set()
        await self.settle()
        await self.click(f"barcode:{second}")
        self.assertEqual(self.session.requests[-1].caption, second)

    async def test_clean_cooldown_and_warning_interval(self):
        await self.send("0001")
        await self.send("0002")
        self.assertIn("Слишком частые запросы", self.session.requests[-1].text)
        count = len(self.session.requests)
        await self.send("0003")
        self.assertEqual(len(self.session.requests), count)
        self.clock.now = 5
        await self.send("0004")
        self.assertEqual(self.session.requests[-1].text, "0004")
        await self.send("0005")
        self.assertEqual(len(self.session.requests), count + 2)

    async def test_input_caps_checked_before_parser(self):
        self.configure(replace(Settings(), max_input_chars=10, max_input_lines=3))
        with patch("bot.clean_numbers") as parse:
            await self.send("0" * 11)
            self.assertIn("слишком большой", self.session.requests[-1].text)
            self.clock.now += 5
            await self.send("1\n2\n3\n4")
            self.assertIn("слишком большой", self.session.requests[-1].text)
        parse.assert_not_called()

    async def test_groups_allowlist_and_unexpected_messages(self):
        first = next(iter(self.catalog))
        await self.send("1", chat_type="supergroup")
        await self.click(f"barcode:{first}", chat_type="group")
        self.assertTrue(all(isinstance(req, AnswerCallbackQuery) for req in self.session.requests))
        self.configure(replace(Settings(), allowed_user_ids=frozenset({43})))
        self.session.requests.clear()
        await self.send("1")
        await self.feed(message={"message_id": 9, "date": 1, "chat": {"id": 43, "type": "private"},
                                 "from": {**USER, "id": 43}})
        self.assertEqual(self.session.requests, [])
        await self.send("009", uid=43)
        self.assertEqual(self.session.requests[-1].text, "009")

    async def test_stale_unknown_and_duplicate_updates(self):
        await self.click("barcode:obsolete")
        self.assertIn("/menu", self.session.requests[-1].text)
        await self.click("invented")
        self.assertIsInstance(self.session.requests[-1], AnswerCallbackQuery)
        await self.click("menu:main", date=0, uid=43)
        self.assertIn("/menu", self.session.requests[-1].text)
        await self.send("007")
        count = len(self.session.requests)
        self.clock.now += 5
        await self.send("007", duplicate=True)
        self.assertEqual(len(self.session.requests), count)

    async def test_edit_rejected_falls_back_to_text_menu_once(self):
        self.session.not_modified = "message can't be edited"
        await self.click("menu:clean")
        self.assertIsInstance(self.session.requests[-1], SendMessage)
        await self.click("menu:clean")
        self.assertIsInstance(self.session.requests[-1], AnswerCallbackQuery)

    async def test_no_cooldown_after_rejection_due_to_full_queue(self):
        first = next(iter(self.catalog))
        self.configure(replace(Settings(), parallel_operations=1, queue_size=0))
        gate = asyncio.Event()
        self.guard.heavy.submit(43, gate.wait)
        await self.click(f"barcode:{first}", settle=False)
        self.assertIn("Бот занят", self.session.requests[-1].text)
        gate.set()
        await self.settle()
        await self.click(f"barcode:{first}")
        self.assertIsInstance(self.session.requests[-1], SendPhoto)
