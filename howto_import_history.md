# Importing the historic sensor data (2019–2024)

One-time migration: an old CSV export of FRITZ!Box smart home readings is loaded
into the same InfluxDB bucket the live collector writes to. This document records
what the export contains, why the importer is built the way it is, and how to run
and verify it.

The importer is `import_history.py`. It is not part of the collector and is not
needed after the migration.

---

## 1. The archive

```
fritzsmarthome_data_2019-2024.tar.xz     4.9 MB packed, 91 MB extracted
└── <year>/<month>/<SensorName>          566 files, no file extension
```

| | |
|---|---|
| format | CSV with header, one row per reading |
| coverage | 2019-01-27 → 2024-09-20 |
| data rows | **1,861,217** |
| importable temperature points | **1,841,890** |
| rows without a temperature | 19,327 |
| sensors | 16 file names for 12 rooms |

```csv
Time,SensorName,actualTemperature,targetTemperature
2020-02-29 23:00:05.051654,<SensorName>,23.0,17.0
```

Rows per year:

| Year | Rows | Note |
|---|---|---|
| 2019 | 458,444 | densest year, 1–2 minute sampling in Feb/Mar |
| 2020 | 331,032 | |
| 2021 | 139,602 | February–June missing entirely |
| 2022 | 329,960 | |
| 2023 | 350,160 | |
| 2024 | 252,020 | ends 2024-09-20 |

---

## 2. What makes this data awkward

### 2.1 The clock is a fixed offset, not local time with DST

The timestamps have no timezone marker and do **not** follow daylight saving:
the stored clock advances continuously across both changeover nights.

Evidence, three independent observations:

- **Scheduled setpoint changes** for one thermostat cluster at `03:40` and `20:40`
  in winter but at `02:40` and `19:40` in summer — exactly one hour earlier,
  which a DST-aware clock would not do.
- **Changeover days have a full 24 hours**: a 15-minute cadence produces 96 rows
  on 2022-03-27, not the 92 a spring-forward would leave.
- **The configured heating schedule** in the box switches to comfort at `03:30`,
  and the data changes at `03:40` — the poll right after. That fixes the offset at
  **UTC+01:00 (CET)**, not UTC.

Consequence: the importer uses a fixed `+01:00` offset by default. `--tz
Europe/Berlin` switches to a DST-aware conversion if a future export really did
record local time.

> Getting this wrong shifts every summer point by 60 minutes — in this export
> that is 56 % of all rows (1,022,766). It would not look obviously broken, which
> is why the offset is pinned by evidence rather than assumed.

### 2.2 Two row shapes

Thermostats carry `targetTemperature`, sockets stop after the temperature:

```
2020-03-14 15:20:05,<Sensor>,20.5,22.0     ← 4 columns
2020-02-29 23:00:05,<Sensor>,19.5          ← 3 columns
```

The importer parses positionally and tolerates both.

### 2.3 Missing values are empty *or* the string `None`

19,327 rows have no usable temperature. Besides empty cells, the exporting
script wrote the literal text `None` into the setpoint column. Both are skipped,
never written as `0` — a zero would be indistinguishable from a real reading.

### 2.4 Sampling density changed three times

| Period | Interval | ≈ rows per month |
|---|---|---|
| Jan 2019 | 4–5 min | 8,000 |
| Feb–Mar 2019 | **1–2 min** | 40,000 |
| Apr 2019 – Jun 2020 | 10 min | 4,300 |
| Jul 2020 – Sep 2024 | 15 min | 2,900 |

So "expected points per month" is not a constant, and Grafana shows visibly
denser lines in early 2019 at the same zoom level.

### 2.5 Sensors were renamed over time

One room appears under three names in sequence — differing only in case — and one
socket was renamed between exports. The importer collapses them onto the current
measurement names, so the chart is continuous instead of showing three stubs.

Renames never overlap in time (checked: zero shared timestamps), so collapsing
cannot silently overwrite readings.

### 2.6 One artefact row

A single row exists for a thermostat type that was never really used (one reading,
2024-02-16), with a name containing a space and `#` — awkward in line protocol.
It is skipped. The importer escapes measurement names anyway, because a real
sensor name with a space would need it.

### 2.7 Gaps are real

2,873 interruptions longer than 15 minutes, the longest 33.9 hours. February–June
2021 is missing completely, and the export ends 2024-09-20. Nothing is
interpolated: a gap in the source stays a gap in InfluxDB.

---

## 3. Name mapping

The concrete mapping lives in `DEFAULT_MAP` inside `import_history.py`, because it
is specific to this installation. The rule it implements:

| Situation | Treatment |
|---|---|
| name unchanged since 2019 | written as-is |
| room renamed (including case changes) | all variants → today's measurement |
| retired socket with a known successor | mapped to that room |
| one-row artefact | skipped |

