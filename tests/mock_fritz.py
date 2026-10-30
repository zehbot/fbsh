"""A stand-in FRITZ!Box for tests: login_sid.lua plus homeautoswitch.lua.

It implements the same challenge-response login as the real box (PBKDF2 with
salt hex decoding, and the older MD5 scheme) and a few smart home devices, so
the client can be tested without any hardware.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EMPTY_SID = "0000000000000000"


def _default_devices() -> list[dict]:
    return [
        {
            "ain": "08761 0000434",
            "id": 17,
            "mask": 896,  # bit7 powermeter + bit8 temperature + bit9 switch
            "fw": "03.33",
            "product": "FRITZ!DECT 200",
            "name": "Kaffeemaschine",
            "present": 1,
            "switch": {"state": 1, "mode": "auto", "lock": 0, "devicelock": 0},
            "powermeter": {"power": 12345, "energy": 707, "voltage": 230252},
            "temperature": {"celsius": 285, "offset": 0},
        },
        {
            "ain": "08761 1048079",
            "id": 16,
            "mask": 320,  # bit6 hkr + bit8 temperature
            "fw": "04.90",
            "product": "FRITZ!DECT 301",
            "name": "Wohnzimmer",
            "present": 1,
            "hkr": {
                "tist": "42",
                "tsoll": "42",
                "komfort": "44",
                "absenk": "34",
                "battery": "80",
                "batterylow": "0",
                "windowopenactiv": "0",
                "boostactive": "0",
                "holidayactive": "0",
                "summeractive": "0",
                "errorcode": "0",
                "lock": "0",
                "endperiod": "1790000000",
                "tchange": "34",
            },
        },
        {
            "ain": "1F0A 0000123",
            "id": 21,
            "mask": 288,  # bit5 button + bit8 temperature
            "fw": "1.0",
            "product": "FRITZ!DECT 440",
            "name": "Flur Taster",
            "present": 0,
            "temperature": {"celsius": "", "offset": "0"},
            "battery": "12",
            "batterylow": "1",
        },
        {
            "ain": "65:3A:18-900",
            "id": 900,
            "mask": 512,
            "fw": "1.0",
            "product": "",
            "name": "Gruppe Wohnzimmer",
            "present": 1,
            "switch": {"state": 1, "mode": "auto"},
            "groupinfo": {"masterdeviceid": 0, "members": "16"},
        },
    ]


class MockBox:
    def __init__(self, password: str = "geheim", user: str = "fbsh", md5_only: bool = False):
        self.password = password
        self.user = user
        self.md5_only = md5_only
        self.blocktime = 0
        self.devices = _default_devices()
        self.valid_sids: set[str] = set()
        self.login_requests = 0
        self.commands: list[tuple[str, dict]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> "MockBox":
        box = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the test output clean
                pass

            def _send(self, status: int, body: str, content_type: str = "text/plain") -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                query = dict(urllib.parse.parse_qsl(parsed.query))
                if parsed.path == "/login_sid.lua":
                    box.login_requests += 1
                    self._send(200, box.session_info(), "text/xml")
                    return
                if parsed.path == "/webservices/homeautoswitch.lua":
                    box.commands.append((query.get("switchcmd", ""), query))
                    status, body, ctype = box.handle_command(query)
                    self._send(status, body, ctype)
                    return
                self._send(404, "not found")

            def do_POST(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
                if parsed.path == "/login_sid.lua":
                    box.login_requests += 1
                    self._send(200, box.handle_login(form), "text/xml")
                    return
                self._send(404, "not found")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    # -- login ---------------------------------------------------------------
    def new_challenge(self) -> str:
        if self.md5_only:
            return secrets.token_hex(8)
        salt1 = secrets.token_hex(8)
        salt2 = secrets.token_hex(8)
        return f"2$1000${salt1}$1000${salt2}"

    def session_info(self) -> str:
        challenge = self.new_challenge()
        self._challenge = challenge
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<SessionInfo>"
            f"<SID>{EMPTY_SID}</SID>"
            f"<Challenge>{challenge}</Challenge>"
            f"<BlockTime>{self.blocktime}</BlockTime>"
            "</SessionInfo>"
        )

    def expected_response(self, challenge: str) -> str:
        if challenge.startswith("2$"):
            _, it1, salt1, it2, salt2 = challenge.split("$")
            static = hashlib.pbkdf2_hmac(
                "sha256", self.password.encode(), bytes.fromhex(salt1), int(it1)
            )
            dynamic = hashlib.pbkdf2_hmac(
                "sha256", static, bytes.fromhex(salt2), int(it2)
            )
            return f"{salt2}${dynamic.hex()}"
        digest = hashlib.md5(f"{challenge}-{self.password}".encode("utf-16-le"))
        return f"{challenge}-{digest.hexdigest()}"

    def handle_login(self, form: dict) -> str:
        challenge = getattr(self, "_challenge", "")
        expected = self.expected_response(challenge)
        if form.get("response") == expected and form.get("username", "") == self.user:
            sid = secrets.token_hex(8)
            self.valid_sids.add(sid)
            return f"<SessionInfo><SID>{sid}</SID><BlockTime>0</BlockTime></SessionInfo>"
        return (
            "<SessionInfo>"
            f"<SID>{EMPTY_SID}</SID>"
            f"<BlockTime>{self.blocktime}</BlockTime>"
            "</SessionInfo>"
        )

    def expire_sessions(self) -> None:
        self.valid_sids.clear()

    # -- commands ------------------------------------------------------------
    def device_xml(self, device: dict) -> str:
        attrs = (
            f'identifier="{device["ain"]}" id="{device["id"]}" '
            f'functionbitmask="{device["mask"]}" fwversion="{device["fw"]}" '
            f'manufacturer="AVM" productname="{device["product"]}"'
        )
        tag = "group" if "groupinfo" in device else "device"
        parts = [f"<{tag} {attrs}>"]
        parts.append(f'<present>{device.get("present", 0)}</present>')
        parts.append(f'<name>{device["name"]}</name>')
        if "battery" in device:
            parts.append(f'<battery>{device["battery"]}</battery>')
        if "batterylow" in device:
            parts.append(f'<batterylow>{device["batterylow"]}</batterylow>')
        if "switch" in device:
            switch = device["switch"]
            parts.append("<switch>")
            parts.append(f'<state>{switch.get("state", "")}</state>')
            parts.append(f'<mode>{switch.get("mode", "")}</mode>')
            if "lock" in switch:
                parts.append(f'<lock>{switch["lock"]}</lock>')
            if "devicelock" in switch:
                parts.append(f'<devicelock>{switch["devicelock"]}</devicelock>')
            parts.append("</switch>")
        if "powermeter" in device:
            meter = device["powermeter"]
            parts.append(
                "<powermeter>"
                f'<power>{meter["power"]}</power>'
                f'<energy>{meter["energy"]}</energy>'
                f'<voltage>{meter["voltage"]}</voltage>'
                "</powermeter>"
            )
        if "temperature" in device:
            temperature = device["temperature"]
            parts.append(
                "<temperature>"
                f'<celsius>{temperature["celsius"]}</celsius>'
                f'<offset>{temperature["offset"]}</offset>'
                "</temperature>"
            )
        if "hkr" in device:
            hkr = device["hkr"]
            parts.append("<hkr>")
            for key in ("tist", "tsoll", "komfort", "absenk", "battery", "batterylow",
                        "windowopenactiv", "boostactive", "holidayactive", "summeractive",
                        "errorcode", "lock"):
                parts.append(f"<{key}>{hkr.get(key, '')}</{key}>")
            parts.append(
                f'<nextchange><endperiod>{hkr.get("endperiod", 0)}</endperiod>'
                f'<tchange>{hkr.get("tchange", 0)}</tchange></nextchange>'
            )
            parts.append("</hkr>")
        if "groupinfo" in device:
            info = device["groupinfo"]
            parts.append(
                "<groupinfo>"
                f'<masterdeviceid>{info["masterdeviceid"]}</masterdeviceid>'
                f'<members>{info["members"]}</members>'
                "</groupinfo>"
            )
        parts.append(f"</{tag}>")
        return "".join(parts)

    def devicelist_xml(self) -> str:
        body = "".join(self.device_xml(device) for device in self.devices)
        return f'<devicelist version="1">{body}</devicelist>'

    def find(self, ain: str) -> dict | None:
        wanted = (ain or "").replace(" ", "")
        for device in self.devices:
            if device["ain"].replace(" ", "") == wanted:
                return device
        return None

    def handle_command(self, query: dict) -> tuple[int, str, str]:
        if query.get("sid") not in self.valid_sids:
            return 403, "", "text/plain"

        command = query.get("switchcmd", "")
        ain = query.get("ain")

        if command == "getdevicelistinfos":
            return 200, self.devicelist_xml(), "text/xml"

        if command == "getswitchlist":
            return 200, ",".join(
                d["ain"] for d in self.devices if "switch" in d
            ), "text/plain"

        device = self.find(ain or "")
        if device is None and command not in ("getdevicelistinfos",):
            return 400, "ain not found", "text/plain"

        assert device is not None
        if command == "getswitchstate":
            return 200, str(device.get("switch", {}).get("state", "")), "text/plain"
        if command in ("setswitchon", "setswitchoff", "setswitchtoggle"):
            switch = device.setdefault("switch", {"state": 0, "mode": "manuell"})
            if command == "setswitchon":
                switch["state"] = 1
            elif command == "setswitchoff":
                switch["state"] = 0
            else:
                switch["state"] = 0 if switch.get("state") else 1
            return 200, str(switch["state"]), "text/plain"
        if command == "getswitchpower":
            return 200, str(device.get("powermeter", {}).get("power", "inval")), "text/plain"
        if command == "getswitchenergy":
            return 200, str(device.get("powermeter", {}).get("energy", "inval")), "text/plain"
        if command == "gettemperature":
            return 200, str(device.get("temperature", {}).get("celsius", "inval")), "text/plain"
        if command == "gethkrtsoll":
            return 200, str(device.get("hkr", {}).get("tsoll", "inval")), "text/plain"
        if command == "sethkrtsoll":
            param = query.get("param", "")
            if not param.isdigit() or not 16 <= int(param) <= 56:
                if param not in ("253", "254"):
                    return 200, "inval", "text/plain"
            device.setdefault("hkr", {})["tsoll"] = str(param)
            return 200, str(param), "text/plain"
        if command == "setsimpleonoff":
            switch = device.setdefault("switch", {"state": 0})
            switch["state"] = 1 if query.get("onoff") == "1" else 0
            return 200, "", "text/plain"
        if command == "setlevelpercentage":
            device["level"] = query.get("level", "0")
            return 200, "", "text/plain"
        if command == "getbasicdevicestats":
            return 200, "<devicestats></devicestats>", "text/xml"

        return 400, f"unknown switchcmd {command}", "text/plain"
