# fbsh — FRITZ!Box Smart Home

Read and control the smart home devices of a FRITZ!Box from a single Python file.
No cloud, no bridge, no dependencies, **nothing to install**.

```console
$ python fbsh.py devices
NAME                        KIND            DATA
------------------------------------------------------------------------------
Kaffeemaschine              switch          switch, power, temperature
Wohnzimmer                  thermostat      thermostat, temperature, battery
Flur Taster                 button          temperature, button, battery
Gruppe Wohnzimmer           group           switch
```

Written against AVM's `AHA-HTTP-API` specification (1.62), which FRITZ!OS 7.x
and 8.x serve — including 8.25.

## Requirements

- Python 3.9+ (`python3 --version`)
- A FRITZ!Box with smart home support
- A **FRITZ!Box user** with the *Smart Home* permission
- *Zugriff für Anwendungen zulassen* enabled: Heimnetz → Netzwerk →
  Netzwerkeinstellungen

FRITZ!OS 7.24+ uses the PBKDF2 login; older firmware falls back to MD5
automatically. Two-factor authentication for the web UI does not block the API.

## Quickstart

```bash
git clone git@github.com:zehbot/fbsh.git
cd fbsh

cp .env.example private/.env
chmod 600 private/.env
$EDITOR private/.env            # FRITZ_HOST, FRITZ_USER, FRITZ_PASSWORD

python fbsh.py devices
```

No virtualenv, no `pip install`: the script imports only the standard library
and reads `private/.env` from the current directory.

## Commands

| Command | Purpose |
|---|---|
| `python fbsh.py devices [--ain AIN\|NAME] [--json]` | inventory: name, kind, capabilities |
| `python fbsh.py status [--ain AIN\|NAME] [--json]` | current values per device |
| `python fbsh.py switch AIN\|NAME on\|off\|toggle\|state` | sockets and lights |
| `python fbsh.py thermostat AIN\|NAME [--set C] [--on] [--off]` | radiator setpoint, 8–28 °C |
| `python fbsh.py collect [--dry-run]` | read all devices and write them to InfluxDB |
| `python fbsh.py raw SWITCHCMD [--ain AIN] [--param K=V]` | any AHA command |

Devices can be addressed by **AIN or by name** (`--ain Entree` and
`--ain "08761 1048079"` both work, case does not matter).

Connection options on every command: `--host`, `--user`, `--password`,
`--scheme`, `--port`, `--verify-tls`, `--ca-file`, `--env-file`, `--timeout`,
`--json`.

```console
$ python fbsh.py status
NAME                      KIND               STATE  SETPOINT     TEMP            POWER
------------------------------------------------------------------------------------------
Kaffeemaschine            switch             on                  28.5°C now     12.3 W
Wohnzimmer                thermostat                21.0°C set   21.0°C now

$ python fbsh.py switch "08761 0000434" off
Kaffeemaschine: off

$ python fbsh.py thermostat "08761 1048079" --set 21.5
Wohnzimmer: set 21.5 °C, actual 21.0 °C
```

Exit codes: `0` ok, `1` device or command error, `2` usage or config error,
`3` login or network error.

### What `devices` shows

`KIND` is what the device is, `DATA` is what it can deliver — both derived from
the `functionbitmask` the box reports:

| Capability | Values behind it |
|---|---|
| `switch` | on/off, mode (auto/manual), lock |
| `thermostat` | setpoint, comfort/eco, window-open, boost, error code |
| `power` | watts, energy Wh, volts |
| `temperature`, `humidity` | °C, % |
| `level`, `color`, `blind` | brightness, hue/saturation, position |
| `button`, `alarm`, `battery` | press timestamp, alarm state, percentage |

`--json` prints the same for every device, including the current values, so it
pipes straight into `jq` or a log line.

## Configuration

`private/.env` (gitignored, mode 600), environment variables or command line —
in that order of increasing precedence:

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
| `INFLUX_URL` | InfluxDB 2.x base URL | — |
| `INFLUX_ORG` | organisation | — |
| `INFLUX_BUCKET` | target bucket for temperature and battery | — |
| `INFLUX_BUCKET_CONSUMPTION` | optional second bucket for power | — |
| `INFLUX_TOKEN` | write token | — |
| `INFLUX_INSECURE` | `1` to skip TLS verification | off |

The file is parsed, never sourced: `KEY=value`, `export`, quotes and `#`
comments work. `FBSH_ENV_FILE` points to a different file.

## InfluxDB

