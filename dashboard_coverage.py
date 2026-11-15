#!/usr/bin/env python3
"""Does the Grafana dashboard cover every sensor that is actually reporting?

Compares the panels of one or more dashboard exports against the measurements and
fields present in InfluxDB, and reports three gaps:

  * sensors in the database that no panel shows
  * panels whose measurement/field no longer exists
  * sensors that stopped reporting

    python dashboard_coverage.py private/*.json
    python dashboard_coverage.py --bucket other --stale-hours 6 private/*.json

Credentials come from private/.env (same keys as the collector). Nothing is
written; the database is only queried.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_VAR = "INFLUX_" + "TOKEN"


def load_env(path: str | None) -> None:
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
        value = os.environ.get(name, "")
        if not value:
            sys.exit(f"error: {name} is not set - run from the repo root or pass --env-file")
        return value

    return {"url": need("INFLUX_URL").rstrip("/"), "org": need("INFLUX_ORG"),
            "bucket": args.bucket or need("INFLUX_BUCKET"), "token": need(TOKEN_VAR)}


def flux(cfg: dict, query: str) -> list[dict]:
    req = urllib.request.Request(
        f"{cfg['url']}/api/v2/query?org={urllib.parse.quote(cfg['org'])}",
        data=query.encode(), method="POST",
        headers={"Authorization": f"Token {cfg['token']}",
                 "Content-Type": "application/vnd.flux", "Accept": "application/csv"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        sys.exit(f"error: influx answered HTTP {err.code}: {err.read().decode()[:200]}")
    except urllib.error.URLError as err:
        sys.exit(f"error: cannot reach influx: {err.reason}")

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


def walk_panels(node):
    """Yield every panel, including those nested in rows."""
    if isinstance(node, list):
        for item in node:
            yield from walk_panels(item)
        return
    if not isinstance(node, dict):
        return
    if node.get("type") and ("targets" in node or "panels" in node):
        yield node
    for key in ("panels",):
        if key in node:
            yield from walk_panels(node[key])


def references(dashboard: dict) -> set[tuple[str, str | None]]:
    """(measurement, field) pairs a dashboard queries."""
    refs: set[tuple[str, str | None]] = set()
    for panel in walk_panels(dashboard.get("panels") or []):
        for target in panel.get("targets") or []:
            measurement = target.get("measurement")
            field = None
            for selection in target.get("select") or []:
                if isinstance(selection, list) and selection:
                    params = selection[0].get("params") or []
                    if params:
                        field = params[0]
            if not measurement:
                # Flux panels keep the query as text
                text = " ".join(str(target.get(k, "")) for k in ("query", "rawSql", "expr"))
                found = re.search(r'_measurement\s*==\s*"([^"]+)"', text)
                if found:
                    measurement = found.group(1)
                ffound = re.search(r'_field\s*==\s*"([^"]+)"', text)
                if ffound:
                    field = ffound.group(1)
            if measurement:
                refs.add((measurement, field))
    return refs


def iso(value: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dashboards", nargs="+", help="dashboard JSON exports")
    parser.add_argument("--bucket")
    parser.add_argument("--env-file")
    parser.add_argument("--stale-hours", type=float, default=24.0)
    args = parser.parse_args(argv)

    load_env(args.env_file)
    cfg = config(args)

    covered: set[tuple[str, str | None]] = set()
    for path in args.dashboards:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        panels = data.get("dashboard", data)          # API wrapper or plain export
        refs = references(panels)
        covered |= refs
        print(f"  {Path(path).name}: {len(refs)} references, title {panels.get('title', '?')!r}")

    inventory_query = (f'from(bucket: "{cfg["bucket"]}") |> range(start: -30d) '
                       f'|> group(columns: ["_measurement", "_field"]) |> last()')
    rows = flux(cfg, inventory_query)

    now = datetime.datetime.now(datetime.timezone.utc)
    present: dict[tuple[str, str], datetime.datetime | None] = {}
    for row in rows:
        measurement, field = row.get("_measurement"), row.get("_field")
        if measurement and field:
            present[(measurement, field)] = iso(row.get("_time", ""))

    def is_covered(measurement: str, field: str) -> bool:
        return (measurement, field) in covered or (measurement, None) in covered

    uncovered, stale = [], []
    for (measurement, field), last in sorted(present.items()):
        if not is_covered(measurement, field):
            uncovered.append((measurement, field, last))
        elif last and (now - last).total_seconds() > args.stale_hours * 3600:
            stale.append((measurement, field, last))

    print(f"\n  bucket {cfg['bucket']}: {len(present)} series in the last 30 days")
    print(f"  dashboard references: {len(covered)}")

    if uncovered:
        print(f"\n  not on any dashboard ({len(uncovered)}):")
        for measurement, field, last in uncovered:
            seen = last.strftime("%Y-%m-%d %H:%M") if last else "?"
            print(f"    {measurement:<26} {field:<14} last {seen}")
    else:
        print("\n  every series in the bucket is shown by a panel")

    if stale:
        print(f"\n  stale, no data in {args.stale_hours:g} h ({len(stale)}):")
        for measurement, field, last in stale:
            print(f"    {measurement:<26} {field:<14} last {last:%Y-%m-%d %H:%M}")

    missing = sorted({(m, f) for m, f in covered
                      if not any(pm == m and (f is None or pf == f) for pm, pf in present)})
    if missing:
        print(f"\n  referenced by a panel but not in this bucket ({len(missing)}):")
        for measurement, field in missing:
            hint = "  (maybe another datasource)" if measurement in ("weather",) else ""
            print(f"    {measurement:<26} {field or '*':<14}{hint}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
