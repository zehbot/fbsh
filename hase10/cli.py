"""Command line interface: hase10 devices | switch | thermostat | watch | raw."""

from __future__ import annotations

import argparse
import json
import sys
import time

from .aha import Aha, AhaError
from .config import ConfigError, Settings
from .models import Device
from .session import Blocked, FritzError, LoginFailed, Session

EXIT_OK = 0
EXIT_DEVICE = 1
EXIT_USAGE = 2
EXIT_CONNECTION = 3


def _add_connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", help="box name or IP (default fritz.box, env FRITZ_HOST)")
    parser.add_argument("--user", help="FRITZ!Box user with Smart Home permission")
    parser.add_argument("--password", help="password (prefer env FRITZ_PASSWORD or the credentials file)")
    parser.add_argument("--scheme", choices=["http", "https"], help="transport (default http)")
    parser.add_argument("--port", type=int, help="explicit port")
    parser.add_argument("--verify-tls", action="store_true", default=None,
                        help="verify the box certificate (needs --ca-file)")
    parser.add_argument("--ca-file", help="PEM file with the box certificate")
    parser.add_argument("--env-file", help="dotenv file with FRITZ_* (default private/.env, then ./.env)")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    parser.add_argument("--json", action="store_true", help="machine readable output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hase10",
        description="FRITZ!Box smart home control via the AHA-HTTP interface.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    devices = sub.add_parser("devices", help="list all smart home devices")
    _add_connection_options(devices)
    devices.add_argument("--ain", help="only this device")

    switch = sub.add_parser("switch", help="switch a socket or light on/off")
    _add_connection_options(switch)
    switch.add_argument("ain")
    switch.add_argument("action", choices=["on", "off", "toggle", "state"])

    thermostat = sub.add_parser("thermostat", help="read or set a radiator thermostat")
    _add_connection_options(thermostat)
    thermostat.add_argument("ain")
    thermostat.add_argument("--set", dest="setpoint", type=float, metavar="C",
                            help="setpoint in celsius, 8..28")
    thermostat.add_argument("--off", action="store_true", help="switch the thermostat off")
    thermostat.add_argument("--on", action="store_true", help="switch the thermostat on")

    watch = sub.add_parser("watch", help="poll and print state changes")
    _add_connection_options(watch)
    watch.add_argument("--interval", type=float, default=60.0, help="seconds between polls")
    watch.add_argument("--count", type=int, default=0, help="stop after N polls (0 = forever)")

    raw = sub.add_parser("raw", help="send a raw switchcmd (escape hatch)")
    _add_connection_options(raw)
    raw.add_argument("switchcmd")
    raw.add_argument("--ain")
    raw.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")

    return parser


def _connect(args) -> Aha:
    settings = Settings.load(
        host=args.host,
        user=args.user,
        password=args.password,
        scheme=args.scheme,
        port=args.port,
        verify_tls=args.verify_tls,
        ca_file=args.ca_file,
        env_file=args.env_file,
    )
    session = Session(
        host=settings.host,
        password=settings.password,
        user=settings.user,
        scheme=settings.scheme,
        port=settings.port,
        verify_tls=settings.verify_tls,
        ca_file=settings.ca_file,
        timeout=args.timeout,
    )
    return Aha(session)


def _print_devices(devices: list[Device], as_json: bool) -> None:
    if as_json:
        print(json.dumps([device.summary() for device in devices], indent=2, ensure_ascii=False))
        return
    if not devices:
        print("no smart home devices found")
        return
    print(f"{'NAME':<24}  {'KIND':<17}  {'STATE':<5}  {'SETPOINT':<11}  {'TEMP':<11}  {'POWER':>8}")
    print("-" * 90)
    for device in devices:
        print(device.line())
        if device.battery_low:
            print(f"  ! battery low ({device.battery}%) on {device.name or device.ain}")


def _find(devices: list[Device], ain: str) -> Device:
    wanted = ain.replace(" ", "").lower()
    for device in devices:
        if device.ain.replace(" ", "").lower() == wanted:
            return device
    known = ", ".join(d.ain for d in devices) or "none"
    raise AhaError(f"no device with AIN {ain}; known: {known}")


def cmd_devices(args) -> int:
    aha = _connect(args)
    devices = aha.device_list()
    if args.ain:
        devices = [_find(devices, args.ain)]
    _print_devices(devices, args.json)
    return EXIT_OK


def cmd_switch(args) -> int:
    aha = _connect(args)
    device = _find(aha.device_list(), args.ain)
    if not device.is_switchable:
        raise AhaError(f"{device.name or device.ain} is not switchable ({device.kind})")

    if args.action == "state":
        state = aha.switch_state(device.ain)
        payload = {"ain": device.ain, "name": device.name, "state": state}
        print(json.dumps(payload) if args.json else f"{device.name or device.ain}: {'on' if state else 'off'}")
        return EXIT_OK

    action = {"on": aha.switch_on, "off": aha.switch_off, "toggle": aha.switch_toggle}[args.action]
    state = action(device.ain)
    if args.json:
        print(json.dumps({"ain": device.ain, "name": device.name, "state": state}))
    else:
        print(f"{device.name or device.ain}: {'on' if state else 'off'}")
    return EXIT_OK


def cmd_thermostat(args) -> int:
    aha = _connect(args)
    device = _find(aha.device_list(), args.ain)
    if not device.is_thermostat:
        raise AhaError(f"{device.name or device.ain} is not a thermostat ({device.kind})")

    changed = False
    if args.setpoint is not None:
        if not 8 <= args.setpoint <= 28:
            raise AhaError("setpoint must be between 8 and 28 °C")
        aha.set_temperature(device.ain, args.setpoint)
        changed = True
    if args.off:
        aha.set_temperature_off(device.ain)
        changed = True
    if args.on:
        aha.set_temperature_on(device.ain)
        changed = True

    if changed:
        device = aha.device(device.ain)

    thermostat = device.thermostat
    payload = {
        "ain": device.ain,
        "name": device.name,
        "actual_c": thermostat.actual_c if thermostat else None,
        "setpoint_c": thermostat.setpoint_c if thermostat else None,
        "setpoint_state": thermostat.setpoint_state if thermostat else None,
        "window_open": thermostat.window_open if thermostat else None,
        "battery": thermostat.battery if thermostat else None,
        "error_code": thermostat.error_code if thermostat else None,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        setpoint = (
            f"{payload['setpoint_c']:.1f} °C" if payload["setpoint_c"] is not None
            else (payload["setpoint_state"] or "unknown")
        )
        actual = f"{payload['actual_c']:.1f} °C" if payload["actual_c"] is not None else "unknown"
        print(f"{device.name or device.ain}: set {setpoint}, actual {actual}"
              + (", window open" if payload["window_open"] else ""))
    return EXIT_OK


def _snapshot(devices: list[Device]) -> dict[str, tuple]:
    state: dict[str, tuple] = {}
    for device in devices:
        state[device.ain] = (
            device.switch.state if device.switch else None,
            round(device.thermostat.setpoint_c, 1) if device.thermostat and device.thermostat.setpoint_c else None,
            device.thermostat.setpoint_state if device.thermostat else None,
            round(device.temperature_c, 1) if device.temperature_c is not None else None,
            round(device.power.power_w, 1) if device.power and device.power.power_w is not None else None,
            device.present,
        )
    return state


def cmd_watch(args) -> int:
    aha = _connect(args)
    previous: dict[str, tuple] = {}
    polls = 0
    try:
        while True:
            devices = aha.device_list()
            current = _snapshot(devices)
            if not previous:
                print(f"watching {len(devices)} devices, interval {args.interval:g}s", flush=True)
                for device in devices:
                    print("  " + device.line(), flush=True)
            else:
                for device in devices:
                    before = previous.get(device.ain)
                    after = current.get(device.ain)
                    if before != after:
                        print(f"{time.strftime('%H:%M:%S')}  {device.name or device.ain}: "
                              f"{before} -> {after}", flush=True)
            previous = current
            polls += 1
            if args.count and polls >= args.count:
                return EXIT_OK
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return EXIT_OK


def cmd_raw(args) -> int:
    aha = _connect(args)
    params = {}
    for item in args.param:
        if "=" not in item:
            raise AhaError(f"--param expects NAME=VALUE, got {item}")
        key, _, value = item.partition("=")
        params[key] = value
    print(aha.call(args.switchcmd, args.ain, **params))
    return EXIT_OK


HANDLERS = {
    "devices": cmd_devices,
    "switch": cmd_switch,
    "thermostat": cmd_thermostat,
    "watch": cmd_watch,
    "raw": cmd_raw,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return HANDLERS[args.command](args)
    except Blocked as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONNECTION
    except LoginFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONNECTION
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (AhaError, FritzError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DEVICE
    except KeyboardInterrupt:
        return EXIT_OK
