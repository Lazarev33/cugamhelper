"""Проверки переключения релизов и отката без запущенного systemd."""

from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import deployment


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        work = Path(stack.enter_context(TemporaryDirectory()))
        self.app = work / "app"
        self.releases = work / "releases"
        self.releases.mkdir()
        self.config = work / "config"
        self.config.mkdir()
        self.token = self.config / "bot.env"
        self.token.write_bytes(b"BOT_TOKEN=123456:unit_test_token\n")
        self.token.chmod(0o640)
        self.unit = work / deployment.SERVICE
        self.cli = work / "numberbot"
        for key, path in {
            "APP_DIR": self.app, "RELEASES": self.releases, "UNIT_FILE": self.unit,
            "CLI_LINK": self.cli, "LOCK_FILE": work / "lock", "CONFIG_DIR": self.config,
        }.items():
            stack.enter_context(patch.object(deployment, key, path))
        self.control = stack.enter_context(patch.object(deployment, "systemctl"))
        self.active = stack.enter_context(patch.object(deployment, "service_active", return_value=True))

    def candidate(self, label, unit):
        path = self.releases / label
        path.mkdir()
        (path / deployment.SERVICE).write_bytes(unit)
        (path / "VERSION").write_text("2.2.0\n", encoding="ascii")
        return path

    def assert_token_untouched(self):
        self.assertEqual(self.token.read_bytes(), b"BOT_TOKEN=123456:unit_test_token\n")
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o640)

    def test_switch_and_keep_one_previous_release(self):
        old = self.candidate("release-old", b"existing unit")
        new = self.candidate("release-new", b"existing unit")
        self.app.symlink_to(old)
        self.unit.write_bytes(b"existing unit")
        with patch.object(deployment, "start_and_check") as start:
            deployment.activate(new, start_service=True, install=False)
        start.assert_called_once()
        self.assertEqual(self.app.resolve(), new)
        self.assertEqual((self.releases / "previous").resolve(), old)
        self.control.assert_called_once_with("stop", deployment.SERVICE)
        self.assert_token_untouched()

    def test_rollback_restores_old_release_and_unit(self):
        old = self.candidate("release-old", b"old unit")
        new = self.candidate("release-new", b"new unit")
        self.app.symlink_to(old)
        self.unit.write_bytes(b"old unit")
        failure = subprocess.CalledProcessError(1, ["systemctl", "restart"])
        with patch.object(deployment, "start_and_check", side_effect=[failure, None]) as start:
            with self.assertRaisesRegex(deployment.DeploymentError, "Восстановлена"):
                deployment.activate(new, start_service=True, install=False)
        self.assertEqual(start.call_count, 2)
        self.assertEqual(self.app.resolve(), old)
        self.assertEqual(self.unit.read_bytes(), b"old unit")
        self.assert_token_untouched()

    def test_migration_from_v21_can_rollback_physical_directory(self):
        self.app.mkdir()
        (self.app / "bot.py").write_text("old bot", encoding="ascii")
        self.unit.write_bytes(b"Type=simple")
        self.cli.symlink_to(self.app / "cli.py")
        new = self.candidate("release-new", b"Type=notify")
        with patch.object(deployment, "start_and_check",
                          side_effect=[deployment.DeploymentError("ошибка старта"), None]):
            with self.assertRaisesRegex(deployment.DeploymentError, "Восстановлена"):
                deployment.activate(new, start_service=True, install=True)
        self.assertFalse(self.app.is_symlink())
        self.assertEqual((self.app / "bot.py").read_text(), "old bot")
        self.assertEqual(self.unit.read_bytes(), b"Type=simple")
        self.assert_token_untouched()

    def test_missing_deploy_key_and_unchanged_revision(self):
        with self.assertRaisesRegex(deployment.DeploymentError, "deploy key"):
            deployment.git_environment()
        installed = self.candidate("release-installed", b"unit")
        revision = "a" * 40
        (installed / "REVISION").write_text(revision, encoding="ascii")
        self.app.symlink_to(installed)
        with patch.object(deployment, "remote_revision", return_value=revision):
            with patch("builtins.input") as confirm, patch.object(deployment, "deploy") as deploy:
                with redirect_stdout(StringIO()):
                    deployment.update(configured=lambda: True)
        confirm.assert_not_called()
        deploy.assert_not_called()
        self.control.assert_not_called()
        self.assert_token_untouched()
