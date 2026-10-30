# hase10smarthome

Control and monitor FRITZ!Box smart home devices from Python — no cloud, no
extra hubs, just the box on the local network. Runs as a CLI on the host or as a
container on a Raspberry Pi (k3s, arm64 or arm/v7).

Built against the **AHA-HTTP interface**, verified against AVM's own
documentation (`AHA-HTTP-API`, 1.62), which FRITZ!OS 8.25 still serves. No
third-party dependencies: everything runs on the standard library.

```python
from hase10 import Aha, Session

aha = Aha(Session(host="fritz.box", user="hase10", password="change-me"))
for device in aha.device_list():
    print(device.name, device.kind, device.temperature_c)

aha.switch_on("08761 0000434")
aha.set_temperature("08761 1048079", 21.5)
```

## Requirements

- Python 3.9+ (or Docker)
- A FRITZ!Box with smart home support, FRITZ!OS 7.24 or newer for the PBKDF2
  login (older firmware works through the MD5 path)
- A **FRITZ!Box user** with the *Smart Home* permission, and *Zugriff für
  Anwendungen zulassen* enabled (Heimnetz → Netzwerk → Netzwerkeinstellungen)

Two-factor authentication for the web UI does not block the API; the interface
only needs the Smart Home right.

## Secrets: `private/.env`

All credentials live in `private/.env`, which is gitignored and mode `600`:

```bash
cp .env.example private/.env
chmod 600 private/.env
$EDITOR private/.env
```

```
FRITZ_HOST=fritz.box
FRITZ_USER=hase10
FRITZ_PASSWORD=change-me
```

Resolution order, first hit wins:

1. command line (`--host`, `--user`, `--password`, …)
2. process environment (`FRITZ_*`)
3. `private/.env`, then `./.env`, then `$HASE10_ENV_FILE`

The file is parsed, never sourced — `KEY=value`, `export KEY=value`, quotes,
blank lines and `#` comments work, a quoted `#` is not treated as a comment.
On a plain host the file must not be readable by group or others; inside a
container the check is skipped, because Kubernetes mounts secrets `0440` by
design.

## Usage

```bash
python -m hase10 devices                 # table of all devices
python -m hase10 devices --json          # machine readable
python -m hase10 switch "08761 0000434" on
python -m hase10 switch "08761 0000434" state
python -m hase10 thermostat "08761 1048079" --set 21.5
python -m hase10 thermostat "08761 1048079" --off
python -m hase10 watch --interval 30     # poll and print changes
python -m hase10 raw getswitchlist       # escape hatch
```

```
NAME                      KIND               STATE  SETPOINT     TEMP            POWER
------------------------------------------------------------------------------------------
Kaffeemaschine            switch             on                  28.5°C now     12.3 W
Wohnzimmer                thermostat                21.0°C set   21.0°C now
Flur Taster               button                                                        OFFLINE
  ! battery low (12%) on Flur Taster
```

Exit codes: `0` ok, `1` device/command error, `2` usage or configuration,
`3` connection or login.

## Docker

```bash
docker compose up -d                     # uses private/.env, runs `watch`
docker compose run --rm hase10 devices   # one-off command
docker run --rm --env-file private/.env hase10smarthome:0.1.0 devices
```

The image is ~145 MB, runs as uid 10001 and needs no writable filesystem.
`private/` is excluded via `.dockerignore`, so secrets never enter a layer.

### Building for the Pi

```bash
# on a workstation, multi-arch manifest (needs qemu binfmt)
./scripts/build-image.sh

# natively on the Pi 4/5 with 64-bit OS
PLATFORMS=linux/arm64 ./scripts/build-image.sh --load

# natively on a 32-bit Pi OS
PLATFORMS=linux/arm/v7 ./scripts/build-image.sh --load
```

`python:3.12-slim` publishes `linux/arm64/v8` and `linux/arm/v7`, both were built
and run for this project. Register emulation once on an x86 host with:

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64,arm
```

## k3s

```bash
kubectl apply -f k8s/00-namespace.yaml

kubectl -n hase10 create secret generic hase10-env \
  --from-env-file=private/.env --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f k8s/20-deployment.yaml
