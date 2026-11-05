#!/usr/bin/env python3
"""Generate a public-safe, offline energy demo, never a copy of a household DB.

The seed and end date reproduce the same 400 days of fictional readings and
tariffs. Export timestamps still reflect when the reports/dashboard were made.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
DEFAULT_END_DATE = date(2026, 11, 4)
DEMO_MARKER = "energy-demo-data-v1"
DAYS = 400


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_demo_database(database: Path) -> bool:
    try:
        conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            row = conn.execute("SELECT value FROM energy_settings WHERE name='synthetic_demo'").fetchone()
            return row == (DEMO_MARKER,)
        finally:
            conn.close()
    except (sqlite3.DatabaseError, OSError):
        return False


def _readings(day: date, fuel: str, kwh: float) -> list[dict]:
    cursor = datetime.combine(day, datetime.min.time(), LONDON).astimezone(timezone.utc)
    stop = datetime.combine(day + timedelta(days=1), datetime.min.time(), LONDON).astimezone(timezone.utc)
    intervals = []
    weights = []
    while cursor < stop:
        local = cursor.astimezone(LONDON)
        hour = local.hour + local.minute / 60 + 0.25
        morning = math.exp(-(((hour - 7.5) / 1.5) ** 2))
        evening = math.exp(-(((hour - 18.5) / 2.2) ** 2))
        weights.append(
            0.5 + 0.8 * morning + 1.8 * evening if fuel == "electricity" else 0.2 + 3 * morning + 2 * evening
        )
        intervals.append((cursor, cursor + timedelta(minutes=30)))
        cursor += timedelta(minutes=30)
    values = [round(kwh * weight / sum(weights), 6) for weight in weights]
    values[-1] = round(kwh - sum(values[:-1]), 6)
    return [
        {"interval_start": _utc(start), "interval_end": _utc(end), "consumption": value}
        for (start, end), value in zip(intervals, values, strict=True)
    ]


def generate_demo(
    database: Path,
    output_dir: Path,
    *,
    seed: int = 2026,
    end_date: date = DEFAULT_END_DATE,
    force: bool = False,
) -> dict:
    """Write deterministic fake data, then use the real report/export entry points."""
    if database.exists():
        if not force:
            raise FileExistsError("Database exists; use --force only to regenerate a synthetic demo.")
        if not _is_demo_database(database):
            raise FileExistsError("Refusing to overwrite a database not created by this demo generator.")

    # Support invocation by absolute path from outside the repository.
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from octopus_energy.dashboard import write_dashboard
    from octopus_energy.reports import write_reports
    from octopus_energy.storage import connect, upsert_account, upsert_consumption, upsert_meter, upsert_rate

    rng = random.Random(seed)
    start_date = end_date - timedelta(days=DAYS - 1)
    start = datetime.combine(start_date, datetime.min.time(), LONDON)
    stop = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), LONDON)
    change = datetime.combine(start_date + timedelta(days=DAYS // 2), datetime.min.time(), LONDON)
    synced_at = _utc(stop + timedelta(hours=1))
    counts = {}
    conn = connect(database)
    try:
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS energy_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for table in (
                "consumption",
                "tariff_rates",
                "daily_costs",
                "anomalies",
                "savings_recommendations",
                "meters",
                "accounts",
                "sync_history",
                "energy_settings",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.executemany(
                "INSERT INTO energy_settings(name,value) VALUES(?,?)",
                (("synthetic_demo", DEMO_MARKER), ("demo_seed", str(seed)), ("demo_end_date", end_date.isoformat())),
            )
            upsert_account(conn, "DEMO-ACCOUNT-NOT-REAL", synced_at)
            for fuel, prices in (("electricity", ((24.5, 58), (26.2, 62))), ("gas", ((6.3, 31), (6.8, 32)))):
                meter = upsert_meter(
                    conn,
                    fuel=fuel,
                    point_id=f"DEMO-{fuel.upper()}-POINT",
                    serial=f"DEMO-{fuel.upper()}-METER",
                    tariff_code=f"DEMO-{fuel.upper()}-FIXED",
                    valid_from=_utc(start),
                    valid_to=None,
                    synced_at=synced_at,
                )
                for beginning, ending, (unit, standing) in (
                    (start, change, prices[0]),
                    (change, None, prices[1]),
                ):
                    for kind, value in (("standard-unit-rates", unit), ("standing-charges", standing)):
                        upsert_rate(
                            conn,
                            meter,
                            kind,
                            {
                                "valid_from": _utc(beginning),
                                "valid_to": _utc(ending) if ending else None,
                                "value_inc_vat": value,
                            },
                        )
                counts[fuel] = 0
                for index in range(DAYS):
                    day = start_date + timedelta(days=index)
                    season = math.cos(2 * math.pi * (day.timetuple().tm_yday - 15) / 365.25)
                    kwh = (
                        8.8 + season + (0.8 if day.weekday() >= 5 else 0) + rng.uniform(-0.6, 0.6)
                        if fuel == "electricity"
                        else 20 + 13 * season + rng.uniform(-1.5, 1.5)
                    )
                    if day == end_date - timedelta(days=7 if fuel == "electricity" else 3):
                        kwh *= 4.2 if fuel == "electricity" else 3.6
                    counts[fuel] += upsert_consumption(conn, meter, _readings(day, fuel, round(kwh, 6)))
            conn.execute(
                "INSERT INTO sync_history(started_at,ended_at,status,electricity_records,gas_records) VALUES(?,?,?,?,?)",
                (synced_at, synced_at, "success", counts["electricity"], counts["gas"]),
            )
    finally:
        conn.close()

    write_reports(database, output_dir, gas_units="kwh")
    dashboard = write_dashboard(database, output_dir)
    # A persistent badge keeps scrolled screenshots visibly fictional, too.
    badge = (
        '<div role="note" style="position:fixed;top:12px;right:12px;z-index:100;'
        "padding:6px 10px;border:1px solid #c9a96e;border-radius:6px;background:#08080a;"
        'color:#e8cc9a;font:600 11px system-ui;letter-spacing:.08em">'
        "SYNTHETIC DEMO · FAKE DATA</div>"
    )
    dashboard.write_text(dashboard.read_text(encoding="utf-8").replace("<body>", "<body>" + badge, 1), encoding="utf-8")
    data_path = output_dir / "data.json"
    data = json.loads(data_path.read_text(encoding="utf-8"))
    data["notice"] = (
        "Entirely synthetic demo, generated locally without credentials or provider requests. "
        "All readings, tariffs and identifiers are fictional. Whole-home data cannot identify devices."
    )
    data_path.write_text(json.dumps(data), encoding="utf-8")
    return {
        "synthetic": True,
        "seed": seed,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "days": DAYS,
        "intervals": counts,
        "database": str(database),
        "dashboard": str(dashboard),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True, help="A new, separate synthetic SQLite DB.")
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="Directory for the static dashboard and reports."
    )
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument(
        "--end-date", type=date.fromisoformat, default=DEFAULT_END_DATE, help="Last included London date."
    )
    parser.add_argument("--force", action="store_true", help="Regenerate only a DB previously made by this tool.")
    args = parser.parse_args()
    try:
        result = generate_demo(
            args.database,
            args.output_dir,
            seed=args.seed,
            end_date=args.end_date,
            force=args.force,
        )
    except FileExistsError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
