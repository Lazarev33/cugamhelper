"""Unit tests for menu logic that do not require a running VPS."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cli import token_is_valid, configured, save_token
from branding import BOT_NAME


class CLITests(unittest.TestCase):
    def test_bot_name(self):
        self.assertEqual(BOT_NAME, "Помощник 2 смены")

    def test_token_format(self):
        self.assertTrue(token_is_valid('123456:' + 'abcDEF1234_-xyz0123456789'))
        self.assertFalse(token_is_valid('123:bad token'))
        self.assertFalse(token_is_valid('token; rm -rf /'))
        self.assertFalse(token_is_valid(''))

    def test_missing_or_incorrect_token(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / 'bot.env'
            with patch('cli.ENV_FILE', path):
                self.assertFalse(configured())
                path.write_text('BOT_TOKEN=bad\n', encoding='utf-8')
                self.assertFalse(configured())

    def test_read_valid_token(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / 'bot.env'
            path.write_text('BOT_TOKEN=123456:abcDEF1234_-xyz0123456789\n', encoding='utf-8')
            with patch('cli.ENV_FILE', path):
                self.assertTrue(configured())

    def test_reject_invalid_token_before_write(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / 'bot.env'
            with self.assertRaises(ValueError):
                save_token('bad value', path)
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