```

The Deployment runs `watch --interval 60`, pulls credentials from the Secret as
environment variables, runs read-only as uid 10001 with all capabilities
dropped, and uses `Recreate` so two pollers never overlap. `30-cronjob.example.yaml`
is the alternative shape: a snapshot every 15 minutes instead of a long running
loop.

The pod needs a route to the box. On a Pi in the same LAN that is given; in a
VLAN setup set `FRITZ_HOST` to the box's IP.

## Protocol notes

Everything below was checked against AVM's document, not guessed:

**Login** (`/login_sid.lua?version=2`)

- FRITZ!OS ≥ 7.24 sends `Challenge: 2$1000$<salt1>$1000$<salt2>`. The response is
  PBKDF2-HMAC-SHA256 over the password with the **hex-decoded** salts, twice:
  `h2 = pbkdf2(h1, salt2, iter2)`, answered as `<salt2>$<h2 hex>`.
- Older firmware sends a plain challenge; the answer is
  `md5(challenge-password)` over UTF-16-LE, sent as `<challenge>-<hash>`.
- A failed login returns `SID 0000000000000000`; repeated failures set
  `BlockTime` and the client raises `Blocked` instead of hammering the box.
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
| `setlevelpercentage`, `setsimpleonoff`, `setcolor` | level and colour control |

**Units** — the box sends integers in fixed steps:

| Field | Wire unit | Attribute |
|---|---|---|
| `<temperature><celsius>` | 0.1 °C | `temperature_c` |
| `<hkr><tist>`, `<tsoll>`, `<komfort>`, `<absenk>` | 0.5 °C | `thermostat.actual_c`, `setpoint_c`, … |
| `<powermeter><power>` | 0.001 W | `power.power_w` |
| `<powermeter><voltage>` | 0.001 V | `power.voltage_v` |
| `<powermeter><energy>` | Wh | `power.energy_wh` |

Empty elements mean "unknown" and become `None` — never `0`. The thermostat
setpoint uses `254` for *on* and `253` for *off*, which is the reverse of what
one would guess.

**Capabilities** come from `functionbitmask`: bit 6 thermostat, 7 power meter,
8 temperature sensor, 9 socket, 15 on/off, 16 level, 17 colour, 18 blind,
20 humidity, 5 button, 4 alarm, 0/13 HAN-FUN.

## Layout

```
hase10/session.py   login_sid.lua, PBKDF2/MD5 challenge-response, SID renewal
hase10/aha.py       homeautoswitch.lua client, typed helpers
hase10/devices.py   XML -> dataclasses, unit conversion
hase10/models.py    Device/Switch/Thermostat/PowerMeter, capability bits
hase10/config.py    settings from CLI, environment and private/.env
hase10/cli.py       devices | switch | thermostat | watch | raw
Dockerfile          python:3.12-slim, non-root, no pip install
docker-compose.yml  local run with env_file: private/.env
k8s/                namespace, secret template, Deployment, CronJob example
scripts/build-image.sh  multi-arch build for amd64, arm64, arm/v7
tests/              mock FRITZ!Box (real HTTP) + unit, config and CLI tests
```

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

52 tests, no hardware required: `tests/mock_fritz.py` is a real HTTP server
implementing both login schemes, the device XML and every command endpoint,
including 403 on an expired SID. Covered: login paths, SID reuse, block time,
`.env` parsing and precedence, permission checks, unit conversion, unknown
values, capability detection, switching, thermostat setpoints and clamping,
re-login, and the CLI.

## Not implemented yet

- TR-064 / SOAP services (WAN status, call lists, host list)
- Push notifications via `startulesubscription` (current `watch` polls)
- Templates and routines (`gettemplatelistinfos`, `applytemplate`)
- Colour and blind commands (`setcolor`, `setblind`)
- Persistence: nothing is stored yet, `watch` only prints changes

## Next steps

The client and the container are the foundation; the smarthome layer on top is
still open — history in SQLite, rules ("window open → setpoint 12 °C"), or
bridging to MQTT / Home Assistant.
