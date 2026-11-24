"""Проверки режимов на настоящем Dispatcher и конфигурации на временных файлах."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.methods import AnswerCallbackQuery, SendPhoto

import cli
import deployment
from bot import dp
from features import ALL_DISABLED, DATA_ACTIONS, DEFAULT_FEATURES, FORMATS, LABELS, MODES, FeatureStore, Features, load_features
from settings import Settings, load_settings
from test_bot import TelegramCase, USER


class FeatureConfigTests(unittest.TestCase):
    def test_registry_defaults_and_dependencies(self):
        self.assertEqual(len(LABELS), 14)
        self.assertTrue(all(DEFAULT_FEATURES.available(key) for key in LABELS))
        self.assertFalse(ALL_DISABLED.data)
        self.assertFalse(ALL_DISABLED.barcodes)
        self.assertFalse(Features(frozenset({"format_text"})).permits("clean"))
        no_tables = Features(frozenset({"format_xlsx", "format_docx"}))
        self.assertFalse(no_tables.permits("align"))
        self.assertTrue(no_tables.permits("quick", "txt"))
        for feature in LABELS:
            with self.subTest(feature=feature):
                policy = Features(frozenset({feature}))
                self.assertFalse(policy.enabled(feature))
                self.assertTrue(all(policy.enabled(other) for other in LABELS if other != feature))
                for mode, (key, _) in MODES.items():
                    if key == feature:
                        self.assertFalse(policy.permits(mode))

    def test_atomic_permissions_reload_rollback_and_concurrent_edit(self):
        with TemporaryDirectory() as work:
            root = Path(work)
            path = root / "features.json"
            lock = root / "lock"
            settings = root / "settings.json"
            settings.write_text('{"owner_telegram_id": 123}')
            reader = FeatureStore(path)
            old = reader.current()
            desired = old.options()
            desired["data_sort"] = False
            with (patch.object(cli, "FEATURES_PATH", path), patch.object(deployment, "LOCK_FILE", lock),
                  patch.object(cli.grp, "getgrnam"), patch.object(cli.os, "fchown")):
                new = cli.save_features(desired, old)
                self.assertEqual(path.stat().st_mode & 0o777, 0o640)
                self.assertEqual(new, reader.current())
                self.assertEqual(FeatureStore(path).current(), new)
                self.assertEqual(load_settings(settings).owner_telegram_id, 123)
                with self.assertRaises(deployment.DeploymentError):
                    cli.save_features(old.options(), old)
                with deployment.maintenance_lock(), self.assertRaises(deployment.DeploymentError):
                    cli.save_features(old.options(), new)
                with self.assertRaises(ValueError):
                    cli.save_features({"data_sort": "false"}, new)
                with patch.object(cli.os, "replace", side_effect=OSError("операция отклонена")), self.assertRaises(OSError):
                    cli.save_features(old.options(), new)
                self.assertEqual(new, load_features(path))
                saved = path.read_bytes()
                path.write_bytes(b'{"data_sort":')
                with self.assertLogs("features", level="WARNING"):
                    self.assertEqual(new, reader.current())
                path.write_bytes(saved)
                self.assertEqual(new, reader.current())


class LiveFeaturesTests(TelegramCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "features.json"
        self.guard.feature_store = FeatureStore(self.path)
        self.guard.sync_features(dp.storage)

    def toggles(self, *disabled):
        temporary = self.path.with_suffix(".new")
        temporary.write_text(json.dumps(dict.fromkeys(disabled, False)))
        temporary.replace(self.path)

    def context(self, uid=43):
        return dp.fsm.get_context(bot=self.bot, chat_id=uid, user_id=uid)

    def labels(self):
        buttons = self.session.requests[-1].reply_markup.inline_keyboard
        return {b.text: b.callback_data for row in buttons for b in row}

    async def file(self, ext, **kwargs):
        await self.feed(message={"message_id": 6, "date": 1, "chat": {"id": 43, "type": "private"},
                                 "from": {**USER, "id": 43}, "document": {
                                     "file_id": "synthetic", "file_unique_id": "synthetic", "file_name": f"test.{ext}",
                                 }}, **kwargs)

    async def test_each_data_switch_blocks_old_callback_and_hides_entry(self):
        for mode, label, _ in DATA_ACTIONS:
            with self.subTest(mode=mode):
                flag = MODES[mode][0]
                self.toggles(flag)
                self.clock.now += 5
                await self.click("data:home", uid=43)
                self.assertNotIn(label, self.labels())
                await self.click(f"data:{mode}", uid=43)
                self.assertIsInstance(self.session.requests[-1], AnswerCallbackQuery)
                self.assertIn("отключены", self.session.requests[-1].text)
                self.assertIsNone(await self.context().get_state())
                self.toggles()
                self.clock.now += 5
                await self.click(f"data:{mode}", uid=43)
                self.assertIsNotNone(await self.context().get_state())
                await self.send("/cancel", uid=43)

    async def test_auto_offers_independent_and_disabled_format_never_downloaded(self):
        self.toggles("data_sort", "data_find_duplicates", "format_xlsx")
        with patch.object(self.bot, "download", new_callable=AsyncMock) as download:
            await self.file("xlsx")
            self.assertIn("отключены", self.session.requests[-1].text)
            self.assertIsNone(await self.context().get_state())
            download.assert_not_awaited()
        for kind in ("docx", "txt"):
            self.clock.now += 5
            await self.file(kind)
            actions = self.labels()
            self.assertNotIn("Сортировать", actions)
            self.assertNotIn("Найти дубликаты", actions)
            self.assertIn("Проверить пары", actions)
            self.assertIn("Сверить два списка", actions)
            self.assertIn("Удалить дубликаты", actions)

    async def test_all_formats_gate_fsm_and_legacy_offer_without_affecting_commands(self):
        for kind in sorted(FORMATS):
            with self.subTest(kind=kind):
                self.toggles()
                self.clock.now += 5
                await self.send("/menu", uid=43)
                if kind == "text":
                    await self.send("B\nA", uid=43)
                else:
                    await self.file(kind)
                old = self.labels()["Сортировать"]
                self.toggles("format_" + kind)
                await self.click(old, uid=43)
                self.assertIsInstance(self.session.requests[-1], AnswerCallbackQuery)
                self.assertIsNone(await self.context().get_state())
                self.clock.now += 5
                await self.click("data:asc", uid=43)
                with patch.object(self.bot, "download", new_callable=AsyncMock) as download:
                    if kind == "text":
                        await self.send("A", uid=43)
                    else:
                        await self.file(kind)
                    self.assertIn("отключены", self.session.requests[-1].text)
                    download.assert_not_awaited()
                await self.send("/myid", uid=43)
                self.assertIn("43", self.session.requests[-1].text)
        await self.send("/cancel", uid=43)

    async def test_disabled_barcode_paths_and_admin_maintenance(self):
        self.toggles("barcode_catalog", "barcode_scan", "clean_numbers")
        await self.send("/start", uid=43)
        self.assertNotIn("ШК", self.labels())
        self.assertNotIn("Очистка номеров", self.labels())
        for callback in ("menu:shk", "menu:barcodes", "catalog:0", "barcode:A-POL", "scan:start", "menu:clean"):
            await self.click(callback, uid=43)
            self.assertIsInstance(self.session.requests[-1], AnswerCallbackQuery)
            self.assertIn("отключ", self.session.requests[-1].text)
        await self.send("/scan", uid=43)
        self.assertIn("отключено", self.session.requests[-1].text)
        await self.send("/menu")
        self.assertIn("Админ-панель", self.labels())
        await self.click("ad:catalog")
        self.assertIn("Добавить по фото", self.labels())
        record = self.repo.by_value("A-POL")
        await self.click(f"ad:generate:{record.id}")
        self.assertIsInstance(self.session.requests[-1], SendPhoto)
        self.clock.now += 5
        self.toggles("barcode_scan")
        await self.click("menu:shk", uid=43)
        self.assertIn("A-POL", self.labels())
        self.toggles("barcode_catalog")
        self.clock.now += 5
        await self.click("menu:shk", uid=43)
        self.assertIn("Пришли фото", self.session.requests[-1].text)

    async def test_all_disabled_no_empty_menu_and_no_staff_admin(self):
        self.toggles(*LABELS)
        await self.send("/start", uid=43)
        self.assertEqual(list(self.labels()), ["Помощь"])
        await self.click("menu:help", uid=43)
        self.assertIn("выключены", self.session.requests[-1].text)
        self.assertNotIn("сортировка", self.session.requests[-1].text.lower())
        await self.send("/start")
        self.assertEqual(list(self.labels()), ["Админ-панель", "Помощь"])
        await self.click("ad:access")
        self.assertIn("Доверенные пользователи", self.labels())
        self.repo.set_admin(42, 44, True)
        await self.send("/start", uid=44)
        self.assertEqual(list(self.labels()), ["Админ-панель", "Помощь"])
        await self.click("ad:home", uid=43)
        self.assertIn("Доступ закрыт", self.session.requests[-1].text)
        count = len(self.session.requests)
        await self.send("/help", uid=300)
        self.assertEqual(len(self.session.requests), count)

    async def test_repeat_rechecks_operation_and_format(self):
        await self.click("data:asc", uid=43)
        await self.send("B\nA", uid=43)
        report = self.session.requests[-2]
        repeat = next(b.callback_data for row in report.reply_markup.inline_keyboard
                      for b in row if b.text == "Повторить")
        for flag in ("data_sort", "format_text"):
            self.toggles(flag)
            await self.click(repeat, uid=43)
            self.assertIsInstance(self.session.requests[-1], AnswerCallbackQuery)
            self.assertIn("отключены", self.session.requests[-1].text)
            self.assertIsNone(await self.context().get_state())
        self.toggles()
        await self.click(repeat, uid=43)
        self.assertEqual((await self.context().get_data())["mode"], "asc")

    async def test_invalidates_queued_and_pending_but_finishes_already_running(self):
        self.configure(replace(Settings(), parallel_operations=2, data_parallel=1))
        self.guard.feature_store = FeatureStore(self.path)
        started, release = asyncio.Event(), asyncio.Event()
        output = {"report": "Сортировка завершена.", "outputs": [{"kind": "text", "data": [["X"]]}]}

        async def run(request, timeout):
            started.set()
            await release.wait()
            return output

        with patch("warehouse_bot.worker", side_effect=run) as worker:
            try:
                await self.click("data:asc", uid=43, settle=False)
                await asyncio.gather(*self.guard.light.tasks)
                await self.send("X", uid=43, settle=False)
                await asyncio.wait_for(started.wait(), 2)
                await self.click("data:asc", uid=44, settle=False)
                await asyncio.gather(*self.guard.light.tasks)
                await self.send("Y", uid=44, settle=False)
                self.assertIn(44, self.guard.heavy.queue)
                await self.click("data:check", uid=45, settle=False)
                await asyncio.gather(*self.guard.light.tasks)
                self.toggles("data_sort")
                self.guard.sync_features(dp.storage)
                self.assertNotIn(44, self.guard.heavy.queue)
                self.assertEqual(await self.context(44).get_data(), {})
                self.assertEqual((await self.context(45).get_data())["mode"], "quick")
                self.assertIn(43, self.guard.heavy.running)
            finally:
                release.set()
            await self.settle()
            self.assertEqual(worker.await_count, 1)
            self.assertEqual(self.session.requests[-1].text, "X")
            self.assertEqual(await self.context().get_data(), {})