`collect` reads every device and writes one point per device to an InfluxDB 2.x
bucket — thermostats with `temp` and `battery`, metered sockets with `power`,
sockets that also sense temperature with both. Groups are skipped: they are
aggregates of their members.

```console
$ python fbsh.py collect --dry-run
Esszimmer01 temp=20.0,battery=80.0 1793453389773733213
Kueche01 temp=20.5,battery=79.0 1793453389773733213
winter-arbeitszimmer temp=21.0,power=1.0 1793453389773733213
wintergarten01 power=4.0 1793453389773733213
# 17 lines, nothing written
```

- measurement = the device name exactly as the FRITZ!Box reports it
- with `INFLUX_BUCKET_CONSUMPTION` set, a second write goes to that bucket with
  `consumption=<watts>` on every metered socket — both writes come from the same
  read, so the values always belong together
- `temp` in °C, `power` in W, `battery` in percent — always floats, because a
  series' field type is immutable in InfluxDB
- one point per device per run, timestamp in nanoseconds
- all lines go in a single `POST /api/v2/write` request, so a run is all or nothing

`--dry-run` prints the line protocol and needs no InfluxDB configuration at all,
which is the safe way to check a rename before it creates a new series.

## Container

```bash
docker build -t fbsh:0.1.0 .
docker run --rm --env-file private/.env fbsh:0.1.0 status
```

The image is `python:3.12-slim` with one file copied in, runs as uid 10001 and
needs no writable filesystem. `private/` never enters a layer.

### Raspberry Pi

`python:3.12-slim` exists for `linux/arm64/v8` and `linux/arm/v7`:

```bash
# on the Pi itself
docker build -t fbsh:0.1.0 .

# from an x86 workstation
docker run --privileged --rm tonistiigi/binfmt --install arm64,arm
docker buildx build --platform linux/arm64,linux/arm/v7 -t fbsh:0.1.0 --push .
```

### k3s: scheduled snapshots

`k8s/cronjob.yaml` runs `collect` every 10 minutes — one pod per run, no long
running process, `concurrencyPolicy: Forbid` so two pollers never overlap.
The full walkthrough, from building the image to troubleshooting, is in
**[how_to_deploy_k3s.md](how_to_deploy_k3s.md)**.

```bash
kubectl apply -f k8s/cronjob.yaml

kubectl -n fbsh create secret generic fbsh-env \
  --from-env-file=private/.env --dry-run=client -o yaml | kubectl apply -f -

kubectl -n fbsh create job --from=cronjob/fbsh-status fbsh-now   # run once
kubectl -n fbsh logs -l job-name=fbsh-now
```

The pod runs non-root with all capabilities dropped and a read-only root
filesystem; credentials arrive as environment variables from the Secret.

## Tests

```bash
python3 test_fbsh.py
```

17 tests, no hardware: the test file starts a stand-in FRITZ!Box over real HTTP
(both login schemes, device XML, switch and thermostat commands, SID expiry) and
runs the actual script against it.

## Protocol notes

Checked against AVM's specification, not guessed.

- **Login** `GET /login_sid.lua?version=2`: FRITZ!OS ≥ 7.24 answers with
  `2$iter1$salt1$iter2$salt2`. The response is PBKDF2-HMAC-SHA256 over the
  password with the **hex-decoded** salts, twice, sent as `<salt2>$<hash>`.
  Older firmware uses `md5(challenge-password)` over UTF-16-LE. A failed login
  returns `SID 0000000000000000`; repeated attempts set `BlockTime`, which the
  script reports instead of retrying.
- **Commands** `GET /webservices/homeautoswitch.lua?switchcmd=…&ain=…&sid=…`.
  The SID is renewed once automatically on HTTP 403.
- **Units**: temperature 0.1 °C, thermostat setpoints 0.5 °C, power 0.001 W,
  voltage 0.001 V, energy Wh. Empty XML elements mean *unknown* and become
  `None` — never `0`.
- **Thermostats** use `254` for *on* and `253` for *off*, the reverse of what one
  would guess.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `the box rejected the user/password combination` | wrong user, missing *Smart Home* right, or *Zugriff für Anwendungen zulassen* disabled |
| `login blocked for N s` | too many failed attempts — wait or log in via the web UI |
| `cannot reach fritz.box` | name resolution or wrong network; put the box IP in `FRITZ_HOST` |
| empty values in the table | device offline (marked `OFFLINE`) or battery empty |

## Status

First iteration: reading and switching plus a scheduled container job.
Not implemented — TR-064/SOAP services, push subscriptions
(`startulesubscription`), templates and routines, colour and blind commands,
and any kind of history.
