#!/usr/bin/env python3
"""Tests for fbsh.py - run with:  python3 test_fbsh.py

Starts a stand-in FRITZ!Box (real HTTP, both login schemes) and drives the
script through the same code paths the real box uses. No hardware, no network.
"""

import contextlib
import hashlib
import io
import json
import os
import secrets
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fbsh

PLUG = "08761 0000434"
THERMOSTAT = "08761 1048079"


class Box:
    """Minimal AHA server: login_sid.lua plus homeautoswitch.lua."""

    def __init__(self, pw="testpw", user="example_user"):
        self.pw = pw
        self.user = user
        self.md5_only = False
        self.block = 0
        self.sids = set()
        self.challenge = ""
        self.log = []
        self.devices = {
            "08761 0000434": {
                "name": "plug", "mask": 896, "product": "FRITZ!DECT 200", "state": 1,
                "power": 12345, "energy": 707, "voltage": 230252, "celsius": 285,
            },
            "08761 1048079": {
                "name": "thermostat", "mask": 320, "product": "FRITZ!DECT 301",
                "tist": 42, "tsoll": 42, "battery": 80,
            },
        }

    def start(self):
        box = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, status, body, ctype="text/plain"):
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/login_sid.lua":
                    self.send(200, box.session_xml(), "text/xml")
                elif parsed.path == "/webservices/homeautoswitch.lua":
                    query = dict(urllib.parse.parse_qsl(parsed.query))
                    box.log.append(query)
                    status, body, ctype = box.command(query)
                    self.send(status, body, ctype)
                else:
                    self.send(404, "not found")

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
                self.send(200, box.login(form), "text/xml")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def port(self):
        return self.server.server_address[1]

    # -- login ---------------------------------------------------------------
    def session_xml(self):
        if self.md5_only:
            self.challenge = secrets.token_hex(8)
        else:
            salt1, salt2 = secrets.token_hex(8), secrets.token_hex(8)
            self.challenge = f"2$1000${salt1}$1000${salt2}"
        return (f"<SessionInfo><SID>{fbsh.EMPTY_SID}</SID>"
                f"<Challenge>{self.challenge}</Challenge>"
                f"<BlockTime>{self.block}</BlockTime></SessionInfo>")

    def login(self, form):
        expected = fbsh.Box.response_for(self.challenge, self.pw)
        if form.get("response") == expected and form.get("username") == self.user:
            sid = secrets.token_hex(8)
            self.sids.add(sid)
            return f"<SessionInfo><SID>{sid}</SID><BlockTime>0</BlockTime></SessionInfo>"
        return (f"<SessionInfo><SID>{fbsh.EMPTY_SID}</SID>"
                f"<BlockTime>{self.block}</BlockTime></SessionInfo>")

    # -- commands ------------------------------------------------------------
    def device_xml(self, ain, d):
        tag = "<device "
        parts = [f'{tag}identifier="{ain}" id="1" functionbitmask="{d["mask"]}" '
                 f'fwversion="1.0" manufacturer="AVM" productname="{d["product"]}">',
                 f'<present>1</present><name>{d["name"]}</name>']
        if "state" in d:
            parts.append(f'<switch><state>{d["state"]}</state><mode>auto</mode></switch>')
        if "power" in d:
            parts.append("<powermeter>"
                         f'<power>{d["power"]}</power><energy>{d["energy"]}</energy>'
                         f'<voltage>{d["voltage"]}</voltage></powermeter>')
        if "celsius" in d:
            parts.append(f'<temperature><celsius>{d["celsius"]}</celsius></temperature>')
        if "tsoll" in d:
            parts.append("<hkr>"
                         f'<tist>{d["tist"]}</tist><tsoll>{d["tsoll"]}</tsoll>'
                         f'<komfort>44</komfort><absenk>34</absenk>'
                         f'<battery>{d["battery"]}</battery><batterylow>0</batterylow>'
                         "<windowopenactiv>0</windowopenactiv><errorcode>0</errorcode></hkr>")
        parts.append("</device>")
        return "".join(parts)

    def command(self, query):
        if query.get("sid") not in self.sids:
            return 403, "", "text/plain"
        cmd, ain = query.get("switchcmd"), query.get("ain")
        if cmd == "getdevicelistinfos":
            body = "".join(self.device_xml(a, d) for a, d in self.devices.items())
            return 200, f'<devicelist version="1">{body}</devicelist>', "text/xml"
        device = self.devices.get(ain)
        if device is None:
            return 400, "ain not found", "text/plain"
        if cmd == "setswitchon":
            device["state"] = 1
        elif cmd == "setswitchoff":
            device["state"] = 0
        elif cmd == "setswitchtoggle":
            device["state"] = 0 if device["state"] else 1
        elif cmd == "sethkrtsoll":
            device["tsoll"] = int(query.get("param", 0))
        elif cmd == "getswitchstate":
            return 200, str(device.get("state", "")), "text/plain"
        return 200, str(device.get("state", "")), "text/plain"


