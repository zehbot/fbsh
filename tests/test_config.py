"""Tests for .env discovery, parsing and precedence."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from hase10 import config
from hase10.config import ConfigError, Settings, find_env_file, parse_env_file


class ParseEnvFileTest(unittest.TestCase):
    def write(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        path = Path(handle.name)
        path.chmod(0o600)
        return path

    def test_simple_values(self):
        path = self.write("FRITZ_HOST=192.168.178.1\nFRITZ_USER=hase10\n")
        values = parse_env_file(path)
        self.assertEqual(values["FRITZ_HOST"], "192.168.178.1")
        self.assertEqual(values["FRITZ_USER"], "hase10")

    def test_quotes_comments_and_blanks(self):
        path = self.write(
            "# a comment\n"
            "\n"
            "export FRITZ_USER='hase 10'\n"
            'FRITZ_PASSWORD="p@ss #word"\n'
            "FRITZ_HOST=fritz.box  # trailing comment\n"
        )
        values = parse_env_file(path)
        self.assertEqual(values["FRITZ_USER"], "hase 10")
        self.assertEqual(values["FRITZ_PASSWORD"], "p@ss #word", "quoted # is not a comment")
        self.assertEqual(values["FRITZ_HOST"], "fritz.box")

    def test_value_with_equals_sign(self):
        path = self.write("FRITZ_PASSWORD=ab=cd\n")
        self.assertEqual(parse_env_file(path)["FRITZ_PASSWORD"], "ab=cd")

    def test_keys_are_upper_cased(self):
        path = self.write("fritz_user=klein\n")
        self.assertIn("FRITZ_USER", parse_env_file(path))


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in self.tmp.glob("*")] and None)
        self._env = os.environ.pop(config.ENV_VAR, None)

    def tearDown(self):
        if self._env is not None:
            os.environ[config.ENV_VAR] = self._env

    def test_env_var_points_to_file(self):
        path = self.tmp / "custom.env"
        path.write_text("FRITZ_USER=x\n")
        os.environ[config.ENV_VAR] = str(path)
        self.assertEqual(find_env_file(), path)

    def test_missing_explicit_file_returns_none(self):
        self.assertIsNone(find_env_file(str(self.tmp / "nope.env")))


class PrecedenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env_file = self.tmp / "test.env"
        self.env_file.write_text(
            "FRITZ_HOST=from-file\nFRITZ_USER=fileuser\nFRITZ_PASSWORD=filepw\n"
        )
        self.env_file.chmod(0o600)
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("FRITZ_HOST", "FRITZ_USER", "FRITZ_PASSWORD")}
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_file_values_are_used(self):
        settings = Settings.load(env_file=str(self.env_file))
        self.assertEqual(settings.host, "from-file")
        self.assertEqual(settings.user, "fileuser")
        self.assertEqual(settings.password, "file" + "pw")

    def test_environment_wins_over_file(self):
        os.environ["FRITZ_HOST"] = "from-env"
        settings = Settings.load(env_file=str(self.env_file))
        self.assertEqual(settings.host, "from-env")
        self.assertEqual(settings.user, "fileuser", "other keys still come from the file")

    def test_arguments_win_over_everything(self):
        os.environ["FRITZ_HOST"] = "from-env"
        settings = Settings.load(env_file=str(self.env_file), host="from-cli")
        self.assertEqual(settings.host, "from-cli")

    def test_missing_password_raises(self):
        self.env_file.write_text("FRITZ_HOST=fritz.box\n")
        self.env_file.chmod(0o600)
        with self.assertRaises(ConfigError) as caught:
            Settings.load(env_file=str(self.env_file))
        self.assertIn("no credentials configured", str(caught.exception))

    def test_world_readable_file_is_refused(self):
        if config.in_container():
            self.skipTest("permission check is disabled inside containers")
        self.env_file.chmod(0o644)
        with self.assertRaises(ConfigError) as caught:
            Settings.load(env_file=str(self.env_file))
        self.assertIn("chmod 600", str(caught.exception))

    def test_permissive_switch_allows_shared_file(self):
        self.env_file.chmod(0o644)
        os.environ["HASE10_ENV_PERMISSIVE"] = "1"
        self.addCleanup(os.environ.pop, "HASE10_ENV_PERMISSIVE", None)
        settings = Settings.load(env_file=str(self.env_file))
        self.assertEqual(settings.password, "file" + "pw")

    def test_group_readable_secret_mount_is_allowed_in_container(self):
        """Kubernetes mounts secrets 0440; the check must not fire there."""
        self.env_file.chmod(0o440)
        original = config.in_container
        config.in_container = lambda: True
        self.addCleanup(setattr, config, "in_container", original)
        settings = Settings.load(env_file=str(self.env_file))
        self.assertEqual(settings.host, "from-file")

    def test_invalid_port_is_reported(self):
        self.env_file.write_text("FRITZ_PASSWORD=x\nFRITZ_PORT=abc\n")
        self.env_file.chmod(0o600)
        with self.assertRaises(ConfigError):
            Settings.load(env_file=str(self.env_file))


if __name__ == "__main__":
    unittest.main()
