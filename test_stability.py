"""Регрессии v2.7.5. Используются тестовая сессия Telegram и локальные документы."""

import asyncio
from base64 import b64decode
from dataclasses import replace
from io import BytesIO
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import EditMessageText, SendDocument, SendMessage, SendPhoto
from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill

from bot import dp
from settings import Settings
from test_bot import TelegramCase, USER
from test_warehouse import item, saved
from warehouse_files import process


class ExcelRecords(TestCase):
    def workbook(self, rows):
        book = Workbook()
        for row in rows:
            book.active.append(row)
        return book

    def annotate(self, cell, name):
        cell.comment = Comment(name, "Тест")
        cell.hyperlink = "https://example.com/" + name
        cell.hyperlink.tooltip = name
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="12AB34")

    def check(self, cell, value, name, data_type):
        self.assertEqual(cell.value, value)
        self.assertEqual(cell.data_type, data_type)
        self.assertEqual((cell.comment.text, cell.comment.author), (name, "Тест"))
        self.assertEqual((cell.hyperlink.target, cell.hyperlink.tooltip, cell.hyperlink.ref),
                         ("https://example.com/" + name, name, cell.coordinate))
        self.assertTrue(cell.font.bold)
        self.assertEqual(cell.fill.fgColor.rgb, "0012AB34")

    def process(self, book, mode, **options):
        result = process({"mode": mode, "items": [item("xlsx", saved(book), **options)], "limit": 20})
        output = load_workbook(BytesIO(b64decode(result["outputs"][0]["data"])))
        self.assertIn("Потерь: 0", result["report"])
        return output.active

    def test_comments_hyperlinks_and_formats_move_with_sort_group_and_unique(self):
        for mode in ("asc", "dups", "unique"):
            with self.subTest(mode=mode):
                book = self.workbook([["Код", "Вес"], ["B", 22], ["A", 33], ["B", 44]])
                for row, name in [(2, "first-B"), (3, "first-A"), (4, "second-B")]:
                    self.annotate(book.active.cell(row, 1), name)
                    self.annotate(book.active.cell(row, 2), name + "-weight")
                    book.active.cell(row, 2).number_format = "0.00"
                result = self.process(book, mode, a="A", header=1)
                self.check(result["A2"], "A", "first-A", "s")
                self.check(result["B2"], 33, "first-A-weight", "n")
                self.assertEqual(result["B2"].number_format, "0.00")
                self.check(result["A3"], "B", "first-B", "s")
                if mode == "unique":
                    self.assertIsNone(result["A4"].value)
                    self.assertIsNone(result["A4"].comment)
                    self.assertIsNone(result["B4"].hyperlink)
                else:
                    self.check(result["A4"], "B", "second-B", "s")

    def test_alignment_transports_related_attributes_and_preserves_reference(self):
        book = self.workbook([["A", "B", 4], ["B", "A", 5], ["D", "C", 6]])
        self.annotate(book.active["A1"], "reference-A")
        for row, name in [(1, "B"), (2, "A"), (3, "C")]:
            self.annotate(book.active.cell(row, 2), name)
            self.annotate(book.active.cell(row, 3), name + "-weight")
        result = self.process(book, "align", a="A", b="B", link="C")
        self.check(result["A1"], "A", "reference-A", "s")
        self.check(result["B1"], "A", "A", "s")
        self.check(result["C1"], 5, "A-weight", "n")
        self.check(result["B2"], "B", "B", "s")
        self.assertIsNone(result["B3"].value)
        self.assertIsNone(result["B3"].hyperlink)
        self.assertIsNone(result["C3"].comment)
        self.check(result["B4"], "C", "C", "s")
        self.check(result["C4"], 6, "C-weight", "n")

    def test_sensitive_office_links_are_still_rejected(self):
        book = self.workbook([["A"]])
        book.active["A1"].hyperlink = "file:///example/warehouse.txt"
        with self.assertRaisesRegex(ValueError, "внешние ссылки"):
            self.process(book, "asc")


