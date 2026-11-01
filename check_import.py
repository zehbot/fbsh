#!/usr/bin/env python3
"""Check what an import actually put into the InfluxDB bucket.

Answers the questions that come up when Grafana shows an empty panel:
is the data there at all, which timestamps does it carry, and does the bucket
keep it long enough?

    python check_import.py
    python check_import.py --env-file /path/to/private/.env --bucket other

Needs no arguments and no credentials on the command line.
"""

from __future__ import annotations

import argparse
import datetime
import socket
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_VAR = "INFLUX_" + "TOKEN"
EXPECTED = {
    "earliest": "2019-01-27",
    "latest": "2024-09-20",
}


def load_env(path: str | None) -> None:
    import os

    file = Path(path or "private/.env")
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


def config(args) -> dict:
    def need(name):
        value = __import__("os").environ.get(name, "")
        if not value:
            sys.exit(f"error: {name} is not set - run this from the repo root "
                     f"(private/.env) or pass --env-file")
        return value

    return {
        "url": need("INFLUX_URL").rstrip("/"),
        "org": need("INFLUX_ORG"),
        "bucket": args.bucket or need("INFLUX_BUCKET"),
        "token": need(TOKEN_VAR),
    }


def request(cfg: dict, path: str, data: bytes | None = None, ctype: str | None = None,
            accept: str = "application/json") -> str:
    headers = {"Authorization": f"Token {cfg['token']}", "Accept": accept}
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(f"{cfg['url']}{path}", data=data, method="POST" if data else "GET",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:200]
        sys.exit(f"error: HTTP {err.code} {detail}")
    except urllib.error.URLError as err:
        sys.exit(f"error: cannot reach {cfg['url']}: {err.reason}")


def flux(cfg: dict, query: str) -> list[dict]:
    """Run a flux query and return its rows as dicts."""
    text = request(cfg, f"/api/v2/query?org={urllib.parse.quote(cfg['org'])}",
                   data=query.encode(), ctype="application/vnd.flux",
                   accept="application/csv")
    header, rows = None, []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        cells = line.split(",")
        if header is None:
            header = cells
            continue
        if len(cells) == len(header):
            rows.append(dict(zip(header, cells)))
    return rows


def bucket_info(cfg: dict) -> dict:
    raw = request(cfg, f"/api/v2/buckets?name={urllib.parse.quote(cfg['bucket'])}")
    data = json.loads(raw or "{}")
    buckets = data.get("buckets") or []
    return buckets[0] if buckets else {}


def show_time(value: str) -> str:
    try:
        stamp = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return value or "-"


