#!/usr/bin/env python3
"""fbsh - FRITZ!Box smart home over the AHA-HTTP interface.

One file, standard library only, nothing to install:

    python fbsh.py devices            # what exists and what it can deliver
    python fbsh.py status --json      # current values, machine readable
    python fbsh.py switch AIN on
    python fbsh.py thermostat AIN --set 21.5
    python fbsh.py raw getswitchlist

Credentials come from private/.env, ./env, or the environment (FRITZ_HOST,
FRITZ_USER, FRITZ_PASSWORD, FRITZ_SCHEME, FRITZ_PORT, FRITZ_VERIFY_TLS,
FRITZ_CA_FILE). The script only reads; it writes nothing and installs nothing.

Exit codes: 0 ok, 1 device/command error, 2 usage or config, 3 login/network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

LOGIN = "/login_sid.lua"
LOGIN_V2 = "/login_sid.lua?version=2"
AHA = "/webservices/homeautoswitch.lua"
EMPTY_SID = "0000000000000000"

# functionbitmask bits (AVM AHA-HTTP-API, section 3.2)
B_ALARM, B_BUTTON, B_HKR, B_METER = 4, 5, 6, 7
B_TEMP, B_SWITCH, B_ONOFF, B_LEVEL = 8, 9, 15, 16
B_COLOR, B_BLIND, B_HUMIDITY = 17, 18, 20
HKR_ON, HKR_OFF = 254, 253


# --- configuration -----------------------------------------------------------------
def parse_env(path: Path) -> dict:
    """Parse KEY=value lines; quotes, `export`, comments. Never sourced."""
    out = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key.strip().upper()] = value
    return out


def find_env_file(explicit: str | None) -> Path | None:
    candidates = [explicit, os.environ.get("FBSH_ENV_FILE"), "private/.env", ".env"]
    for candidate in candidates:
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file():
                return path
    return None


def settings(cli) -> dict:
    path = find_env_file(getattr(cli, "env_file", None))
    if path is not None and not os.access(path, os.R_OK):
        raise ConfigError(f"cannot read {path}")
    values = parse_env(path) if path else {}

    def pick(attr: str, key: str, default=None):
        given = getattr(cli, attr, None)
        if given not in (None, ""):
            return given
        return os.environ.get(key) or values.get(key) or default

    cfg = {
        "host": pick("host", "FRITZ_HOST", "fritz.box"),
        "user": pick("user", "FRITZ_USER", ""),
        "pw": pick("password", "FRITZ_PASSWORD", ""),
        "scheme": pick("scheme", "FRITZ_SCHEME", "http"),
        "port": pick("port", "FRITZ_PORT"),
        "verify": pick("verify_tls", "FRITZ_VERIFY_TLS"),
        "ca": pick("ca_file", "FRITZ_CA_FILE"),
        "timeout": getattr(cli, "timeout", 10.0),
        "influx_url": pick("influx_url", "INFLUX_URL"),
        "influx_org": pick("influx_org", "INFLUX_ORG"),
        "influx_bucket": pick("influx_bucket", "INFLUX_BUCKET"),
        "influx_token": pick("influx_token", "INFLUX_TOKEN"),
        "influx_insecure": pick("influx_insecure", "INFLUX_INSECURE"),
    }
    if not cfg["pw"]:
        raise ConfigError(
            "no credentials - set FRITZ_PASSWORD, use --password, "
            "or copy .env.example to private/.env"
        )
    if cfg["port"]:
        cfg["port"] = int(cfg["port"])
    return cfg


# --- box access --------------------------------------------------------------------
class LoginError(Exception):
    pass


class ConfigError(Exception):
    pass


class Box:
    """One FRITZ!Box: login, SID handling and the AHA commands."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.sid = None
        self.ctx = None
        if cfg["scheme"] == "https":
            if str(cfg["verify"] or "").lower() in ("1", "true", "yes"):
                self.ctx = ssl.create_default_context(cafile=cfg["ca"])
            else:
                self.ctx = ssl._create_unverified_context()  # noqa: SLF001

    def url(self, path: str, params: dict | None = None) -> str:
        host = self.cfg["host"] if not self.cfg["port"] else f"{self.cfg['host']}:{self.cfg['port']}"
        url = f"{self.cfg['scheme']}://{host}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        return url

    def request(self, path, params=None, data=None):
        body = urllib.parse.urlencode(data).encode() if data else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if data else {}
        req = urllib.request.Request(self.url(path, params), data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg["timeout"], context=self.ctx) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode("utf-8", "replace")
        except urllib.error.URLError as err:
            raise LoginError(f"cannot reach {self.cfg['host']}: {err.reason}") from err

    @staticmethod
    def response_for(challenge: str, pw: str) -> str:
        """PBKDF2 (FRITZ!OS >= 7.24) or the old MD5 challenge-response."""
        if challenge.startswith("2$"):
            _, it1, salt1, it2, salt2 = challenge.split("$")
            h1 = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt1), int(it1))
            h2 = hashlib.pbkdf2_hmac("sha256", h1, bytes.fromhex(salt2), int(it2))
            return f"{salt2}${h2.hex()}"
        digest = hashlib.md5(f"{challenge}-{pw}".encode("utf-16-le"))
        return f"{challenge}-{digest.hexdigest()}"

    def login(self) -> str:
        status, text = self.request(LOGIN_V2)
        info = ET.fromstring(text)
        sid = (info.findtext("SID") or "").strip()
        if sid and sid != EMPTY_SID:
            self.sid = sid
            return sid
        block = int(info.findtext("BlockTime") or 0)
        if block:
            raise LoginError(f"login blocked for {block} s - wait or log in via the web UI")

        answer = self.response_for(info.findtext("Challenge") or "", self.cfg["pw"])
        _, text = self.request(LOGIN, data={"username": self.cfg["user"], "response": answer})
        info = ET.fromstring(text)
        sid = (info.findtext("SID") or "").strip()
        if sid and sid != EMPTY_SID:
            self.sid = sid
            return sid
        block = int(info.findtext("BlockTime") or 0)
        if block:
            raise LoginError(f"login blocked for {block} s")
        raise LoginError("the box rejected the user/password combination")

    def call(self, switchcmd: str, ain: str | None = None, **params) -> str:
        """Run one command; renew the SID once when the box forgot it (403)."""
        for attempt in (1, 2):
            if not self.sid:
                self.login()
            query = {"switchcmd": switchcmd, "sid": self.sid}
            if ain:
                query["ain"] = ain
            query.update({k: v for k, v in params.items() if v is not None})
            status, body = self.request(AHA, query)
            if status == 403 and attempt == 1:
                self.sid = None
                continue
            if status != 200:
                raise RuntimeError(f"{switchcmd}: HTTP {status} {body.strip()[:100]}")
            return body.strip()
        raise RuntimeError(f"{switchcmd}: session could not be renewed")

    # -- convenience ----------------------------------------------------------------
    def devices(self) -> list[dict]:
        return parse_devices(self.call("getdevicelistinfos"))

    def device(self, key: str) -> dict:
        """Find a device by AIN or by name - both are accepted everywhere."""
        devices = self.devices()
        wanted = key.replace(" ", "").lower()
        for device in devices:
            if device["ain"].replace(" ", "").lower() == wanted:
                return device
        for device in devices:
            if device["name"].lower() == key.lower():
                return device
        known = ", ".join(sorted(d["name"] or d["ain"] for d in devices))
        raise RuntimeError(f"no device with AIN or name {key!r}; known: {known}")

    def switch(self, ain: str, action: str) -> bool | None:
        if action == "state":
            return number(self.call("getswitchstate", ain)) == 1
        result = self.call({"on": "setswitchon", "off": "setswitchoff",
                            "toggle": "setswitchtoggle"}[action], ain)
        return number(result) == 1

    def set_temperature(self, ain: str, celsius: float) -> None:
        steps = max(16, min(56, int(round(celsius * 2))))
        self.call("sethkrtsoll", ain, param=steps)

    def set_temperature_mode(self, ain: str, mode: str) -> None:
        self.call("sethkrtsoll", ain, param=HKR_ON if mode == "on" else HKR_OFF)