class StabilityTelegram(TelegramCase):
    def buttons(self, method=None):
        request = method or self.session.requests[-1]
        return {button.text: button.callback_data for row in request.reply_markup.inline_keyboard for button in row}

    def ctx(self, uid=42):
        return dp.fsm.get_context(bot=self.bot, chat_id=uid, user_id=uid)

    async def document(self, uid=42, **kwargs):
        await self.feed(message={"message_id": 6, "date": 1, "chat": {"id": uid, "type": "private"},
                                 "from": {**USER, "id": uid}, "document": {
                                     "file_id": "mock", "file_unique_id": "unique", "file_name": "stock.xlsx",
                                 }}, **kwargs)

    async def test_stale_cancel_does_not_abort_new_dialog(self):
        await self.send("/scan")
        stale = self.buttons()["Отмена"]
        await self.click("data:asc")
        await self.click(stale)
        self.assertIn("устарела", self.session.requests[-1].text)
        await self.send("B\nA")
        self.assertEqual(self.session.requests[-1].text, "A\nB")

    async def test_cancel_during_processing_releases_state_and_resources(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        async def worker(request, timeout):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        with patch("warehouse_bot.worker", new=worker):
            await self.click("data:asc")
            await self.send("B\nA", settle=False)
            await asyncio.wait_for(started.wait(), 1)
            await self.send("/cancel")
        self.assertTrue(stopped.is_set())
        self.assertIsNone(await self.ctx().get_state())
        self.assertNotIn(42, self.guard.heavy.keys)
        self.assertIn("отменено", self.session.requests[-1].text)
        self.assertFalse(any(getattr(m, "text", None) == "A\nB" for m in self.session.requests))

    async def test_cancel_after_report_discards_unsent_results(self):
        original = self.session.make_request
        part_started = asyncio.Event()

        async def transport(bot, method, timeout=None):
            if isinstance(method, SendMessage) and method.text == "A\nB":
                part_started.set()
                await asyncio.Future()
            return await original(bot, method, timeout)

        with patch.object(self.session, "make_request", new=transport):
            await self.click("data:check")
            await self.send("B\nA\n\nA\nB", settle=False)
            await asyncio.wait_for(part_started.wait(), 2)
            await self.send("/cancel")
        self.assertTrue(any("Сверка завершена" in (getattr(x, "text", "") or "") for x in self.session.requests))
        self.assertFalse(any(getattr(m, "text", None) == "A\nB" for m in self.session.requests))

    async def test_main_menu_offers_continue_or_cancel(self):
        started = asyncio.Event()

        async def worker(request, timeout):
            started.set()
            await asyncio.Future()

        with patch("warehouse_bot.worker", new=worker):
            await self.click("data:asc")
            await self.send("B\nA", settle=False)
            await asyncio.wait_for(started.wait(), 1)
            await self.send("/menu", settle=False)
            await asyncio.gather(*self.guard.control.tasks)
            actions = self.buttons()
            await self.click(actions["Продолжить операцию"], settle=False)
            self.assertIn(42, self.guard.heavy.keys)
            self.assertEqual((await self.ctx().get_data())["mode"], "asc")
            await self.click("menu:main", settle=False)
            await asyncio.gather(*self.guard.control.tasks)
            await self.click(self.buttons()["Отменить и открыть меню"])
        self.assertIsNone(await self.ctx().get_state())
        self.assertIn("Помощник 2 смены", self.session.requests[-1].text)

    async def test_rejected_result_updates_report_and_never_retries(self):
        original = self.session.make_request
        sent = []
        fallback = {"report": "Сортировка завершена.", "outputs": [{"kind": "xlsx", "data": "QUJD"}]}

        async def transport(bot, method, timeout=None):
            if isinstance(method, SendDocument):
                sent.append(method)
                raise TelegramBadRequest(method=method, message="Документ не принят")
            return await original(bot, method, timeout)

        with patch("warehouse_bot.worker", return_value=fallback), patch.object(self.session, "make_request", new=transport):
            await self.click("data:asc")
            await self.send("B\nA")
        self.assertEqual(len(sent), 1)
        self.assertIsInstance(self.session.requests[-1], EditMessageText)
        self.assertIn("Подтверждено частей результата: 0 из 1", self.session.requests[-1].text)
        self.assertNotIn("неизвестен", self.session.requests[-1].text)

    async def test_partial_delivery_after_network_failure_is_reported(self):
        original = self.session.make_request
        attempts = []

        async def transport(bot, method, timeout=None):
            if isinstance(method, SendMessage) and method.text == "A\nC":
                attempts.append(method)
                raise TelegramNetworkError(method=method, message="Тестовая сеть")
            return await original(bot, method, timeout)

        with patch.object(self.session, "make_request", new=transport):
            await self.click("data:check")
            await self.send("A\nB\n\nA\nC")
        self.assertEqual(len(attempts), 1)
        self.assertTrue(any(getattr(m, "text", None) == "A\nB" for m in self.session.requests))
        self.assertIn("Статус последней отправки неизвестен", self.session.requests[-1].text)
        self.assertIn("частей результата: 1 из 2", self.session.requests[-1].text)

    async def test_repeat_keeps_different_reference_options_without_work_content(self):
        first_opts = ["лист 2", "столбцы A B C,D", "заголовок 1", "кодировка 1251"]
        worker = AsyncMock(return_value={"report": "Сверка завершена.", "outputs": [{"kind": "text", "data": [["A"]]}]})
        with patch("warehouse_bot.worker", new=worker):
            await self.click("data:ref")
            for text in first_opts:
                await self.send(text)
            await self.send("ORIGINAL-REFERENCE")
            await self.send("лист 3")
            await self.send("ORIGINAL-ACTUAL")
            report = self.session.requests[-2]
            repeat = self.buttons(report)["Повторить"]
            await self.click(repeat)
            self.clock.now += 5
            await self.send("NEW-REFERENCE")
            await self.send("NEW-ACTUAL")
        old, new = (call.args[0] for call in worker.await_args_list)
        self.assertNotEqual([i["data"] for i in old["items"]], [i["data"] for i in new["items"]])
        self.assertEqual([i["opts"] for i in old["items"]], [i["opts"] for i in new["items"]])
        self.assertEqual(new["items"][0]["opts"], {"sheet": "2", "a": "A", "b": "B", "link": "C,D",
                                                   "header": 1, "encoding": "cp1251"})
        self.assertEqual(new["items"][1]["opts"], {"sheet": "3"})
        self.assertNotIn("ORIGINAL", repr(self.guard.state(42).repeats))
        self.assertEqual(await self.ctx().get_data(), {})

    async def test_changing_mode_does_not_reset_heavy_cooldown(self):
        await self.click("menu:clean", uid=43)
        await self.send("0009", uid=43)
        await self.click("data:asc", uid=43)
        with patch("warehouse_bot.worker", new_callable=AsyncMock) as worker:
            await self.send("B\nA", uid=43)
            worker.assert_not_awaited()
        self.assertIn("Подожди", self.session.requests[-1].text)

    async def test_closed_start_write_budget_limits_new_users(self):
        for uid in range(700, 732):
            await self.send("/start", uid=uid)
        self.assertEqual(self.session.requests, [])
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM start_visitors").fetchone()[0], 12)
        self.clock.now += 61
        await self.send("/start", uid=799)
        self.assertFalse(self.repo.allowed(799))
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM start_visitors").fetchone()[0], 13)

    async def test_staff_back_preserves_search_and_page_without_taking_work_text(self):
        for uid in range(100, 113):
            self.repo.change_employee(42, uid, "give", name=f"склад {uid}")
        self.repo.change_employee(42, 200, "give", name="другая группа")
        await self.click("ad:staffsearch")
        await self.send("склад")
        await self.click(self.buttons()["Далее ›"])
        buttons = self.buttons()
        card = next(button for label, button in buttons.items() if "склад 109" in label)
        await self.click(card)
        await self.click(self.buttons()["Назад"])
        self.assertIn("страница 2", self.session.requests[-1].text)
        self.assertTrue(any("склад 109" in text for text in self.buttons()))
        self.assertIsNone(await self.ctx().get_state())
        await self.send("B\nA")
        self.assertEqual(self.session.requests[-1].text, "Что сделать с текстом?")

    async def test_saturated_service_pool_allows_staff_short_work_and_owner_panel(self):
        gate = asyncio.Event()
        original = self.session.make_request
        regular = range(100, 100 + self.guard.light.parallel)
        for uid in regular:
            self.repo.change_employee(42, uid, "give")

        async def transport(bot, method, timeout=None):
            if isinstance(method, SendMessage) and method.chat_id in regular:
                await gate.wait()
            return await original(bot, method, timeout)

        with patch.object(self.session, "make_request", new=transport):
            try:
                for uid in regular:
                    await self.click("data:asc", uid=uid)
                for uid in regular:
                    # Ошибочный параметр требует только служебного ответа.
                    await asyncio.wait_for(self.send("лист 99", uid=uid, settle=False), 1)
                self.assertEqual(len(self.guard.light.keys), len(regular))
                await asyncio.wait_for(self.send("лист 99", uid=100, settle=False), 1)
                self.assertEqual(len(self.guard.light.keys), len(regular))
                await self.send("/admin", settle=False)
                await asyncio.gather(*self.guard.priority.tasks)
                self.assertEqual(self.session.requests[-1].text, "Админ-панель")
                await self.click("barcode:" + next(iter(self.catalog)), uid=43, settle=False)
                await asyncio.gather(*self.guard.heavy.tasks)
                self.assertIsInstance(self.session.requests[-1], SendPhoto)
            finally:
                gate.set()
            await self.settle()

    async def test_waiting_file_expires_without_download_and_short_work_starts(self):
        self.configure(replace(Settings(), parallel_operations=2, data_parallel=1))
        gate = asyncio.Event()
        self.guard.heavy.submit(44, gate.wait, long=True)
        await self.click("data:asc", settle=False)
        await asyncio.gather(*self.guard.light.tasks)
        download = AsyncMock()
        with patch("warehouse_bot.DATA_DEADLINE", 0.03), patch.object(self.bot, "download", new=download):
            try:
                await self.document(settle=False)
                self.assertIn(42, self.guard.heavy.queue)
                await self.click("barcode:" + next(iter(self.catalog)), uid=43, settle=False)
                self.assertIn(43, self.guard.heavy.running)
                await asyncio.sleep(0.06)
                await asyncio.gather(*self.guard.control.tasks)
                download.assert_not_awaited()
                self.assertIsNone(await self.ctx().get_state())
                self.assertTrue(any("истекло в очереди" in (getattr(x, "text", "") or "") for x in self.session.requests))
            finally:
                gate.set()
            await self.settle()

    async def test_cancel_waiting_file_discards_job_without_download(self):
        self.configure(replace(Settings(), parallel_operations=2, data_parallel=1))
        release = asyncio.Event()
        self.guard.heavy.submit(44, release.wait, long=True)
        download = AsyncMock()
        with patch.object(self.bot, "download", new=download):
            try:
                await self.click("data:asc", settle=False)
                await asyncio.gather(*self.guard.light.tasks)
                await self.document(settle=False)
                self.assertIn(42, self.guard.heavy.queue)
                await self.send("/cancel", settle=False)
                self.assertNotIn(42, self.guard.heavy.queue)
                self.assertEqual(await self.ctx().get_data(), {})
            finally:
                release.set()
            await self.settle()
            download.assert_not_awaited()

    async def test_total_deadline_interrupts_active_download(self):
        cancelled = asyncio.Event()

        async def download(file_id, destination):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        await self.click("data:asc")
        with patch("warehouse_bot.DATA_DEADLINE", 0.03), patch.object(self.bot, "download", new=download):
            await self.document()
        self.assertTrue(cancelled.is_set())
        self.assertNotIn(42, self.guard.heavy.keys)
        self.assertTrue(any("Время обработки истекло" in (getattr(x, "text", "") or "") for x in self.session.requests))
        self.assertFalse(any(isinstance(x, SendDocument) for x in self.session.requests))

    async def test_download_and_send_share_one_deadline(self):
        original = self.session.make_request

        async def download(file_id, destination):
            await asyncio.sleep(0.02)
            destination.write(b"local data")

        async def transport(bot, method, timeout=None):
            if isinstance(method, SendDocument):
                await asyncio.Future()
            return await original(bot, method, timeout)

        output = {"report": "Сортировка завершена.", "outputs": [{"kind": "xlsx", "data": "QUJD"}]}
        with (patch("warehouse_bot.DATA_DEADLINE", 0.10),
              patch.object(self.bot, "download", new=download),
              patch("warehouse_bot.worker", return_value=output) as worker,
              patch.object(self.session, "make_request", new=transport)):
            await self.click("data:asc")
            await asyncio.wait_for(self.document(), 1)
        self.assertLess(worker.await_args.args[1], 0.10)
        self.assertIn("Статус последней отправки неизвестен", self.session.requests[-1].text)
        self.assertIn("0 из 1", self.session.requests[-1].text)

    async def test_same_user_has_a_bounded_service_budget(self):
        calls = AsyncMock()
        for _ in range(4):
            self.assertTrue(self.guard.service(43, calls))
            await self.settle()
        self.assertFalse(self.guard.service(43, calls))
        self.assertTrue(self.guard.service(44, calls))
        await self.settle()
        self.clock.now += 1
        self.assertTrue(self.guard.service(43, calls))
        await self.settle()
        self.assertEqual(calls.await_count, 6)

    async def test_quick_mode_works_directly_from_reconciliation_page(self):
        await self.click("data:check")
        self.assertNotIn("Быстрая сверка", self.buttons())
        await self.send("B\nA\n\nA\nB")
        self.assertIn("Сошлось: 2/2", self.session.requests[-3].text)
