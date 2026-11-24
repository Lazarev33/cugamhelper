"""Проверки клавиатуры и безопасного применения настроек панели."""

import curses
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import cli
from features import DEFAULT_FEATURES
from tui import ControlPanel, Snapshot, safe_text


class TerminalPanel(unittest.TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.path = Path(root.name) / "features.json"
        config = patch.object(cli, "FEATURES_PATH", self.path)
        config.start()
        self.addCleanup(config.stop)
        self.window = MagicMock()
        self.window.getmaxyx.return_value = (19, 46)
        with patch.object(ControlPanel, "setup_terminal"):
            self.ui = ControlPanel(self.window, initial=Snapshot(release="v2.7.6", active="active"))
        self.addCleanup(self.ui.executor.shutdown)

    def draw(self):
        return self.ui.paint(*self.ui.page_content())

    def test_small_terminal_navigation_draft_and_escape(self):
        rows = self.draw()
        self.ui.handle(curses.KEY_DOWN, rows)
        self.ui.handle(curses.KEY_DOWN, rows)
        self.ui.handle("\n", self.draw())
        self.assertEqual(self.ui.page.name, "features")
        self.ui.handle("\n", self.draw())
        self.assertFalse(self.ui.options["barcode_catalog"])
        self.assertFalse(self.path.exists())
        self.ui.handle("\x1b", self.draw())
        self.assertIsNotNone(self.ui.dialog)
        self.ui.handle("\x1b", self.draw())
        self.assertFalse(self.ui.options["barcode_catalog"])
        self.ui.handle("\x1b", self.draw())
        self.ui.handle(curses.KEY_RIGHT, self.draw())
        self.ui.handle("\n", self.draw())
        self.assertEqual(self.ui.page.name, "root")
        self.ui.handle("\x1b", self.draw())
        self.assertTrue(self.ui.stopping)

    def test_feature_save_waits_for_explicit_confirmation(self):
        self.ui.open("features")
        self.ui.flip("data_sort")
        with patch.object(cli, "save_features", return_value=DEFAULT_FEATURES) as save:
            self.ui.save_features()
            self.ui.handle("\n", self.draw())
            save.assert_not_called()
            self.ui.save_features()
            self.ui.handle(curses.KEY_LEFT, self.draw())
            self.ui.handle("\n", self.draw())
            save.assert_called_once()
        self.assertIn("без перезапуска", self.ui.notice)

    def test_validation_and_hidden_token_entry(self):
        self.ui.open("limits")
        self.ui.edit_limit("global_rate")
        self.ui.dialog.value = "9999"
        with self.assertRaises(ValueError):
            self.ui.handle("\n", self.draw())
        self.assertIsNone(self.ui.dialog)

        value = "123456:abcdefghijklmnopqrstu"
        self.ui.open("telegram")
        self.ui.field("Токен", "Ввод скрыт", lambda token: None, secret=True)
        for char in value:
            self.ui.handle(char, [])
        self.window.addnstr.reset_mock()
        self.draw()
        self.assertNotIn(value, repr(self.window.addnstr.call_args_list))
        self.assertNotIn(value, safe_text(value))
        self.ui.handle("\x1b", [])
        self.assertIsNone(self.ui.dialog)

    def test_noninteractive_command_does_not_open_tui(self):
        with (patch.object(cli.sys, "argv", ["numberbot", "status"]),
              patch.object(cli.os, "geteuid", return_value=0),
              patch.object(cli, "run", return_value=True) as run):
            self.assertEqual(cli.main(), 0)
        run.assert_called_once_with("status")
