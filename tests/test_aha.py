"""End-to-end tests: client and CLI against the mock box."""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest

from fbsh.aha import Aha, AhaError
from fbsh.cli import main
from fbsh.session import Session
from tests.mock_fritz import MockBox

PLUG = "08761 0000434"
THERMOSTAT = "08761 1048079"
OFFLINE_BUTTON = "1F0A 0000123"


class AhaTest(unittest.TestCase):
    def setUp(self):
        self.box = MockBox().start()
        self.addCleanup(self.box.stop)
        session = Session(host="127.0.0.1", port=self.box.port,
                          password="geheim", user="example_user", timeout=5)
        self.aha = Aha(session)

    def test_device_list(self):
        devices = self.aha.device_list()
        self.assertEqual(len(devices), 4)
        self.assertEqual(self.aha.device(PLUG).name, "Kaffeemaschine")

    def test_switch_off_and_on(self):
        self.assertIs(self.aha.switch_state(PLUG), True)
        self.assertIs(self.aha.switch_off(PLUG), False)
        self.assertIs(self.aha.switch_state(PLUG), False)
        self.assertIs(self.aha.switch_on(PLUG), True)

    def test_switch_toggle(self):
        self.assertIs(self.aha.switch_toggle(PLUG), False)
        self.assertIs(self.aha.switch_toggle(PLUG), True)

    def test_power_and_energy(self):
        self.assertAlmostEqual(self.aha.power(PLUG), 12.345)
        self.assertEqual(self.aha.energy(PLUG), 707)

    def test_temperature(self):
        self.assertAlmostEqual(self.aha.temperature(PLUG), 28.5)

    def test_set_temperature_sends_half_degrees(self):
        self.aha.set_temperature(THERMOSTAT, 21.5)
        command, query = self.box.commands[-1]
        self.assertEqual(command, "sethkrtsoll")
        self.assertEqual(query["param"], "43", "21.5 C is 43 half-degree steps")
        self.assertEqual(self.aha.setpoint(THERMOSTAT), "43")

    def test_set_temperature_clamps(self):
        self.aha.set_temperature(THERMOSTAT, 30)
        self.assertEqual(self.box.commands[-1][1]["param"], "56")
        self.aha.set_temperature(THERMOSTAT, 4)
        self.assertEqual(self.box.commands[-1][1]["param"], "16")

    def test_thermostat_off_and_on(self):
        self.aha.set_temperature_off(THERMOSTAT)
        self.assertEqual(self.box.commands[-1][1]["param"], "253")
        self.aha.set_temperature_on(THERMOSTAT)
        self.assertEqual(self.box.commands[-1][1]["param"], "254")

    def test_relogin_after_session_loss(self):
        self.aha.device_list()
        self.box.expire_sessions()
        devices = self.aha.device_list()  # 403 -> renewed sid -> retry
        self.assertEqual(len(devices), 4)

    def test_unknown_device_is_reported(self):
        with self.assertRaises(AhaError):
            self.aha.device("00:00:00-000")

    def test_unknown_command_is_reported(self):
        with self.assertRaises(AhaError):
            self.aha.call("getnonsense", PLUG)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.box = MockBox().start()
        self.addCleanup(self.box.stop)
        self.connection = [
            "--host", "127.0.0.1",
            "--port", str(self.box.port),
            "--user", "example_user",
            "--password", "geheim",
            "--env-file", "/nonexistent",
        ]
        self._env = os.environ.pop("FRITZ_PASSWORD", None)

    def tearDown(self):
        if self._env is not None:
            os.environ["FRITZ_PASSWORD"] = self._env

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args) + self.connection)
        return code, out.getvalue(), err.getvalue()

    def test_devices_lists_kind_and_capabilities(self):
        code, out, _ = self.run_cli("devices")
        self.assertEqual(code, 0)
        self.assertIn("DATA", out)
        self.assertIn("Kaffeemaschine", out)
        self.assertIn("switch, power, temperature", out)
        self.assertIn("thermostat, temperature, battery", out)

    def test_status_shows_values(self):
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("SETPOINT", out)
        self.assertIn("°C", out)
        self.assertIn("28.5", out)

    def test_devices_json(self):
        code, out, _ = self.run_cli("devices", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        names = {entry["name"] for entry in payload}
        self.assertIn("Kaffeemaschine", names)
        plug = next(entry for entry in payload if entry["name"] == "Kaffeemaschine")
        self.assertEqual(plug["kind"], "switch")
        self.assertEqual(plug["capabilities"], ["switch", "power", "temperature"])
        self.assertAlmostEqual(plug["temperature_c"], 28.5)
        self.assertEqual(plug["power"]["energy_wh"], 707)

    def test_switch_command(self):
        code, out, _ = self.run_cli("switch", PLUG, "off")
        self.assertEqual(code, 0)
        self.assertIn("off", out)
        self.assertIs(self.aha_state(), False)

    def aha_state(self):
        session = Session(host="127.0.0.1", port=self.box.port,
                          password="geheim", user="example_user", timeout=5)
        return Aha(session).switch_state(PLUG)

    def test_thermostat_set(self):
        code, out, _ = self.run_cli("thermostat", THERMOSTAT, "--set", "19.5")
        self.assertEqual(code, 0)
        self.assertIn("19.5", out)
        setpoints = [q["param"] for cmd, q in self.box.commands if cmd == "sethkrtsoll"]
        self.assertEqual(setpoints, ["39"], "19.5 C is 39 half-degree steps")

    def test_thermostat_rejects_out_of_range(self):
        code, _, err = self.run_cli("thermostat", THERMOSTAT, "--set", "31")
        self.assertEqual(code, 1)
        self.assertIn("between 8 and 28", err)

    def test_switch_on_thermostat_is_refused(self):
        code, _, err = self.run_cli("switch", THERMOSTAT, "on")
        self.assertEqual(code, 1)
        self.assertIn("not switchable", err)

    def test_watch_prints_initial_state(self):
        code, out, _ = self.run_cli("watch", "--interval", "0.01", "--count", "1")
        self.assertEqual(code, 0)
        self.assertIn("watching 4 devices", out)
        self.assertIn("Kaffeemaschine", out)

    def test_missing_password_is_a_config_error(self):
        args = [a for a in self.connection]
        args[args.index("--password") + 1] = ""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["devices"] + args)
        self.assertEqual(code, 2)
        self.assertIn("no credentials configured", err.getvalue())


if __name__ == "__main__":
    unittest.main()
