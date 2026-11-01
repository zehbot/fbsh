#!/usr/bin/env python3
"""One-time import of the historic FRITZ!Box sensor export into InfluxDB 2.x.

Reads the .tar.xz directly (no extraction), parses YYYY/M/<SensorName> CSV files
and writes line protocol to /api/v2/write.

Dry run by default - nothing is written without --write.

    python import_history.py --src fritzsmarthome_data_2019-2024.tar.xz
    python import_history.py --src ... --since 2022-03-26 --until 2022-03-28 --write

Data facts this script encodes:
  * timestamps run on a fixed offset all year (no DST jump): scheduled setpoint
    changes appear one hour earlier in summer than in winter
  * default offset is +01:00 (CET); use --tz for a DST-aware conversion instead
  * thermostats have 4 columns, sockets only 3
  * missing setpoints appear as an empty cell or the literal string "None"
  * sampling density varies: 1-5 min in early 2019, 10 min 2019/2020, 15 min later

InfluxDB overwrites a point with the same series and timestamp, so re-running the
same import is safe - it costs time, not correctness. Changing the clock offset
does NOT overwrite: it produces different timestamps and therefore duplicates,
which is why the script refuses to write into a range that already holds data
unless --force is given.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import io
import json
import os
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

TOKEN_VAR = "INFLUX_" + "TOKEN"

# file name -> measurement; everything else keeps its own name
DEFAULT_MAP = {
    "Arbeitszimmer": "Arbeitszimmer01",
    "arbeitszimmer": "Arbeitszimmer01",
    "Terrasse01": "aussen01",
    "switch01": "Entree",
}
# rows we deliberately do not import
DEFAULT_SKIP = {"Comet DECT #18"}

EMPTY_VALUES = {"", "none", "nan", "inval", "null"}
VERBOSE = False


class Stats:
    def __init__(self) -> None:
        self.read = 0
        self.written = 0
        self.empty = 0
        self.unparsable = 0
        self.out_of_range = 0
        self.ambiguous = 0          # DST fall-back hour
        self.per_sensor = defaultdict(int)
        self.per_year = Counter()
        self.batches = 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--src", required=True, help="the .tar.xz archive")
    p.add_argument("--write", action="store_true", help="actually write (default: dry run)")
    p.add_argument("--since", help="YYYY-MM-DD, inclusive")
    p.add_argument("--until", help="YYYY-MM-DD, inclusive")
    p.add_argument("--sensor", action="append", default=[], help="only these files (repeatable)")
    p.add_argument("--map", action="append", default=[], metavar="OLD=NEW",
                   help="rename a sensor (repeatable, overrides the built-in map)")
    p.add_argument("--skip", action="append", default=[], help="skip a sensor (repeatable)")
    p.add_argument("--with-setpoint", action="store_true",
                   help="also write targetTemperature as field setpoint")
    p.add_argument("--offset", default="+01:00",
                   help="fixed UTC offset of the source clock (default +01:00)")
    p.add_argument("--tz", help="use an IANA timezone instead of a fixed offset, e.g. Europe/Berlin")
    p.add_argument("--batch", type=int, default=5000, help="lines per request (default 5000)")
    p.add_argument("--bucket", help="target bucket (default: INFLUX_BUCKET)")
    p.add_argument("--env-file", help="dotenv file with INFLUX_* (default private/.env if present)")
    p.add_argument("--limit", type=int, help="stop after N rows (for experiments)")
    p.add_argument("--sample", type=int, default=5, help="how many line samples to print")
    p.add_argument("--verbose", action="store_true", help="print the HTTP answer of every batch")
    p.add_argument("--force", action="store_true",
                   help="write even if the target range already contains temperature data")
    return p.parse_args(argv)


def load_env(path: str | None) -> None:
    if path is None:
        path = "private/.env"
    file = Path(path)
    if not file.is_file():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def influx_config(args) -> dict:
    def need(name):
        value = os.environ.get(name, "")
        if not value:
            sys.exit(f"error: {name} is not set (see --env-file or the environment)")
        return value

    return {
        "url": need("INFLUX_URL").rstrip("/"),
        "org": need("INFLUX_ORG"),
        "bucket": args.bucket or need("INFLUX_BUCKET"),
        "token": need(TOKEN_VAR),
    }


def post(cfg: dict, body: str) -> None:
    query = urllib.parse.urlencode({"org": cfg["org"], "bucket": cfg["bucket"], "precision": "ns"})
    req = urllib.request.Request(
        f"{cfg['url']}/api/v2/write?{query}",
        data=body.encode(),
        method="POST",
        headers={"Authorization": f"Token {cfg['token']}",
                 "Content-Type": "text/plain; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if VERBOSE:
                print(f"  HTTP {resp.status} {resp.headers.get('X-Influxdb-Version', '')} "
                      f"server={resp.headers.get('Server', '')}", file=sys.stderr)
            if resp.status not in (200, 204):
                sys.exit(f"error: influx answered HTTP {resp.status}")
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:300]
        sys.exit(f"error: influx rejected the write: HTTP {err.code} {detail}")
    except urllib.error.URLError as err:
        sys.exit(f"error: cannot reach influx: {err.reason}")


def bucket_retention(cfg: dict) -> int:
    """Retention of the target bucket in seconds, 0 means infinite."""
    url = f"{cfg['url']}/api/v2/buckets?name={urllib.parse.quote(cfg['bucket'])}"
    req = urllib.request.Request(url, headers={"Authorization": f"Token {cfg['token']}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode() or "{}")
    except (urllib.error.URLError, ValueError) as err:
        print(f"  warning: could not read bucket settings: {err}", file=sys.stderr)
        return 0
    buckets = data.get("buckets") or []
    if not buckets:
        sys.exit(f"error: bucket {cfg['bucket']} not found or not readable by this token")
    rules = buckets[0].get("retentionRules") or [{}]
    return int(rules[0].get("everySeconds") or 0)


def count_existing(cfg: dict, start: str, stop: str) -> int:
    """How many temp points already exist in that range - guards against duplicates."""
    flux = (f'from(bucket: "{cfg["bucket"]}") |> range(start: {start}, stop: {stop}) '
            f'|> filter(fn: (r) => r._field == "temp") |> count() |> group() |> sum()')
    req = urllib.request.Request(
        f"{cfg['url']}/api/v2/query?org={urllib.parse.quote(cfg['org'])}",
        data=flux.encode(),
        method="POST",
        headers={"Authorization": f"Token {cfg['token']}",
                 "Content-Type": "application/vnd.flux",
                 "Accept": "application/csv"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        print(f"  warning: could not check the target range (HTTP {err.code})", file=sys.stderr)
        return 0
    except urllib.error.URLError as err:
        sys.exit(f"error: cannot reach influx: {err.reason}")

    header, total = None, 0
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        cells = line.split(",")
        if header is None:
            header = cells
            continue
        if len(cells) == len(header) and "_value" in header:
            try:
                total += int(float(cells[header.index("_value")]))
            except ValueError:
                pass
    return total


def escape_measurement(name: str) -> str:
    return name.replace(",", "\\,").replace(" ", "\\ ")


def to_ns(local: datetime.datetime, zone, stats: Stats) -> int:
    """Convert a naive timestamp to UTC nanoseconds.

    zone is either a ZoneInfo (DST-aware) or a fixed datetime.timezone.
    """
    aware = local.replace(tzinfo=zone)
    if isinstance(zone, ZoneInfo) and aware.fold:
        stats.ambiguous += 1
    return int(aware.astimezone(datetime.timezone.utc).timestamp() * 1_000_000_000)


def number(text: str) -> float | None:
    text = (text or "").strip()
    if text.lower() in EMPTY_VALUES:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def main(argv=None) -> int:
    args = parse_args(argv)
    global VERBOSE
    VERBOSE = args.verbose
    load_env(args.env_file)
    cfg = None if not args.write else influx_config(args)

    if args.tz:
        zone = ZoneInfo(args.tz)
        zone_label = args.tz + " (DST-aware)"
    else:
        sign = 1 if args.offset.startswith("+") else -1
        hours, _, minutes = args.offset.lstrip("+-").partition(":")
        zone = datetime.timezone(sign * datetime.timedelta(hours=int(hours),
                                                           minutes=int(minutes or 0)))
        zone_label = f"fixed UTC{args.offset}"
    print(f"  source clock: {zone_label}", file=sys.stderr)
    mapping = dict(DEFAULT_MAP)
    for item in args.map:
        old, _, new = item.partition("=")
        mapping[old.strip()] = new.strip() or old.strip()
    skipping = DEFAULT_SKIP | set(args.skip)

    since = datetime.date.fromisoformat(args.since) if args.since else None
    until = datetime.date.fromisoformat(args.until) if args.until else None
    wanted = set(args.sensor)

    if cfg is not None:
        seconds = bucket_retention(cfg)
        if seconds:
            oldest = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)
            print(f"  warning: bucket {cfg['bucket']} keeps only {seconds / 86400:.0f} days "
                  f"(back to {oldest:%Y-%m-%d}); older points are dropped by InfluxDB",
                  file=sys.stderr)
            if not args.force and (since is None or since < oldest.date()):
                sys.exit("error: the import starts before the bucket's retention - "
                         "raise the retention or use a different bucket")

    if cfg is not None and not args.force:
        start = f"{args.since}T00:00:00Z" if args.since else "1970-01-01T00:00:00Z"
        stop = f"{args.until}T23:59:59Z" if args.until else "2100-01-01T00:00:00Z"
        existing = count_existing(cfg, start, stop)
        if existing:
            sys.exit(f"error: {existing:,} temperature points already exist in "
                     f"{start}..{stop} in bucket {cfg['bucket']}; "
                     f"use --force if that is intended")

    stats = Stats()
    samples: list[str] = []
    lines: list[str] = []

    def flush() -> None:
        if not lines:
            return
        body = "\n".join(lines) + "\n"
        if cfg is not None:
            post(cfg, body)
        stats.written += len(lines)
        stats.batches += 1
        lines.clear()
        if stats.batches % 50 == 0:
            print(f"  ... {stats.written:,} points written", file=sys.stderr, flush=True)

    with tarfile.open(args.src, "r:xz") as archive:
        members = [m for m in archive.getmembers()
                   if m.isfile() and len(Path(m.name).parts) == 3]
        members.sort(key=lambda m: (m.name.split("/")[0], int(m.name.split("/")[1])))
        print(f"  {len(members)} Dateien im Archiv", file=sys.stderr)

        for member in members:
            sensor = Path(member.name).name
            if sensor in skipping or (wanted and sensor not in wanted):
                continue
            target = escape_measurement(mapping.get(sensor, sensor))
            handle = archive.extractfile(member)
            if handle is None:
                continue
            reader = csv.reader(io.TextIOWrapper(handle, encoding="utf-8", errors="replace"))
            next(reader, None)                      # header
            for row in reader:
                if len(row) < 3:
                    stats.unparsable += 1
                    continue
                try:
                    local = datetime.datetime.fromisoformat(row[0].strip())
                except ValueError:
                    stats.unparsable += 1
                    continue
                if (since and local.date() < since) or (until and local.date() > until):
                    continue
                stats.read += 1
                value = number(row[2])
                if value is None:
                    stats.empty += 1
                    continue
                if not -50.0 <= value <= 60.0:
                    stats.out_of_range += 1
                    continue

                fields = [f"temp={repr(value)}"]
                if args.with_setpoint and len(row) > 3:
                    setpoint = number(row[3])
                    if setpoint is not None:
                        fields.append(f"setpoint={repr(setpoint)}")

                line = f"{target} {','.join(fields)} {to_ns(local, zone, stats)}"
                lines.append(line)
                if len(samples) < args.sample:
                    samples.append(line)
                stats.per_sensor[mapping.get(sensor, sensor)] += 1
                stats.per_year[local.year] += 1

                if len(lines) >= args.batch:
                    flush()
                if args.limit and stats.read >= args.limit:
                    break
            if args.limit and stats.read >= args.limit:
                break
    flush()

    print()
    print("  sample lines:")
    for line in samples:
        print(f"    {line}")
    print()
    print(f"  rows read:       {stats.read:,}")
    print(f"  points written:  {stats.written:,}" + ("" if args.write else "   (dry run)"))
    print(f"  skipped empty:   {stats.empty:,}")
    print(f"  unparsable:      {stats.unparsable:,}")
    print(f"  out of range:    {stats.out_of_range:,}")
    print(f"  DST ambiguous:   {stats.ambiguous:,}")
    print(f"  batches:         {stats.batches:,}")
    print()
    print("  per sensor:")
    for name, count in sorted(stats.per_sensor.items()):
        print(f"    {name:<20}{count:>12,}")
    print()
    print("  per year:")
    for year, count in sorted(stats.per_year.items()):
        print(f"    {year:<6}{count:>12,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