Overrides are possible without touching the code:

```bash
python import_history.py --src … --map OldName=NewName --skip SomeSensor
```

---

## 4. What gets written

- one field only: `temp`, as a float (your decision: no `setpoint`)
- measurement = device name exactly as the collector writes it today
- timestamp = source time converted from `+01:00` to UTC, in nanoseconds
- **only the temperature bucket** — the export contains no power data at all, so
  the consumption bucket is not touched
- no `battery`, because the old export never recorded it

---

## 5. Using the importer

### Options

| Option | Meaning |
|---|---|
| `--src` | the `.tar.xz`, read directly — no extraction |
| `--write` | actually write; without it the run is a dry run |
| `--since` / `--until` | `YYYY-MM-DD`, inclusive |
| `--sensor` | only these files (repeatable) |
| `--map OLD=NEW` | rename, overrides the built-in map |
| `--skip NAME` | omit a sensor |
| `--offset` | source clock offset, default `+01:00` |
| `--tz` | use an IANA zone instead (DST-aware) |
| `--batch` | lines per request, default 5000 |
| `--bucket` | override `INFLUX_BUCKET` |
| `--env-file` | dotenv file, default `private/.env` |
| `--limit` | stop after N rows, for experiments |
| `--force` | write even if the target range already has data |

### Credentials

`INFLUX_URL`, `INFLUX_ORG`, `INFLUX_BUCKET`, `INFLUX_TOKEN` — read from
`private/.env` **relative to the working directory**, so run the script from the
repository root. Process environment wins over the file. A dry run needs no
credentials at all.

### Procedure

```bash
# 1. dry run - reads all 566 files, writes nothing
python import_history.py --src fritzsmarthome_data_2019-2024.tar.xz

# 2. one month for real; March is a good pick, the clock changed on the 27th
python import_history.py --src fritzsmarthome_data_2019-2024.tar.xz \
  --since 2022-03-01 --until 2022-03-31 --write

# 3. look at that month in Grafana, then import everything
python import_history.py --src fritzsmarthome_data_2019-2024.tar.xz --write --force
```

The dry run prints samples, the accounting per skip reason, and a count per
sensor and per year — compare them with section 1 before trusting a write.

---

## 6. The duplicate guard

InfluxDB overwrites a point that has the same series and timestamp, so **re-running
the same import is harmless** — it costs time, not correctness.

Changing the clock offset is different: it produces *different* timestamps, so the
wrong-offset points would remain next to the correct ones. There is no overwrite
to save you.

Therefore the importer queries the target range before writing:

```
error: 1,861,890 temperature points already exist in 1970-01-01T00:00:00Z..2100-01-01T00:00:00Z
       in bucket smarthome; use --force if that is intended
```

`--force` is required after the March test, because the full range overlaps that
month.

---

## 7. Verification

Row accounting from the dry run:

| Metric | Expected |
|---|---|
| rows read | 1,861,217 |
| points written | 1,841,890 |
| skipped, no temperature | 19,327 |
| unparsable | 0 |
| out of range (< −50 or > 60 °C) | 0 |

Per year, in InfluxDB:

```flux
from(bucket: "smarthome")
  |> range(start: 2019-01-01T00:00:00Z, stop: 2025-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "<Room>")
  |> aggregateWindow(every: 1y, fn: count)
```

Compare against the table in section 1. Then check the shape:

- **February–June 2021** must be a hole, not interpolated data
- **2024 must stop on 20 September**
- **27 March 2022** must be continuous across the night — a DST-aware conversion
  would show a one-hour jump here
- the live series from 2026 must be unchanged

A wrongly converted timestamp usually shows as suspiciously tidy edges inside
the gaps.

---

## 8. Troubleshooting

| Symptom | Cause |
|---|---|
| `INFLUX_URL is not set` | running outside the repo root, or no `private/.env`; use `--env-file` |
| `cannot reach <host>: Name or service not known` | DNS from that machine |
| `HTTP 401 unauthorized access` | token lacks write permission on the bucket |
| `HTTP 404` | wrong org or bucket — both are case-sensitive |
| `temperature points already exist … use --force` | the range was already imported |
| points look shifted by an hour | offset wrong: check with `--offset +00:00` |
| everything is one hour late in summer only | the same, DST-aware conversion used by mistake |

### Undoing an import

If the offset turns out to be wrong after writing, delete the range and import
again:

```bash
curl -sS --request POST "https://<host>/api/v2/delete?org=<org>&bucket=<bucket>" \
  -H "Authorization: Token $INFLUX_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"start":"2019-01-01T00:00:00Z","stop":"2025-01-01T00:00:00Z",
           "predicate":"_measurement=\"<Room>\""}'
```

Without a predicate the whole range goes — including live data, so be precise.