def diagnose(cfg: dict) -> None:
    base = (f'from(bucket: "{cfg["bucket"]}") '
            f'|> range(start: 1970-01-01T00:00:00Z) '
            f'|> filter(fn: (r) => r._field == "temp")')

    def grouped(agg: str) -> dict[str, dict]:
        rows = flux(cfg, base + f' |> group(columns: ["_measurement"]) |> {agg}()')
        out = {}
        for row in rows:
            name = row.get("_measurement") or "(none)"
            out[name] = row
        return out

    first = grouped("first")
    last = grouped("last")
    counts = grouped("count")

    print("\n  per measurement (all time):")
    print(f"    {'measurement':<26}{'points':>10}  {'from':<20}{'to':<20}")
    for name in sorted(counts, key=lambda n: -int(float(counts[n].get("_value", 0) or 0))):
        try:
            total = int(float(counts[name]["_value"]))
        except (KeyError, ValueError):
            total = 0
        print(f"    {name:<26}{total:>10,}  "
              f"{show_time(first.get(name, {}).get('_time', '')):<20}"
              f"{show_time(last.get(name, {}).get('_time', '')):<20}")

    print("\n  points per month (all measurements):")
    rows = flux(cfg, base + ' |> aggregateWindow(every: 1mo, fn: count, createEmpty: false) '
                            '|> group(columns: ["_time"]) |> sum()')
    for row in rows[-40:]:
        stamp = (row.get("_time") or "")[:7]
        try:
            value = int(float(row.get("_value", 0) or 0))
        except ValueError:
            continue
        print(f"    {stamp}  {value:>9,}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file")
    parser.add_argument("--window", metavar="START,STOP",
                        help="count temp points in exactly that RFC3339 window")
    parser.add_argument("--tasks", action="store_true", help="list InfluxDB tasks")
    parser.add_argument("--diagnose", action="store_true",
                        help="per measurement: count, first, last, plus a monthly histogram")
    parser.add_argument("--bucket")
    args = parser.parse_args(argv)
    load_env(args.env_file)
    cfg = config(args)

    print(f"bucket: {cfg['bucket']}")

    host = urllib.parse.urlparse(cfg["url"]).hostname or ""
    port = urllib.parse.urlparse(cfg["url"]).port or (443 if cfg["url"].startswith("https") else 80)
    try:
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(host, port)})
        print(f"  host: {host} -> {', '.join(addresses)}"
              + ("   ! several addresses, requests may not all reach the same server"
                 if len(addresses) > 1 else ""))
    except OSError as err:
        print(f"  host: {host} (resolution failed: {err})")

    info = bucket_info(cfg)
    if info:
        rules = info.get("retentionRules") or [{}]
        seconds = int(rules[0].get("everySeconds") or 0)
        infinite = seconds == 0
        label = "infinite" if infinite else f"{seconds} s ({seconds / 86400:.0f} days)"
        print(f"  retention: {label}")
        if not infinite:
            print("  ! a finite retention deletes anything older than that on write")
    else:
        print("  ! bucket not found - check INFLUX_BUCKET and the token's permissions")

    scope = 'from(bucket: "%s") |> range(start: 1970-01-01T00:00:00Z) ' \
            '|> filter(fn: (r) => r._field == "temp")' % cfg["bucket"]

    first = flux(cfg, scope + ' |> group() |> sort(columns: ["_time"]) |> limit(n: 1)')
    last = flux(cfg, scope + ' |> group() |> sort(columns: ["_time"], desc: true) |> limit(n: 1)')
    if not first:
        print("\n  no temperature points at all in this bucket")
        print("  -> the import did not write, or went to another bucket")
        return 1

    print(f"\n  earliest point: {show_time(first[0].get('_time', ''))}")
    print(f"  latest point:   {show_time(last[0].get('_time', ''))}")

    total = flux(cfg, scope + " |> count() |> group() |> sum()")
    if total:
        try:
            print(f"  temp points:    {int(float(total[0]['_value'])):,}")
        except (KeyError, ValueError):
            pass

    per_year = flux(cfg,
                    f'from(bucket: "{cfg["bucket"]}") '
                    f'|> range(start: 2019-01-01T00:00:00Z, stop: 2025-01-01T00:00:00Z) '
                    f'|> filter(fn: (r) => r._field == "temp") '
                    f'|> aggregateWindow(every: 1y, fn: count, createEmpty: false) '
                    f'|> group() |> sum()')
    per_series = flux(cfg,
                      f'from(bucket: "{cfg["bucket"]}") '
                      f'|> range(start: 2019-01-01T00:00:00Z, stop: 2025-01-01T00:00:00Z) '
                      f'|> filter(fn: (r) => r._field == "temp") '
                      f'|> group(columns: ["_measurement"]) |> count()')

    if per_series:
        print("\n  per measurement:")
        for row in sorted(per_series, key=lambda r: -int(float(r.get("_value", 0) or 0))):
            name = row.get("_measurement") or row.get("_field") or "?"
            try:
                print(f"    {name:<24}{int(float(row['_value'])):>12,}")
            except (KeyError, ValueError):
                pass

    if args.tasks:
        raw = request(cfg, f"/api/v2/tasks?org={urllib.parse.quote(cfg['org'])}")
        tasks = (json.loads(raw or "{}").get("tasks") or [])
        print(f"\n  tasks: {len(tasks)}")
        for task in tasks:
            print(f"    {task.get('name','?'):<30}{task.get('status','?'):<10}{task.get('every','')}")
        return 0

    if args.window:
        start, _, stop = args.window.partition(",")
        rows = flux(cfg, f'from(bucket: "{cfg["bucket"]}") '
                         f'|> range(start: {start}, stop: {stop}) '
                         f'|> filter(fn: (r) => r._field == "temp") |> count()')
        total = sum(int(float(r.get("_value", 0) or 0)) for r in rows)
        print(f"\n  temp points in {start} .. {stop}: {total:,}")
        for row in rows[:10]:
            print(f"    {row.get('_measurement','?'):<26}{row.get('_value','?'):>10}")
        return 0

    if args.diagnose:
        diagnose(cfg)
        return 0

    print("\n  verdict:")
    earliest = first[0].get("_time", "")
    if earliest.startswith("19") or earliest.startswith("20"):
        print("    data is present with plausible timestamps")
        print("    -> if Grafana stays empty, set an absolute range "
              "(2019-01-01 .. 2025-01-01); the default 'last 12 hours' shows nothing")
    else:
        print(f"    timestamps look wrong ({earliest}) - report this with the output above")
    return 0


if __name__ == "__main__":
    sys.exit(main())
