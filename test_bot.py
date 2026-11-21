"""Проверки меню и запросов, которые aiogram отправляет в Telegram."""

from pathlib import Path
import unittest

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument, SendMessage
from aiogram.types import BufferedInputFile, Message, Update

from barcodes import load_catalog
from bot import dp


CHAT = {"id": 42, "type": "private"}
USER = {"id": 42, "is_bot": False, "first_name": "Сотрудник"}


class TelegramStub(AiohttpSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.not_modified = False
        self.retry_once = False

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, EditMessageText) and self.not_modified:
            raise TelegramBadRequest(method=method, message="message is not modified")
        if isinstance(method, SendMessage) and self.retry_once and method.text == "0001":
            self.retry_once = False
            raise TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=0)
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

    async def feed(self, **payload):
        await dp.feed_update(
            self.bot, Update.model_validate({"update_id": 1, **payload}), catalog=self.catalog,
        )

    async def send(self, text):
        await self.feed(message={"message_id": 2, "date": 1, "chat": CHAT, "from": USER, "text": text})

    async def click(self, data):
        await self.feed(callback_query={
            "id": "1", "chat_instance": "test", "from": USER, "data": data,
            "message": {"message_id": 3, "date": 1, "chat": CHAT, "text": "Меню"},
        })

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
        await self.send("0" * 4096)
        self.assertEqual(len(self.session.requests), 2)

    async def test_invalid_and_oversized_lines_have_no_result(self):
        await self.send("00023-03.11.2026\nошибка\n\n0009-31.02.2026")
        error, = self.session.requests
        self.assertIn("Строка 2\nСтрока 4", error.text)
        self.session.requests.clear()
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
        await self.click("menu:barcodes")
        menu = self.session.requests[-1]
        keyboard = [button for row in menu.reply_markup.inline_keyboard for button in row]
        self.assertEqual([button.text for button in keyboard], [*self.catalog, "Назад", "Главное меню"])
        self.session.not_modified = True
        await self.click("menu:barcodes")
        self.assertIsInstance(self.session.requests[-2], AnswerCallbackQuery)
        self.session.not_modified = False
        for key in ("menu:back", "menu:main", "menu:clean", "menu:help"):
            await self.click(key)
            self.assertIsInstance(self.session.requests[-1], EditMessageText)

    async def test_each_code_and_repeated_selection(self):
        for code in (*self.catalog, next(iter(self.catalog))):
            await self.click(f"barcode:{code}")
            ack, document = self.session.requests[-2:]
            self.assertIsInstance(ack, AnswerCallbackQuery)
            self.assertIsInstance(document, SendDocument)
            self.assertIsInstance(document.document, BufferedInputFile)
            self.assertEqual(document.caption, code)
            self.assertEqual(document.document.filename, f"{code}.png")
            self.assertEqual(document.document.data, self.catalog[code])
        await self.click("barcode:obsolete")
        self.assertTrue(self.session.requests[-1].show_alert)
