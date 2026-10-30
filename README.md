# hase10smarthome

Control and monitor FRITZ!Box smart home devices from Python — no cloud, no
extra hubs, just the box on the local network.

Built against the **AHA-HTTP interface** and verified against AVM's own
documentation (`AHA-HTTP-API`, version 1.62) — the same interface FRITZ!OS 8.25
still serves. No third-party dependencies: everything runs on the standard
library.

```python
from hase10 import Aha, Session

aha = Aha(Session(host="fritz.box", user="hase10", password="…"))
for device in aha.device_list():
    print(device.name, device.kind, device.temperature_c)

aha.switch_on("08761 0000434")
aha.set_temperature("08761 1048079", 21.5)
```

## Requirements

- Python 3.9+
- A FRITZ!Box with smart home support and FRITZ!OS 7.24 or newer (PBKDF2 login);
  older firmware works too via the MD5 challenge-response path
- A **FRITZ!Box user** with the *Smart Home* permission, and *Zugriff für
  Anwendungen zulassen* enabled in the box UI (Heimnetz → Netzwerk →
  Netzwerkeinstellungen)

The login is the box's normal password login. Two-factor authentication for the
web UI does not block the API; the AHA interface only needs the Smart Home right.

## Setup

```bash
mkdir -p ~/.config/hase10
cat > ~/.config/hase10/credentials <<'EOF'
user=hase10
password=…
EOF
chmod 600 ~/.config/hase10/credentials
```

Configuration is read from command line, then environment, then this file:

| Setting | Environment | Default |
|---|---|---|
| host | `FRITZ_HOST` | `fritz.box` |
| user | `FRITZ_USER` | — |
| password | `FRITZ_PASSWORD` | — |
| transport | `FRITZ_SCHEME` | `http` |
| port | `FRITZ_PORT` | box default |
| TLS verification | `FRITZ_VERIFY_TLS` + `FRITZ_CA_FILE` | off (self-signed cert) |

The package refuses a credentials file that is readable by others.

## Usage

```bash
python -m hase10 devices                 # table of all devices
python -m hase10 devices --json          # same, machine readable
python -m hase10 switch "08761 0000434" on
python -m hase10 switch "08761 0000434" state
python -m hase10 thermostat "08761 1048079" --set 21.5
python -m hase10 thermostat "08761 1048079" --off
python -m hase10 watch --interval 30     # poll and print changes
python -m hase10 raw getswitchlist       # escape hatch for any switchcmd
```

```
NAME                      KIND               STATE  SETPOINT   TEMP       POWER
--------------------------------------------------------------------------------------
Kaffeemaschine            switch             on                28.5°C now    12.3 W
Wohnzimmer                thermostat               21.0°C set  21.0°C now
Flur Taster               button                                       OFFLINE
Gruppe Wohnzimmer         group              on
```

`--host`, `--user`, `--password`, `--scheme`, `--port` and `--credentials` work
with every subcommand. Exit codes: `0` ok, `1` device/command error, `2` usage
or configuration, `3` connection or login.

## Protocol notes

Everything below was checked against AVM's AHA-HTTP-API document, not guessed:

**Login** (`/login_sid.lua?version=2`)

- FRITZ!OS ≥ 7.24 sends `Challenge: 2$1000$<salt1>$1000$<salt2>`. The response is
  PBKDF2-HMAC-SHA256 over the password with the **hex-decoded** salts, twice:
  `h2 = pbkdf2(h1, salt2, iter2)`, answered as `<salt2>$<h2 hex>`.
- Older firmware sends a plain challenge; the response is
  `md5(challenge-password)` over UTF-16-LE, answered as `<challenge>-<hash>`.
- Failures return `SID 0000000000000000`; repeated failures set `BlockTime`
  (seconds) and the client raises `Blocked` instead of hammering the box.
- The SID is cached and renewed automatically once on HTTP 403.

**Commands** (`/webservices/homeautoswitch.lua?switchcmd=…&ain=…&sid=…`)

| Command | Returns |
|---|---|
| `getdevicelistinfos` | XML with all devices, groups and units |
| `getswitchlist` | comma separated AINs |
| `getswitchstate`, `setswitchon`, `setswitchoff`, `setswitchtoggle` | `0`/`1` |
| `getswitchpower`, `getswitchenergy` | mW, Wh |
| `gettemperature` | 0.1 °C |
| `gethkrtsoll`, `sethkrtsoll` | 0.5 °C steps, `254` = on, `253` = off |
| `setlevelpercentage`, `setsimpleonoff`, `setcolor` | level/color control |

**Units** — the box sends integers in fixed steps, the client converts them:

| Field | Wire unit | Attribute |
|---|---|---|
| `<temperature><celsius>` | 0.1 °C | `temperature_c` |
| `<hkr><tist>`, `<tsoll>`, `<komfort>`, `<absenk>` | 0.5 °C | `thermostat.actual_c`, `setpoint_c`, … |
| `<powermeter><power>` | 0.001 W | `power.power_w` |
| `<powermeter><voltage>` | 0.001 V | `power.voltage_v` |
| `<powermeter><energy>` | Wh | `power.energy_wh` |

Empty elements mean "unknown" and become `None` — never `0`.

**Capabilities** come from `functionbitmask`: bit 6 thermostat, 7 power meter,
8 temperature sensor, 9 socket, 15 on/off, 16 level, 17 color, 18 blind,
20 humidity, 5 button, 4 alarm, 0/13 HAN-FUN.

## Layout

```
hase10/session.py   login_sid.lua, PBKDF2/MD5 challenge-response, SID renewal
hase10/aha.py       homeautoswitch.lua client, typed helpers
hase10/devices.py   XML -> dataclasses, unit conversion
hase10/models.py    Device/Switch/Thermostat/PowerMeter, capability bits
hase10/config.py    settings from CLI, environment, credentials file
hase10/cli.py       devices | switch | thermostat | watch | raw
tests/              mock FRITZ!Box (real HTTP) + unit and CLI tests
```

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

38 tests, no hardware required: `tests/mock_fritz.py` is a real HTTP server that
implements the challenge-response login (both schemes), the device XML and the
command endpoints, including 403 on an expired SID. Covered: login paths, SID
reuse, block time, unit conversion, unknown values, capability detection,
switching, thermostat setpoints and clamping, re-login, and the CLI.

## Not implemented yet

- TR-064 / SOAP services (WAN status, call lists, host list)
- Push notifications via `startulesubscription` (current `watch` polls)
- Templates and routines (`gettemplatelistinfos`, `applytemplate`)
- Colour and blind commands (`setcolor`, `setblind`)

## Next steps

The client is the foundation; the smarthome layer on top is still open:
dashboards, rules ("if window open then reduce setpoint"), persistence of
temperature/power history, or bridging to MQTT / Home Assistant.