class FbshTest(unittest.TestCase):
    def setUp(self):
        self.box = Box().start()
        self.addCleanup(self.box.stop)

    def args(self, *argv):
        return list(argv) + ["--host", "127.0.0.1", "--port", str(self.box.port),
                             "--user", self.box.user, "--password", self.box.pw,
                             "--env-file", "/nonexistent"]

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fbsh.main(self.args(*argv))
        return code, out.getvalue(), err.getvalue()

    # -- login ---------------------------------------------------------------
    def test_pbkdf2_login(self):
        box = fbsh.Box({"host": "127.0.0.1", "port": self.box.port, "user": self.box.user,
                        "pw": self.box.pw, "scheme": "http", "verify": None, "ca": None,
                        "timeout": 5})
        sid = box.login()
        self.assertEqual(len(sid), 16)
        self.assertIn(sid, self.box.sids)

    def test_md5_login(self):
        self.box.md5_only = True
        box = fbsh.Box({"host": "127.0.0.1", "port": self.box.port, "user": self.box.user,
                        "pw": self.box.pw, "scheme": "http", "verify": None, "ca": None,
                        "timeout": 5})
        self.assertIn(box.login(), self.box.sids)

    def test_wrong_credentials(self):
        code, _, err = self.run_cli("devices")
        self.assertEqual(code, 0)
        args = self.args("devices")
        args[args.index("--password") + 1] = "falsch"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(fbsh.main(args), 3)

    def test_block_time(self):
        self.box.block = 120
        box = fbsh.Box({"host": "127.0.0.1", "port": self.box.port, "user": self.box.user,
                        "pw": "wrong", "scheme": "http", "verify": None, "ca": None,
                        "timeout": 5})
        with self.assertRaises(fbsh.LoginError) as caught:
            box.login()
        self.assertIn("120", str(caught.exception))

    # -- parsing and inventory ----------------------------------------------
    def test_devices_lists_kind_and_capabilities(self):
        code, out, _ = self.run_cli("devices")
        self.assertEqual(code, 0)
        self.assertIn("plug", out)
        self.assertIn("switch", out)
        self.assertIn("switch, power, temperature", out)
        self.assertIn("thermostat, temperature, battery", out)

    def test_kind_follows_the_functionbitmask(self):
        payload = json.loads(self.run_cli("devices", "--json")[1])
        by_name = {d["name"]: d for d in payload}
        self.assertEqual(by_name["plug"]["kind"], "switch", "bit 9 marks a socket")
        self.assertEqual(by_name["thermostat"]["kind"], "thermostat")

    def test_units_are_converted(self):
        code, out, _ = self.run_cli("devices", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        plug = next(d for d in payload if d["name"] == "plug")
        self.assertAlmostEqual(plug["temperature_c"], 28.5)    # 285 * 0.1
        self.assertAlmostEqual(plug["power"]["watts"], 12.345)  # 12345 mW
        self.assertAlmostEqual(plug["power"]["volts"], 230.252)
        self.assertEqual(plug["power"]["energy_wh"], 707)
        self.assertEqual(plug["capabilities"], ["switch", "power", "temperature"])

    def test_thermostat_values(self):
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        thermostat = next(d for d in json.loads(out) if d["name"] == "thermostat")
        self.assertAlmostEqual(thermostat["thermostat"]["actual_c"], 21.0)
        self.assertAlmostEqual(thermostat["thermostat"]["setpoint_c"], 21.0)
        self.assertEqual(thermostat["kind"], "thermostat")

    def test_status_table(self):
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("SETPOINT", out)
        self.assertIn("12.3 W", out)

    # -- control -------------------------------------------------------------
    def test_switch_off(self):
        code, out, _ = self.run_cli("switch", PLUG, "off")
        self.assertEqual(code, 0)
        self.assertIn("off", out)
        self.assertEqual(self.box.devices[PLUG]["state"], 0)

    def test_thermostat_setpoint_in_half_degrees(self):
        code, _, _ = self.run_cli("thermostat", THERMOSTAT, "--set", "21.5")
        self.assertEqual(code, 0)
        self.assertEqual(self.box.devices[THERMOSTAT]["tsoll"], 43)

    def test_thermostat_rejects_out_of_range(self):
        code, _, err = self.run_cli("thermostat", THERMOSTAT, "--set", "30")
        self.assertEqual(code, 1)
        self.assertIn("between 8 and 28", err)
        self.assertEqual(self.box.devices[THERMOSTAT]["tsoll"], 42, "value must stay unchanged")

    def test_set_temperature_clamps_at_the_protocol_limit(self):
        box = fbsh.Box({"host": "127.0.0.1", "port": self.box.port, "user": self.box.user,
                        "pw": self.box.pw, "scheme": "http", "verify": None, "ca": None,
                        "timeout": 5})
        box.set_temperature(THERMOSTAT, 40)     # protocol limit is 8..28
        self.assertEqual(self.box.devices[THERMOSTAT]["tsoll"], 56)
        box.set_temperature(THERMOSTAT, 3)
        self.assertEqual(self.box.devices[THERMOSTAT]["tsoll"], 16)

    def test_switch_on_thermostat_is_refused(self):
        code, _, err = self.run_cli("switch", THERMOSTAT, "on")
        self.assertEqual(code, 1)
        self.assertIn("not switchable", err)

    def test_unknown_device(self):
        code, _, err = self.run_cli("switch", "00:00:00-000", "on")
        self.assertEqual(code, 1)
        self.assertIn("no device with AIN", err)

    def test_session_is_renewed_after_403(self):
        code, _, _ = self.run_cli("devices")
        self.assertEqual(code, 0)
        self.box.sids.clear()          # the box forgot the session
        code, out, _ = self.run_cli_same_session()
        self.assertEqual(code, 0)

    def run_cli_same_session(self):
        """Run two commands through one Box instance to exercise the 403 retry."""
        box = fbsh.Box({"host": "127.0.0.1", "port": self.box.port, "user": self.box.user,
                        "pw": self.box.pw, "scheme": "http", "verify": None, "ca": None,
                        "timeout": 5})
        box.devices()
        self.box.sids.clear()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            devices = box.devices()
        self.assertEqual(len(devices), 2)
        return 0, out.getvalue(), ""


class EnvFileTest(unittest.TestCase):
    def write(self, text):
        import tempfile
        handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        handle.write(text)
        handle.close()
        return fbsh.Path(handle.name)

    def test_parse_env_file(self):
        path = self.write("# comment\nexport FRITZ_USER='example user'\n"
                          'FRITZ_PASSWORD="p#ss"\nFRITZ_HOST=box  # trailing\n')
        values = fbsh.parse_env(path)
        self.assertEqual(values["FRITZ_USER"], "example user")
        self.assertEqual(values["FRITZ_PASSWORD"], "p#ss")
        self.assertEqual(values["FRITZ_HOST"], "box")
        path.unlink()

    def test_cli_beats_env(self):
        os.environ["FRITZ_HOST"] = "from-env"
        self.addCleanup(os.environ.pop, "FRITZ_HOST", None)
        cli = fbsh.build_parser().parse_args(
            ["devices", "--host", "from-cli", "--password", "x", "--env-file", "/nonexistent"])
        self.assertEqual(fbsh.settings(cli)["host"], "from-cli")


if __name__ == "__main__":
    unittest.main(verbosity=1)
