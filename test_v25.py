"""Проверки мастера, защиты настроек, резервной копии и качества декодирования."""
from contextlib import ExitStack, closing
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, Mock

from PIL import Image, ImageDraw, ImageFilter, ImageEnhance

import cli
from barcodes import render_png
from scanner import decode_image


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(TemporaryDirectory()))
        self.settings = self.root / "settings.json"
        self.settings.write_text('{"barcode_mode":"document","owner_telegram_id":10}')
        self.env = self.root / "bot.env"
        self.env.write_text("OTHER=keep\n")
        stack.enter_context(patch.multiple(cli, SETTINGS_PATH=self.settings, ENV_FILE=self.env,
                                          BACKUPS=self.root / "copies", DB=self.root / "db"))
        stack.enter_context(patch("cli.os.fchown"))
        stack.enter_context(patch("cli.grp.getgrnam", return_value=Mock(gr_gid=os.getgid())))
        stack.enter_context(patch("cli.service_active", return_value=True))
        self.start = stack.enter_context(patch("cli.start_and_check"))

    def test_preserve_and_validate(self):
        cli.apply_settings({"owner_telegram_id": 11})
        self.assertEqual(json.loads(self.settings.read_text()), {"owner_telegram_id":11,"barcode_mode":"document"})
        for changes in [{"allowed_user_ids":[]}, {"queue_size":33}]:
            with self.assertRaises(ValueError):
                cli.apply_settings(changes)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o640)
        self.start.assert_called_once()

    def test_rollback_both_files(self):
        old = self.settings.read_bytes(), self.env.read_bytes()
        self.start.side_effect = [RuntimeError("нет готовности"), None]
        with self.assertRaises(RuntimeError):
            cli.apply_settings({"owner_telegram_id":22}, "123:" + "x"*22)
        self.assertEqual((self.settings.read_bytes(), self.env.read_bytes()), old)
        self.assertEqual(self.start.call_count, 2)

    def test_wizard_first_setup(self):
        self.settings.unlink()
        token = "123:" + "z"*22
        with patch("cli.verify_token") as verify, patch("cli.getpass.getpass", return_value=token), patch("builtins.input", side_effect=["12","да"]):
            cli.setup("setup")
        verify.assert_called_once_with(token)
        self.assertEqual(json.loads(self.settings.read_text())["owner_telegram_id"], 12)
        self.assertIn("OTHER=keep", self.env.read_text())

    def test_owner_cancel(self):
        old = self.settings.read_bytes()
        with patch("builtins.input", side_effect=["13","нет"]):
            cli.setup("owner")
        self.assertEqual(self.settings.read_bytes(), old)
        self.start.assert_not_called()

    def test_getme(self):
        with patch("cli.http.client.HTTPSConnection") as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 401
            with self.assertRaises(ValueError):
                cli.verify_token("123:"+"x"*22)
            response.status = 200
            response.read.return_value = b'{"ok":true,"result":{"is_bot":true}}'
            cli.verify_token("123:"+"x"*22)
            self.assertTrue(factory.return_value.request.call_args.args[1].endswith("/getMe"))

    def test_backup_with_wal(self):
        with closing(sqlite3.connect(cli.DB)) as db:
            db.execute("pragma journal_mode=wal")
            db.execute("create table admins(id)")
            db.execute("insert into admins values (42)")
            db.commit()
            cli.backup()
            path, = cli.BACKUPS.iterdir()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with closing(sqlite3.connect(path)) as copy:
                self.assertEqual(copy.execute("select id from admins").fetchone(), (42,))


class ScannerQualityTests(unittest.TestCase):
    def test_caption_below_bars(self):
        image = Image.open(BytesIO(render_png("A-POL"))).convert("L")
        # При 508 dpi полосы заканчиваются на строке 500 включительно.
        self.assertEqual(image.crop((0, 501, image.width, 521)).getextrema(), (255, 255))
        self.assertEqual(image.crop((0, 521, image.width, image.height)).getextrema()[0], 0)

    def test_jpeg_variants(self):
        value = " 00Mix_ed-9 "
        image = Image.open(BytesIO(render_png(value))).convert("L")
        glare = image.copy()
        ImageDraw.Draw(glare).ellipse((0,0,image.width, image.height//3), fill=250)
        for variant in [image.rotate(7, expand=True, fillcolor=255), image.transpose(Image.Transpose.ROTATE_90),
                        ImageEnhance.Contrast(image).enhance(.18), image.filter(ImageFilter.GaussianBlur(1)), glare]:
            with self.subTest(size=variant.size):
                buf = BytesIO()
                variant.save(buf, "JPEG", quality=48)
                self.assertEqual([code.value for code in decode_image(buf.getvalue())], [value])
