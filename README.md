# fbsh — FRITZ!Box Smart Home

Read and control the smart home devices of a FRITZ!Box from Python and from the
command line. No cloud, no bridge, no third-party packages — the box is queried
directly over the local network through its **AHA-HTTP interface**.

Written against AVM's `AHA-HTTP-API` specification (1.62), which FRITZ!OS 7.x
and 8.x serve — including 8.25.

```console
$ fbsh devices
NAME                        KIND            DATA
------------------------------------------------------------------------------
Kaffeemaschine              switch          switch, power, temperature
Wohnzimmer                  thermostat      thermostat, temperature, battery
Flur Taster                 button          temperature, button, battery
Gruppe Wohnzimmer           group           switch
```

## Requirements

- Python 3.9 or newer (3.12 recommended)
- A FRITZ!Box with smart home support
- A **FRITZ!Box user** with the *Smart Home* permission, see
  [Setup on the box](#setup-on-the-box)
- FRITZ!OS 7.24+ for the modern PBKDF2 login; older firmware works through the
  MD5 fallback

Nothing else. `fbsh` has no dependencies outside the standard library, so there
is nothing to compile and nothing to download at install time.

## Quickstart

```bash
git clone git@github.com:zehbot/fbsh.git
cd fbsh

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e .                   # optional, provides the `fbsh` command
```

The package also runs without installation, straight from the checkout:

```bash
python -m fbsh devices
```

Create the configuration file:

```bash
cp .env.example private/.env
chmod 600 private/.env
$EDITOR private/.env               # FRITZ_HOST, FRITZ_USER, FRITZ_PASSWORD
```

First contact:

```bash
python -m fbsh devices
```

## Setup on the box

1. **Create a user** in the FRITZ!Box UI: *System → FRITZ!Box-Benutzer → Benutzer
   hinzufügen*. Give it the **Smart Home** permission. Some commands (level,
   colour) also need *Eingeschränkte FRITZ!Box-Einstellungen für Apps*.
2. **Allow application access**: *Heimnetz → Netzwerk → Netzwerkeinstellungen →
   Zugriff für Anwendungen zulassen*.
3. Two-factor authentication for the web UI does **not** block the API — the AHA
   interface only checks the Smart Home right.

If the box rejects the login, it is almost always one of those two switches, not
the password.

## Configuration

All secrets live in `private/.env`, which is gitignored and must be mode `600`:

```
FRITZ_HOST=192.168.178.1
FRITZ_USER=example_user
FRITZ_PASSWORD=change-me
```

| Key | Meaning | Default |
|---|---|---|
| `FRITZ_HOST` | box name or IP | `fritz.box` |
| `FRITZ_USER` | FRITZ!Box user | — |
| `FRITZ_PASSWORD` | its password | — |
| `FRITZ_SCHEME` | `http` or `https` | `http` |
| `FRITZ_PORT` | explicit port | box default |
| `FRITZ_VERIFY_TLS` | `1` to verify the certificate | off |
| `FRITZ_CA_FILE` | PEM of the box certificate | — |

Resolution order, first hit wins:

1. command line (`--host`, `--user`, `--password`, `--env-file`, …)
2. process environment (`FRITZ_*`)
3. `private/.env` → `./.env` → `$FBSH_ENV_FILE`

The file is **parsed, not sourced**: `KEY=value`, `export KEY=value`, quotes,
blank lines and `#` comments work, a quoted `#` stays part of the value. On a
normal host a file readable by group or others is refused with a hint to run
`chmod 600`. Inside a container the check is skipped, because Kubernetes mounts
secrets as `0440` by design; `FBSH_ENV_PERMISSIVE=1` disables it explicitly.

## The `devices` call

`fbsh devices` is the inventory: every device with its **kind** and the **data it
can deliver**.

| Column | Meaning |
|---|---|
| `NAME` | the name from the FRITZ!Box UI, or the AIN if unnamed |
| `KIND` | `switch`, `thermostat`, `light`, `dimmable-light`, `color-light`, `blind`, `button`, `sensor`, `temperature-sensor`, `group`, `device` |
| `DATA` | capabilities: `switch`, `thermostat`, `blind`, `light`, `level`, `color`, `power`, `temperature`, `humidity`, `alarm`, `button`, `battery` |

`--json` gives the same thing machine readable, including the current values:

```console
$ fbsh devices --json
[
  {
    "ain": "08761 0000434",
    "name": "Kaffeemaschine",
    "kind": "switch",
    "capabilities": ["switch", "power", "temperature"],
    "present": true,
    "product": "FRITZ!DECT 200",
    "switch": {"state": true, "mode": "auto", "locked": false},
    "power": {"watts": 12.345, "energy_wh": 707, "voltage_v": 230.252},
    "temperature_c": 28.5
  }
]
```

Capability meanings:

| Capability | Values behind it |
|---|---|
| `switch` | on/off state, mode (auto/manual), switch lock |
| `thermostat` | setpoint, comfort/eco temperature, window-open, boost, error code |
| `power` | current watts, total energy in Wh, voltage |
| `temperature` | measured temperature in °C |
| `humidity` | relative humidity in percent |
| `level` / `color` | brightness 0–100 %, hue and saturation |
| `blind` | position and direction |
| `button` | last press timestamp |
| `alarm` | alarm state, last change |
| `battery` | battery percentage and low-battery flag |

## Command line

```
fbsh devices   [--ain AIN] [--json]        inventory: kind and capabilities
fbsh status    [--ain AIN] [--json]        current values per device
fbsh switch    AIN on|off|toggle|state     sockets and lights
fbsh thermostat AIN [--set C] [--on] [--off]
fbsh watch     [--interval 60] [--count N] poll and print changes
fbsh raw       SWITCHCMD [--ain AIN] [--param NAME=VALUE ...]
```

Global options on every command: `--host`, `--user`, `--password`, `--scheme`,
`--port`, `--verify-tls`, `--ca-file`, `--env-file`, `--timeout`, `--json`.

```console
$ fbsh status
NAME                      KIND               STATE  SETPOINT     TEMP            POWER
------------------------------------------------------------------------------------------
Kaffeemaschine            switch             on                  28.5°C now     12.3 W
Wohnzimmer                thermostat                21.0°C set   21.0°C now
Flur Taster               button                                                        OFFLINE
  ! battery low (12%) on Flur Taster

$ fbsh switch "08761 0000434" off
Kaffeemaschine: off

$ fbsh thermostat "08761 1048079" --set 21.5
Wohnzimmer: set 21.5 °C, actual 21.0 °C
```

Exit codes: `0` ok, `1` device or command error, `2` usage or configuration
error, `3` connection or login error.

## Library

```python
import fbsh

for device in fbsh.list_devices():
    print(device.name, device.kind, device.capabilities)
```

`list_devices()` reads the same configuration as the CLI, so it works in a
notebook or a script with nothing but a `private/.env` next to it.

For repeated access keep one session and client:

```python
from fbsh import Aha, Session

aha = Aha(Session(host="fritz.box", user="example_user", password="change-me"))

devices = aha.device_list()
thermostat = aha.device("08761 1048079")
print(thermostat.thermostat.setpoint_c)      # 21.0
print(thermostat.thermostat.actual_c)        # 21.0
print(thermostat.capabilities)               # ['thermostat', 'temperature', 'battery']

aha.switch_on("08761 0000434")
aha.set_temperature("08761 1048079", 21.5)   # clamped to 8..28 °C
aha.switch_state("08761 0000434")            # True
```

Device objects carry the parsed values and the raw shape:

| Attribute | Type |
|---|---|
| `ain`, `name`, `productname`, `firmware`, `manufacturer` | `str` |
| `kind`, `capabilities` | `str`, `list[str]` |
| `present`, `battery_low` | `bool \| None` |
| `switch.state`, `.mode`, `.locked` | `bool \| None`, `str \| None` |
| `power.power_w`, `.energy_wh`, `.voltage_v` | `float \| None`, `int \| None` |
| `temperature_c`, `humidity`, `level_percent` | `float \| None`, `int \| None` |
| `thermostat.setpoint_c`, `.actual_c`, `.comfort_c`, `.eco_c` | `float \| None` |
| `thermostat.setpoint_state` | `"on"`, `"off"` or `None` |
| `thermostat.window_open`, `.boost_active`, `.battery` | `bool \| None`, `int \| None` |

## Protocol notes

Everything below was read from AVM's specification, not guessed.

**Login** — `GET /login_sid.lua?version=2`

- FRITZ!OS ≥ 7.24 answers with `Challenge: 2$1000$<salt1>$1000$<salt2>`. The
  response is PBKDF2-HMAC-SHA256 over the password with the **hex-decoded**
  salts, applied twice: `h2 = pbkdf2(h1, salt2, iter2)`, sent as
  `<salt2>$<h2 hex>`.
- Older firmware sends a plain challenge; the answer is
  `md5(challenge-password)` over UTF-16-LE, sent as `<challenge>-<hash>`.
- A failed login returns `SID 0000000000000000`. Repeated failures set
  `BlockTime`; the client raises `Blocked` with the remaining seconds instead of
  hammering the box.
- The SID is cached for the process and renewed once automatically on HTTP 403.

**Commands** — `GET /webservices/homeautoswitch.lua?switchcmd=…&ain=…&sid=…`

| Command | Answer |
|---|---|
| `getdevicelistinfos` | XML with devices, groups and HAN-FUN/Zigbee units |
| `getswitchlist` | comma separated AINs of all switchable actors |
| `getswitchstate`, `setswitchon`, `setswitchoff`, `setswitchtoggle` | `0` / `1` |
| `getswitchpower`, `getswitchenergy` | mW, Wh |
| `gettemperature` | 0.1 °C |
| `gethkrtsoll`, `sethkrtsoll` | 0.5 °C steps, `254` = on, `253` = off |
| `setsimpleonoff`, `setlevelpercentage`, `setcolor` | actor control |

**Units** — the box sends integers in fixed steps; `fbsh` converts them:

| XML element | Wire unit | Attribute |
|---|---|---|
| `<temperature><celsius>` | 0.1 °C | `temperature_c` |
| `<hkr><tist>`, `<tsoll>`, `<komfort>`, `<absenk>` | 0.5 °C | `thermostat.*_c` |
| `<powermeter><power>` | 0.001 W | `power.power_w` |
| `<powermeter><voltage>` | 0.001 V | `power.voltage_v` |
| `<powermeter><energy>` | Wh | `power.energy_wh` |

Empty elements mean "unknown" and become `None` — never `0`, which would
otherwise show up as a plausible measurement.

**Capabilities** come from `functionbitmask`: bit 6 thermostat, 7 power meter,
8 temperature sensor, 9 socket, 15 on/off, 16 level, 17 colour, 18 blind,
20 humidity, 5 button, 4 alarm, 0/13 HAN-FUN.

## Docker

```bash
docker compose up -d                      # runs `watch` with private/.env
docker compose run --rm fbsh devices      # one-off command
docker build -t fbsh:0.1.0 .
docker run --rm --env-file private/.env fbsh:0.1.0 devices
```

The image is based on `python:3.12-slim`, is about 145 MB, runs as uid 10001 and
needs no writable filesystem. `private/` is excluded via `.dockerignore`, so
secrets never enter a layer.

### Raspberry Pi / ARM

`python:3.12-slim` exists for `linux/arm64/v8` and `linux/arm/v7`, both are
verified for this project.

```bash
# natively on the Pi (64-bit OS)
PLATFORMS=linux/arm64 ./scripts/build-image.sh --load

# natively on a 32-bit Pi OS
PLATFORMS=linux/arm/v7 ./scripts/build-image.sh --load

# multi-arch manifest on a workstation (needs qemu binfmt)
docker run --privileged --rm tonistiigi/binfmt --install arm64,arm
./scripts/build-image.sh --push
```

## k3s

```bash
kubectl apply -f k8s/00-namespace.yaml

kubectl -n fbsh create secret generic fbsh-env \
  --from-env-file=private/.env --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f k8s/20-deployment.yaml
```

`20-deployment.yaml` runs `watch --interval 60` with credentials from the Secret,
as non-root with all capabilities dropped and a read-only root filesystem, and
uses `Recreate` so two pollers never overlap. `30-cronjob.example.yaml` is the
alternative shape: a snapshot every 15 minutes instead of a long running loop.

Deliberately no probes: every probe would log into the box. The watch loop
reconnects on its own after a failure.

## Project layout

```
fbsh/session.py     login_sid.lua, PBKDF2/MD5 challenge-response, SID renewal
fbsh/aha.py         homeautoswitch.lua client, typed helpers
fbsh/devices.py     XML -> dataclasses, unit conversion
fbsh/models.py      Device/Switch/Thermostat/PowerMeter, kind and capabilities
fbsh/config.py      settings from CLI, environment and .env files
fbsh/cli.py         devices | status | switch | thermostat | watch | raw
Dockerfile          python:3.12-slim, non-root, no build step
docker-compose.yml  local run with env_file: private/.env
k8s/                namespace, secret template, Deployment, CronJob example
scripts/build-image.sh  multi-arch build for amd64, arm64, arm/v7
tests/              mock FRITZ!Box (real HTTP) + unit, config and CLI tests
```

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

55 tests, **no hardware required**. `tests/mock_fritz.py` is a real HTTP server
that implements both login schemes, the device XML, every command endpoint and
SID expiry, so the whole path (login, request, parse, control) is exercised.

Covered: login with PBKDF2 and MD5, SID reuse and renewal after 403, block time,
`.env` parsing and precedence, permission checks, secret mounts, unit
conversion, unknown values, capability detection, switching, thermostat
setpoints and clamping, and the CLI including exit codes.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `the box rejected the user/password combination` | wrong user, user without the *Smart Home* right, or *Zugriff für Anwendungen zulassen* disabled |
| `login blocked for N s` | too many failed attempts; wait, or log in once through the web UI |
| `cannot reach http://fritz.box: …` | name resolution or wrong network — use the box IP in `FRITZ_HOST` |
| `readable by others (mode 644)` | `chmod 600 private/.env` |
| Empty values in the table | device offline (`OFFLINE` column) or battery empty |

Enable `--scheme https` if the box only serves HTTPS; the certificate is
self-signed, so verification stays off unless you provide `--ca-file`.

## Status

Early, but the client side is complete for the AHA-HTTP interface. Not
implemented yet:

- TR-064 / SOAP services (WAN status, call lists, host list)
- push notifications via `startulesubscription` — `watch` polls instead
- templates and routines (`gettemplatelistinfos`, `applytemplate`)
- colour and blind commands
- persistence: nothing is stored yet, `watch` only prints changes