# --- XML parsing -------------------------------------------------------------------
def child(node, name):
    for item in node:
        if item.tag.rsplit("}", 1)[-1] == name:
            return item
    return None


def text(node, name):
    item = child(node, name)
    return None if item is None else (item.text or "").strip()


def number(value) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if not value or value == "inval":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def scaled(value, factor):
    raw = number(value)
    return None if raw is None else raw * factor


def flag(value):
    raw = number(value)
    return None if raw is None else bool(raw)


def parse_devices(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    devices = []
    for node in root:
        if node.tag.rsplit("}", 1)[-1] not in ("device", "group"):
            continue
        mask = int(node.get("functionbitmask") or 0)
        device = {
            "ain": (node.get("identifier") or "").strip(),
            "name": (text(node, "name") or "").strip(),
            "product": node.get("productname") or "",
            "firmware": node.get("fwversion") or "",
            "mask": mask,
            "kind": kind_of(mask, node.tag.rsplit("}", 1)[-1] == "group"),
            "present": flag(text(node, "present")),
            "battery": number(text(node, "battery")),
            "battery_low": flag(text(node, "batterylow")),
        }

        node_switch = child(node, "switch")
        if node_switch is not None:
            device["switch"] = {
                "state": flag(text(node_switch, "state")),
                "mode": text(node_switch, "mode") or None,
                "locked": flag(text(node_switch, "lock")),
            }
        node_meter = child(node, "powermeter")
        if node_meter is not None:
            device["power"] = {
                "watts": scaled(text(node_meter, "power"), 0.001),
                "energy_wh": number(text(node_meter, "energy")),
                "volts": scaled(text(node_meter, "voltage"), 0.001),
            }
        node_temp = child(node, "temperature")
        if node_temp is not None:
            device["temperature_c"] = scaled(text(node_temp, "celsius"), 0.1)
        node_hkr = child(node, "hkr")
        if node_hkr is not None:
            def hkr(name):
                return scaled(text(node_hkr, name), 0.5)
            device["thermostat"] = {
                "actual_c": hkr("tist"),
                "setpoint_c": hkr("tsoll"),
                "setpoint_mode": {HKR_ON: "on", HKR_OFF: "off"}.get(number(text(node_hkr, "tsoll"))),
                "comfort_c": hkr("komfort"),
                "eco_c": hkr("absenk"),
                "window_open": flag(text(node_hkr, "windowopenactiv")),
                "boost": flag(text(node_hkr, "boostactive")),
                "error": number(text(node_hkr, "errorcode")),
                "battery": number(text(node_hkr, "battery")),
            }
        node_level = child(node, "levelcontrol")
        if node_level is not None:
            device["level"] = number(text(node_level, "levelpercentage"))
        node_alert = child(node, "alert")
        if node_alert is not None:
            device["alert"] = number(text(node_alert, "state"))
        devices.append(device)
    return devices


def kind_of(mask: int, is_group: bool) -> str:
    if is_group:
        return "group"
    if mask & (1 << B_HKR):
        return "thermostat"
    if mask & (1 << B_BLIND):
        return "blind"
    if mask & (1 << B_COLOR):
        return "color-light"
    if mask & (1 << B_LEVEL):
        return "dimmable-light"
    if mask & (1 << B_SWITCH) or mask & (1 << B_ONOFF):
        return "switch"
    if mask & (1 << B_BUTTON):
        return "button"
    if mask & (1 << B_ALARM):
        return "sensor"
    if mask & (1 << B_TEMP):
        return "temperature-sensor"
    return "device"


def capabilities(device: dict) -> list[str]:
    mask = device["mask"]
    caps = []
    if mask & (1 << B_ONOFF) or mask & (1 << B_SWITCH):
        caps.append("switch")
    if mask & (1 << B_HKR):
        caps.append("thermostat")
    if mask & (1 << B_BLIND):
        caps.append("blind")
    if mask & (1 << B_LEVEL):
        caps.append("level")
    if mask & (1 << B_COLOR):
        caps.append("color")
    if mask & (1 << B_METER):
        caps.append("power")
    if mask & (1 << B_TEMP) or mask & (1 << B_HKR):
        caps.append("temperature")
    if mask & (1 << B_HUMIDITY):
        caps.append("humidity")
    if mask & (1 << B_BUTTON):
        caps.append("button")
    if mask & (1 << B_ALARM):
        caps.append("alarm")
    if device["battery"] is not None or (device.get("thermostat") or {}).get("battery") is not None:
        caps.append("battery")
    return caps


# --- influxdb ----------------------------------------------------------------------
def influx_lines(devices: list[dict], timestamp_ns: int) -> list[str]:
    """Line protocol for every non-group device that has at least one value."""
    lines = []
    for device in devices:
        if device["kind"] == "group":
            continue                      # aggregates duplicate their members
        fields = []
        temp = device.get("temperature_c")
        thermostat = device.get("thermostat") or {}
        if temp is None:
            temp = thermostat.get("actual_c")
        if temp is not None:
            fields.append(f"temp={repr(float(temp))}")
        meter = device.get("power") or {}
        if meter.get("watts") is not None:
            fields.append(f"power={repr(float(meter['watts']))}")
        battery = device.get("battery")
        if battery is None:
            battery = thermostat.get("battery")
        if battery is not None:
            fields.append(f"battery={repr(float(battery))}")
        if not fields:
            continue
        measurement = device["name"] or device["ain"]
        measurement = measurement.replace(",", "\\,").replace(" ", "\\ ")
        lines.append(f"{measurement} {','.join(fields)} {timestamp_ns}")
    return lines


def influx_write(cfg: dict, body: str) -> tuple[int, str]:
    url = f"{cfg['influx_url'].rstrip('/')}/api/v2/write"
    query = urllib.parse.urlencode({"org": cfg["influx_org"], "bucket": cfg["influx_bucket"],
                                    "precision": "ns"})
    ctx = ssl.create_default_context()
    if str(cfg.get("influx_insecure") or "").lower() in ("1", "true", "yes"):
        ctx = ssl._create_unverified_context()  # noqa: SLF001
    req = urllib.request.Request(
        f"{url}?{query}", data=body.encode(), method="POST",
        headers={"Authorization": f"Token {cfg['influx_token']}",
                 "Content-Type": "text/plain; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"], context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


def collect(args, box: Box, cfg: dict) -> int:
    missing = [name for key, name in (("influx_url", "INFLUX_URL"), ("influx_org", "INFLUX_ORG"),
                                      ("influx_bucket", "INFLUX_BUCKET"), ("influx_token", "INFLUX_TOKEN"))
               if not cfg.get(key)]
    if missing and not args.dry_run:
        raise ConfigError(f"{', '.join(missing)} not set - add it to private/.env")

    lines = influx_lines(box.devices(), time.time_ns())
    if not lines:
        raise RuntimeError("no device delivered a value")
    body = "\n".join(lines) + "\n"

    if args.dry_run:
        print(body, end="")
        note = f"# {len(lines)} lines, nothing written"
        if missing:
            note += f" (config incomplete: {', '.join(missing)})"
        print(note, file=sys.stderr)
        return 0

    status, text = influx_write(cfg, body)
    if status not in (200, 204):
        raise RuntimeError(f"influx rejected the write: HTTP {status} {text.strip()[:200]}")
    print(f"wrote {len(lines)} points to {cfg['influx_bucket']}")
    return 0


# --- output ------------------------------------------------------------------------
def table_inventory(devices: list[dict]) -> None:
    print(f"{'NAME':<26}  {'KIND':<18}  DATA")
    print("-" * 82)
    for device in devices:
        name = (device["name"] or device["ain"])[:26]
        print(f"{name:<26}  {device['kind']:<18}  {', '.join(capabilities(device)) or '-'}")


def line_status(device: dict) -> str:
    node_switch = device.get("switch") or {}
    thermostat = device.get("thermostat") or {}
    state = "" if node_switch.get("state") is None else ("on" if node_switch["state"] else "off")
    setpoint = ""
    if thermostat.get("setpoint_c") is not None:
        setpoint = f"{thermostat['setpoint_c']:.1f}°C set"
    elif thermostat.get("setpoint_mode"):
        setpoint = thermostat["setpoint_mode"]
    current = device.get("temperature_c")
    if current is None:
        current = thermostat.get("actual_c")
    temperature = "" if current is None else f"{current:.1f}°C now"
    meter = device.get("power") or {}
    power = "" if meter.get("watts") is None else f"{meter['watts']:.1f} W"
    line = (f"{(device['name'] or device['ain'])[:24]:<24}  {device['kind']:<17}  {state:<5}  "
            f"{setpoint:<11}  {temperature:<11}  {power:>8}")
    if device["present"] is False:
        line += "  OFFLINE"
    return line.rstrip()


def table_status(devices: list[dict]) -> None:
    print(f"{'NAME':<24}  {'KIND':<17}  {'STATE':<5}  {'SETPOINT':<11}  {'TEMP':<11}  {'POWER':>8}")
    print("-" * 90)
    for device in devices:
        print(line_status(device))
        if device.get("battery_low"):
            print(f"  ! battery low ({device.get('battery')}%) on {device['name'] or device['ain']}")


def as_json(devices: list[dict]) -> str:
    return json.dumps([dict(device, capabilities=capabilities(device)) for device in devices],
                      indent=2, ensure_ascii=False)


# --- command line ------------------------------------------------------------------
def add_connection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", help="box name or IP (default fritz.box)")
    parser.add_argument("--user", help="FRITZ!Box user with the Smart Home right")
    parser.add_argument("--password", help="prefer FRITZ_PASSWORD or private/.env")
    parser.add_argument("--scheme", choices=["http", "https"])
    parser.add_argument("--port", type=int)
    parser.add_argument("--verify-tls")
    parser.add_argument("--ca-file")
    parser.add_argument("--env-file", help="dotenv file (default private/.env, then .env)")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--json", action="store_true", help="machine readable output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fbsh.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (("devices", "list devices with kind and capabilities"),
                            ("status", "list devices with current values")):
        cmd = sub.add_parser(name, help=help_text)
        add_connection(cmd)
        cmd.add_argument("--ain", metavar="AIN|NAME", help="only this device")

    switch = sub.add_parser("switch", help="switch a socket or light")
    add_connection(switch)
    switch.add_argument("ain", metavar="AIN|NAME")
    switch.add_argument("action", choices=["on", "off", "toggle", "state"])

    thermostat = sub.add_parser("thermostat", help="read or set a radiator thermostat")
    add_connection(thermostat)
    thermostat.add_argument("ain", metavar="AIN|NAME")
    thermostat.add_argument("--set", dest="setpoint", type=float, metavar="C")
    thermostat.add_argument("--on", action="store_true")
    thermostat.add_argument("--off", action="store_true")

    collect_cmd = sub.add_parser("collect", help="read all devices and write them to InfluxDB")
    add_connection(collect_cmd)
    collect_cmd.add_argument("--dry-run", action="store_true",
                             help="print the line protocol instead of writing")

    raw = sub.add_parser("raw", help="send a raw switchcmd")
    add_connection(raw)
    raw.add_argument("switchcmd")
    raw.add_argument("--ain")
    raw.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    return parser


def select(box: Box, ain: str | None) -> list[dict]:
    devices = box.devices()
    return [box.device(ain)] if ain else devices


def run(args) -> int:
    box = Box(settings(args))

    if args.command == "devices":
        devices = select(box, args.ain)
        print(as_json(devices) if args.json else "", end="")
        if not args.json:
            table_inventory(devices)
        return 0

    if args.command == "status":
        devices = select(box, args.ain)
        print(as_json(devices) if args.json else "", end="")
        if not args.json:
            table_status(devices)
        return 0

    if args.command == "switch":
        device = box.device(args.ain)
        if not (device["mask"] & (1 << B_ONOFF) or device["mask"] & (1 << B_SWITCH)):
            raise RuntimeError(f"{device['name'] or device['ain']} is not switchable ({device['kind']})")
        state = box.switch(device["ain"], args.action)
        if args.json:
            print(json.dumps({"ain": device["ain"], "name": device["name"], "state": state}))
        else:
            print(f"{device['name'] or device['ain']}: {'on' if state else 'off'}")
        return 0

    if args.command == "thermostat":
        device = box.device(args.ain)
        if not device["mask"] & (1 << B_HKR):
            raise RuntimeError(f"{device['name'] or device['ain']} is not a thermostat")
        if args.setpoint is not None:
            if not 8 <= args.setpoint <= 28:
                raise RuntimeError("setpoint must be between 8 and 28 °C")
            box.set_temperature(device["ain"], args.setpoint)
            device = box.device(device["ain"])
        if args.on or args.off:
            box.set_temperature_mode(device["ain"], "on" if args.on else "off")
            device = box.device(device["ain"])
        thermostat = device.get("thermostat") or {}
        if args.json:
            print(json.dumps({"ain": device["ain"], "name": device["name"], **thermostat},
                             ensure_ascii=False))
        else:
            setpoint = (f"{thermostat['setpoint_c']:.1f} °C"
                        if thermostat.get("setpoint_c") is not None
                        else (thermostat.get("setpoint_mode") or "unknown"))
            actual = (f"{thermostat['actual_c']:.1f} °C"
                      if thermostat.get("actual_c") is not None else "unknown")
            print(f"{device['name'] or device['ain']}: set {setpoint}, actual {actual}")
        return 0

    if args.command == "collect":
        return collect(args, box, settings(args))

    if args.command == "raw":
        params = dict(item.split("=", 1) for item in args.param if "=" in item)
        print(box.call(args.switchcmd, args.ain, **params))
        return 0

    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except ConfigError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    except LoginError as err:
        print(f"error: {err}", file=sys.stderr)
        return 3
    except RuntimeError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
