from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .analytics import recompute_daily_costs
from .client import OctopusAPIError, OctopusClient, utc_now
from .dashboard import write_dashboard
from .reports import write_reports
from .storage import (
    GAS_UNIT_CHOICES,
    connect,
    upsert_account,
    upsert_consumption,
    upsert_meter,
    upsert_rate,
    validate_gas_units,
)


def load_env(path: Path) -> dict[str, str]:
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def _watermark(conn: sqlite3.Connection, query: str, params: tuple, default: str) -> str:
    """Resume from the newest stored timestamp; overlap is safe because writes are upserts."""
    return conn.execute(query, params).fetchone()[0] or default


CONSUMPTION_LOOKBACK = timedelta(days=7)


def _iso_utc(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _consumption_resume_from(watermark: str | None, default_from: str) -> str:
    """Re-read a short window before the watermark so late backfilled intervals are picked up.

    The lookback never reaches earlier than the --days window, unless the watermark itself is older.
    """
    if not watermark:
        return default_from
    mark = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
    floor = datetime.fromisoformat(default_from.replace("Z", "+00:00"))
    return _iso_utc(min(mark, max(mark - CONSUMPTION_LOOKBACK, floor)))


def sync(database: Path, account_number: str, api_key: str, days: int = 400, *, gas_units: str = "unknown") -> dict:
    validate_gas_units(gas_units)
    if days <= 0:
        raise ValueError("Sync days must be positive")
    if not account_number.strip() or not api_key.strip():
        raise ValueError("Octopus account number and API key are required")
    started = utc_now()
    conn = connect(database)
    conn.execute("INSERT INTO sync_history(started_at,status) VALUES(?,?)", (started, "running"))
    sync_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    counts = {"electricity": 0, "gas": 0}
    empty_meters = 0
    try:
        client = OctopusClient(api_key)
        account = client.account(account_number)
        meters = client.meters(account)
        if not meters:
            raise OctopusAPIError("Account returned no supported smart meters")
        upsert_account(conn, account_number, started)
        period_to = started
        default_from = _iso_utc(datetime.now(timezone.utc) - timedelta(days=days))
        for meter in meters:
            meter_id = upsert_meter(
                conn,
                fuel=meter.fuel,
                point_id=meter.point_id,
                serial=meter.serial_number,
                tariff_code=meter.tariff_code,
                valid_from=meter.valid_from,
                valid_to=meter.valid_to,
                synced_at=started,
            )
            watermark = conn.execute(
                "SELECT MAX(interval_start) FROM consumption WHERE meter_id=?", (meter_id,)
            ).fetchone()[0]
            consumption_from = _consumption_resume_from(watermark, default_from)
            meter_records = upsert_consumption(conn, meter_id, client.consumption(meter, period_from=consumption_from))
            counts[meter.fuel] += meter_records
            if not meter_records:
                empty_meters += 1
                continue
            for kind in ("standard-unit-rates", "standing-charges"):
                rates_from = _watermark(
                    conn,
                    "SELECT MAX(valid_from) FROM tariff_rates WHERE meter_id=? AND kind=?",
                    (meter_id, kind),
                    default_from,
                )
                if meter.agreements:
                    # Account history repairs tariff changes missed by older syncs.
                    rates_from = default_from
                conn.execute(
                    "DELETE FROM tariff_rates WHERE meter_id=? AND kind=? "
                    "AND julianday(valid_from)<julianday(?) "
                    "AND (valid_to IS NULL OR julianday(valid_to)>julianday(?))",
                    (meter_id, kind, period_to, rates_from),
                )
                upsert_rate(
                    conn,
                    meter_id,
                    kind,
                    client.tariff_rates(meter, kind, period_from=rates_from, period_to=period_to),
                )
        if empty_meters:
            status = "partial" if sum(counts.values()) else "no_data"
            conn.rollback()
            conn.execute(
                "UPDATE sync_history SET ended_at=?,status=?,electricity_records=?,gas_records=? WHERE id=?",
                (utc_now(), status, counts["electricity"], counts["gas"], sync_id),
            )
            conn.commit()
            return {
                "status": status,
                "meters": len(meters),
                "has_readings": bool(sum(counts.values())),
                "empty_meters": empty_meters,
                **counts,
            }
        recompute_daily_costs(conn, gas_units=gas_units)
        conn.execute(
            "UPDATE sync_history SET ended_at=?,status=?,electricity_records=?,gas_records=? WHERE id=?",
            (utc_now(), "success", counts["electricity"], counts["gas"], sync_id),
        )
        conn.commit()
        return {"status": "success", "meters": len(meters), "has_readings": True, "empty_meters": 0, **counts}
    except Exception as exc:
        conn.rollback()
        conn.execute(
            "UPDATE sync_history SET ended_at=?,status=?,error_category=? WHERE id=?",
            (utc_now(), "failed", type(exc).__name__, sync_id),
        )
        conn.commit()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Octopus Energy collection and analysis")
    parser.add_argument("--database", default="data/octopus_energy.sqlite3")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--report-dir", default="reports/energy")
    parser.add_argument("--dashboard-dir", default="reports/energy/dashboard")
    parser.add_argument(
        "--gas-units",
        choices=GAS_UNIT_CHOICES,
        default=None,
        help="Gas source units, overriding GAS_UNITS in the environment or .env. "
        "An unconfigured CLI run uses unknown and reverts previously confirmed gas kWh.",
    )
    args = parser.parse_args()
    try:
        env = {**load_env(Path(args.env_file)), **os.environ}
        gas_units = validate_gas_units(
            args.gas_units if args.gas_units is not None else env.get("GAS_UNITS", "unknown")
        )
        result = sync(Path(args.database), env["ACCOUNT_NUMBER"], env["API_KEY"], args.days, gas_units=gas_units)
        if result["status"] != "success":
            print(f"Octopus read-only sync incomplete: status={result['status']}; previous data and outputs preserved")
            return 1
        write_reports(Path(args.database), Path(args.report_dir), gas_units=gas_units)
        write_dashboard(Path(args.database), Path(args.dashboard_dir))
        print("Octopus read-only sync succeeded: " + ", ".join(f"{key}={value}" for key, value in result.items()))
        return 0
    except (KeyError, OSError, OctopusAPIError, ValueError, sqlite3.Error) as exc:
        print(f"Octopus sync failed safely: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
