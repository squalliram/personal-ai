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
DEMO_ACCOUNT = "DEMO-ACCOUNT-NOT-REAL"
DEMO_METERS = {
    fuel: (f"DEMO-{fuel.upper()}-POINT", f"DEMO-{fuel.upper()}-METER", f"DEMO-{fuel.upper()}-FIXED")
    for fuel in ("electricity", "gas")
}
DEMO_TABLES = (
    "consumption",
    "tariff_rates",
    "daily_costs",
    "anomalies",
    "savings_recommendations",
    "meters",
    "accounts",
    "sync_history",
    "energy_settings",
)
REFUSE_OVERWRITE = "Refusing to overwrite a database that is not exclusively synthetic demo data."


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _has_only_demo_data(conn: sqlite3.Connection) -> bool:
    """Fail closed if a sync or another application has added non-demo data."""
    try:
        objects = {
            tuple(row) for row in conn.execute("SELECT type,name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'")
        }
        if objects != {("table", name) for name in DEMO_TABLES}:
            return False
        settings = dict(conn.execute("SELECT name,value FROM energy_settings"))
        if set(settings) != {"synthetic_demo", "demo_seed", "demo_end_date", "gas_units"}:
            return False
        if settings["synthetic_demo"] != DEMO_MARKER or settings["gas_units"] != "kwh":
            return False
        end_date = date.fromisoformat(settings["demo_end_date"])
        if (
            end_date.isoformat() != settings["demo_end_date"]
            or str(int(settings["demo_seed"])) != settings["demo_seed"]
        ):
            return False
        start = datetime.combine(end_date - timedelta(days=DAYS - 1), datetime.min.time(), LONDON)
        stop = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), LONDON)
        synced_at = _utc(stop + timedelta(hours=1))
        accounts = [tuple(row) for row in conn.execute("SELECT account_number,payload_json,synced_at FROM accounts")]
        if accounts != [(DEMO_ACCOUNT, "{}", synced_at)]:
            return False
        meters = [
            tuple(row)
            for row in conn.execute(
                "SELECT id,fuel,point_id,serial_number,tariff_code,valid_from,valid_to,synced_at FROM meters ORDER BY id"
            )
        ]
        expected_meters = [
            (index, fuel, *identity, _utc(start), None, synced_at)
            for index, (fuel, identity) in enumerate(DEMO_METERS.items(), 1)
        ]
        if meters != expected_meters or conn.execute("PRAGMA foreign_key_check").fetchone():
            return False
        intervals = int((stop.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / 1800)
        for table, count in (("consumption", intervals), ("tariff_rates", 4)):
            counts = [
                tuple(row)
                for row in conn.execute(f"SELECT meter_id,COUNT(*) FROM {table} GROUP BY meter_id ORDER BY meter_id")
            ]
            if counts != [(1, count), (2, count)]:
                return False
        history = [
            tuple(row)
            for row in conn.execute(
                "SELECT started_at,ended_at,status,electricity_records,gas_records,error_category FROM sync_history"
            )
        ]
        if history != [(synced_at, synced_at, "success", intervals, intervals, None)]:
            return False
        for table in ("anomalies", "savings_recommendations"):
            if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return False
        return not conn.execute("SELECT 1 FROM daily_costs WHERE fuel NOT IN ('electricity','gas') LIMIT 1").fetchone()
    except (sqlite3.DatabaseError, ValueError, TypeError, OverflowError):
        return False


def _is_demo_database(database: Path) -> bool:
    try:
        # Immutable mode ignores a live WAL and can miss newly synced identities.
        conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            conn.execute("BEGIN")
            return _has_only_demo_data(conn)
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
    replacing = database.exists()
    if replacing:
        if not force:
            raise FileExistsError("Database exists; use --force only to regenerate a synthetic demo.")
        if not _is_demo_database(database):
            raise FileExistsError(REFUSE_OVERWRITE)

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
            # Revalidate after locking out writers, not just in the read-only preflight.
            conn.execute("BEGIN IMMEDIATE")
            if replacing and not _has_only_demo_data(conn):
                raise FileExistsError(REFUSE_OVERWRITE)
            conn.execute("CREATE TABLE IF NOT EXISTS energy_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            if not replacing and any(
                conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in DEMO_TABLES
            ):
                raise FileExistsError(REFUSE_OVERWRITE)
            for table in DEMO_TABLES:
                conn.execute(f"DELETE FROM {table}")
            conn.executemany(
                "INSERT INTO energy_settings(name,value) VALUES(?,?)",
                (("synthetic_demo", DEMO_MARKER), ("demo_seed", str(seed)), ("demo_end_date", end_date.isoformat())),
            )
            upsert_account(conn, DEMO_ACCOUNT, synced_at)
            for fuel, prices in (("electricity", ((24.5, 58), (26.2, 62))), ("gas", ((6.3, 31), (6.8, 32)))):
                point_id, serial, tariff_code = DEMO_METERS[fuel]
                meter = upsert_meter(
                    conn,
                    fuel=fuel,
                    point_id=point_id,
                    serial=serial,
                    tariff_code=tariff_code,
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
