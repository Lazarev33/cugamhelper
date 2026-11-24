"""Проверки миграции, уникальности и сценариев Telegram."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiogram.methods import AnswerCallbackQuery, SendPhoto
from PIL import Image
import zxingcpp

from barcodes import load_seed_values, render_png
from catalog import Catalog, CatalogDenied, CatalogError, DuplicateBarcode
from scanner import MAX_BYTES, Decoded, ScanError, decode_image, scan_local, scan_telegram
from test_bot import TelegramCase

SEEDS = ["A-POL", "A-AKB1", "A-OUT13", "A-IN-S1", "A-IN-G1", "A-05", "A-03", "A-OUTP1", "A-BR1"]


def qr_image():
    barcode = zxingcpp.create_barcode("000Xy", zxingcpp.BarcodeFormat.QRCode)
    return Image.fromarray(zxingcpp.write_barcode_to_image(barcode, scale=6))


def png(image):
    data = BytesIO()
    image.save(data, "PNG")
    return data.getvalue()


class CatalogTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "catalog.sqlite3"

    def repo(self, **kwargs):
        repo = Catalog(self.path, owner=42, **kwargs)
        self.addCleanup(repo.close)
        return repo

    def test_migration_local_catalog_and_no_reimport_after_deletion(self):
        local = self.path.with_name("barcodes.json")
        local.write_text('["LOCAL-001", "A-POL"]')
        repo = self.repo(seed=lambda: load_seed_values(local))
        self.assertEqual([v[0] for v in repo.db.execute("SELECT value FROM barcodes ORDER BY id")],
                         ["LOCAL-001", *SEEDS])
        repo.change(42, repo.by_value("A-POL"), delete=True)
        repo.set_admin(42, 43, True)
        item = repo.save(43, "Новый", " 000Xy ")
        local.unlink()
        restored = self.repo()
        self.assertIsNone(restored.by_value("A-POL"))
        self.assertEqual(restored.by_value(" 000Xy "), item)
        self.assertTrue(restored.is_admin(43))
        self.assertEqual(len(restored.recent()), 3)

    def test_schema_backup_for_existing_database(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE existing (value TEXT)")
            connection.execute("INSERT INTO existing VALUES ('prior')")
        self.repo()
        with sqlite3.connect(self.path.with_name("catalog.sqlite3.schema-0.bak")) as snapshot:
            self.assertEqual(snapshot.execute("SELECT value FROM existing").fetchone()[0], "prior")
            self.assertEqual(snapshot.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_database_unique_per_type_and_exact_case(self):
        repo = self.repo(seed=lambda: [])
        original = repo.save(42, "Первая", "000Ab")
        second = repo.save(42, "Вторая", "000ab")
        self.assertNotEqual(original.id, second.id)
        with self.assertRaises(sqlite3.IntegrityError):
            repo.db.execute("INSERT INTO barcodes(name,value) VALUES ('дубликат','000Ab')")
        repo.db.execute("INSERT INTO barcodes(name,value,format) VALUES ('другой тип','000Ab','QR Code')")
        repo.change(42, original)
        with self.assertRaises(DuplicateBarcode) as error:
            repo.save(42, "Третье название", "000Ab")
        self.assertEqual(error.exception.record.id, original.id)
        self.assertFalse(error.exception.record.active)
        with self.assertRaises(DuplicateBarcode):
            repo.save(42, second.name, "000Ab", second)
        self.assertEqual(repo.get(second.id), second)

    def test_concurrent_admins_can_save_only_one_record(self):
        repo = self.repo(seed=lambda: [])
        repo.set_admin(42, 43, True)
        barrier = Barrier(2)

        def submit(uid):
            other = Catalog(self.path, owner=42)
            try:
                barrier.wait(3)
                try:
                    return ("saved", other.save(uid, str(uid), "000Ab").id)
                except DuplicateBarcode as error:
                    return ("duplicate", error.record.id)
            finally:
                other.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = executor.map(submit, [42, 43])
        self.assertEqual({first[0], second[0]}, {"saved", "duplicate"})
        self.assertEqual(first[1], second[1])
        self.assertEqual(repo.db.execute("SELECT count(*) FROM barcodes").fetchone()[0], 1)

    def test_roles_revocation_revision_and_owner_protection(self):
        repo = self.repo()
        with self.assertRaises(CatalogDenied):
            repo.save(43, "Зона", "000Ab")
        with self.assertRaises(CatalogError):
            repo.set_admin(42, 42, False)
        repo.set_admin(42, 43, True)
        old = repo.save(43, "Зона", "000Ab")
        changed = repo.save(43, "Зона 2", "000aB", old)
        self.assertEqual(old.created_by, changed.created_by)
        with self.assertRaises(CatalogError):
            repo.change(42, old, delete=True)
        with self.assertRaises(CatalogDenied):
            repo.set_admin(43, 44, True)
        repo.set_admin(42, 43, False)
        with self.assertRaises(CatalogDenied):
            repo.change(43, changed, delete=True)
        self.assertEqual(repo.get(changed.id), changed)


class ScannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_download_is_bounded_even_without_size_metadata(self):
        class Telegram:
            async def get_file(self, file_id, request_timeout):
                return SimpleNamespace(file_path="img.png", file_size=None)

            async def download_file(self, path, destination, timeout, seek):
                destination.write(b"0" * MAX_BYTES)
                destination.write(b"1")

        with self.assertRaisesRegex(ScanError, "5 МиБ"):
            await scan_telegram(Telegram(), "image", None)

    async def test_real_worker_and_nine_original_values(self):
        self.assertEqual(load_seed_values(Path("unconfigured-seed.json")), SEEDS)
        for value in [*SEEDS, " 000Ab "]:
            self.assertEqual(decode_image(render_png(value)), [Decoded("Code 128", value, True)])
        self.assertEqual(await scan_local(render_png("00Az")), [Decoded("Code 128", "00Az", True)])
        for invalid in ("", "русский", "A\nB", "Z"*49, "    "):
            with self.assertRaises(ValueError):
                render_png(invalid)

    async def test_multiple_formats_blank_and_pixel_limit(self):
        barcode = Image.open(BytesIO(render_png("A-IN-S1")))
        qr = qr_image()
        photo = Image.new("RGB", (barcode.width + qr.width + 40, barcode.height), "white")
        photo.paste(barcode, (0, 0))
        photo.paste(qr, (barcode.width+20, 70))
        jpeg = BytesIO()
        photo.resize((1000, 480)).save(jpeg, "JPEG", quality=78)
        found = {item.format: item for item in await scan_local(jpeg.getvalue())}
        self.assertEqual(found, {"Code 128": Decoded("Code 128", "A-IN-S1", True),
                                 "QR Code": Decoded("QR Code", "000Xy", False)})
        self.assertEqual(await scan_local(png(Image.new("L", (300, 200), 255))), [])
        with self.assertRaises(ScanError):
            await scan_local(png(Image.new("L", (4000, 2100))))
        with self.assertRaises(ScanError):
            await scan_local(b"not an image")


class AdminTests(TelegramCase):
    def buttons(self):
        return {b.text: b.callback_data for row in self.session.requests[-1].reply_markup.inline_keyboard for b in row}

    async def photo(self, uid=42, **kwargs):
        await self.feed(message={
            "message_id": 10, "date": 1, "chat": {"id": uid, "type": "private"},
            "from": {"id": uid, "is_bot": False, "first_name": "Сотрудник"},
            "photo": [{"file_id": "img", "file_unique_id": "fixture", "width": 1000, "height": 480}],
        }, **kwargs)

    async def manual(self, name, value, uid=42):
        await self.click("ad:manual", uid)
        await self.send(name, uid)
        await self.send(value, uid)

    async def test_manual_confirm_then_staff_catalog_updates(self):
        await self.send("/admin")
        self.assertIn("Каталог ШК", self.buttons())
        await self.manual("Зона приёмки №1", "000Az")
        self.assertIsInstance(self.session.requests[-1], SendPhoto)
        self.assertIsNone(self.repo.by_value("000Az"))
        save = self.buttons()["Сохранить"]
        await self.click(save)
        record = self.repo.by_value("000Az")
        self.assertEqual(record.name, "Зона приёмки №1")
        await self.click("catalog:1", 43)
        self.assertIn("Зона приёмки №1", self.buttons())
        await self.click(f"code:{record.id}:1", 43)
        media = self.session.requests[-1]
        self.assertIsInstance(media, SendPhoto)
        self.assertEqual(decode_image(media.photo.data)[0].value, "000Az")
        await self.click(save)
        self.assertEqual(self.repo.by_value("000Az"), record)

    async def test_duplicate_inactive_can_be_restored_and_value_edit_is_checked(self):
        existing = self.repo.by_value("A-POL")
        self.repo.change(42, existing)
        await self.manual("Другое название", "A-POL")
        self.assertIn("Данный штрихкод уже существует!", self.session.requests[-1].text)
        self.assertIn("выключен", self.session.requests[-1].text)
        self.assertIn("Открыть существующий ШК", self.buttons())
        await self.click(self.buttons()["Восстановить ШК"])
        self.assertTrue(self.repo.get(existing.id).active)
        second = self.repo.by_value("A-05")
        await self.click(f"ad:value:{second.id}")
        await self.send("A-POL")
        self.assertIn("Данный штрихкод уже существует!", self.session.requests[-1].text)
        self.assertEqual(self.repo.get(second.id), second)
        await self.send("000Mix")
        self.assertIn("Было: «A-05»\nБудет: «000Mix»", self.session.requests[-1].caption)
        await self.click(self.buttons()["Сохранить"])
        self.assertEqual(self.repo.get(second.id).value, "000Mix")

    async def test_admin_multi_scan_chooses_then_confirms_and_rejects_unsupported(self):
        await self.click("ad:scan")
        values = [Decoded("QR Code", "abc", False), Decoded("Code 128", "00F", True)]
        with patch("admin.scan_telegram", return_value=values):
            await self.photo()
        self.assertIsNone(self.repo.by_value("00F"))
        await self.click(self.buttons()["1. QR Code · abc"])
        self.assertIn("Формат: QR Code", self.session.requests[-1].text)
        self.assertNotIn("Сохранить", self.buttons())
        self.clock.now += 5
        with patch("admin.scan_telegram", return_value=values):
            await self.photo()
        await self.click(self.buttons()["2. Code 128 · 00F"])
        await self.send("Из фото")
        save = self.buttons()["Сохранить"]
        self.assertIsNone(self.repo.by_value("00F"))
        await self.click(save)
        self.assertEqual(self.repo.by_value("00F").name, "Из фото")

    async def test_scan_duplicate_and_cancel_preserve_database(self):
        await self.click("ad:scan")
        with patch("admin.scan_telegram", return_value=[Decoded("Code 128", "A-POL", True)]):
            await self.photo()
        self.assertIn("Данный штрихкод уже существует!", self.session.requests[-1].text)
        await self.manual("Отмена", "00F")
        save = self.buttons()["Сохранить"]
        await self.click(self.buttons()["Отмена"])
        await self.click(save)
        self.assertIsNone(self.repo.by_value("00F"))

    async def test_grant_revoke_and_forged_or_old_callbacks(self):
        await self.click("ad:manual", 43)
        self.assertIsInstance(self.session.requests[-1], AnswerCallbackQuery)
        self.assertIn("Доступ закрыт", self.session.requests[-1].text)
        await self.click("ad:grant")
        await self.send("43")
        await self.click(self.buttons()["Подтвердить"])
        self.assertTrue(self.repo.is_admin(43))
        await self.manual("Назначенный", "00F", 43)
        save = self.buttons()["Сохранить"]
        await self.click("ad:revoke:43")
        await self.click(self.buttons()["Подтвердить"])
        await self.click(save, 43)
        self.assertIsNone(self.repo.by_value("00F"))
        await self.click("ad:revoke:42")
        await self.click(self.buttons()["Подтвердить"])
        self.assertTrue(self.repo.is_admin(42))

    async def test_unconfigured_owner_closes_access(self):
        self.repo.set_admin(42,43,True)
        self.repo.owner=None
        await self.send("/admin",43)
        self.assertEqual(self.session.requests,[])
        await self.click("menu:barcodes",43)
        self.assertIsNone(self.session.requests[-1].text)

    async def test_duplicate_appearing_while_preview_open_and_delete_confirmation(self):
        await self.manual("Ручное имя", "ABC")
        save = self.buttons()["Сохранить"]
        record = self.repo.save(42, "Существующее", "ABC")
        await self.click(save)
        self.assertIn("Данный штрихкод уже существует!", self.session.requests[-1].text)
        self.assertIn("Существующее", self.session.requests[-1].text)
        await self.click(f"ad:delete:{record.id}:{record.revision}")
        self.assertIsNotNone(self.repo.get(record.id))
        await self.click(self.buttons()["Подтвердить"])
        self.assertIsNone(self.repo.get(record.id))

    async def test_employee_scan_then_cancel_restores_cleaning(self):
        await self.send("/scan", 43)
        with patch("admin.scan_telegram", return_value=[]):
            await self.photo(43)
        self.assertIn("Штрихкод не прочитан", self.session.requests[-1].text)
        self.clock.now += 5
        with patch("admin.scan_telegram", return_value=[Decoded("Code 128", "NewCode", True)]):
            await self.photo(43)
        self.assertIn("NewCode", self.session.requests[-1].text)
        self.assertIsNone(self.repo.by_value("NewCode"))
        await self.send("/cancel", 43)
        await self.click("menu:clean", uid=43)
        await self.send("0001-21.11.2026", 43)
        self.assertEqual(self.session.requests[-1].text, "0001")

    async def test_revoked_while_photo_scanning_discards_result(self):
        self.repo.set_admin(42, 43, True)
        started = asyncio.Event()
        finish = asyncio.Event()

        async def decode(*args):
            started.set()
            await finish.wait()
            return [Decoded("Code 128", "NewCode", True)]

        await self.click("ad:scan", 43)
        with patch("admin.scan_telegram", side_effect=decode):
            await self.photo(43, settle=False)
            await asyncio.wait_for(started.wait(), 2)
            self.repo.set_admin(42, 43, False)
            finish.set()
            await self.settle()
        self.assertIn("Доступ к админ-панели закрыт", self.session.requests[-1].text)
        self.assertIsNone(self.repo.by_value("NewCode"))
