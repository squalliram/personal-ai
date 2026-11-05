from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .analytics import anomalies, expensive_days, recompute_daily_costs, summary
from .client import OctopusAPIError, OctopusClient, utc_now
from .storage import connect, upsert_account, upsert_consumption, upsert_meter, upsert_rate
from .reports import write_reports
from .dashboard import write_dashboard


def load_env(path: Path) -> dict[str, str]:
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1); values[key.strip()] = value.strip().strip("\"'")
    return values


def sync(database: Path, account_number: str, api_key: str, days: int = 400) -> dict:
    if days <= 0:
        raise ValueError("Sync days must be positive")
    if not account_number.strip() or not api_key.strip():
        raise ValueError("Octopus account number and API key are required")
    started = utc_now(); conn = connect(database)
    conn.execute("INSERT INTO sync_history(started_at,status) VALUES(?,?)", (started, "running")); sync_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    counts = {"electricity": 0, "gas": 0}
    empty_meters = 0
    try:
        client = OctopusClient(api_key)
        account = client.account(account_number)
        meters = client.meters(account)
        if not meters: raise OctopusAPIError("Account returned no supported smart meters")
        upsert_account(conn, account_number, started)
        period_to = started
        period_from = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        for meter in meters:
            meter_id = upsert_meter(conn, fuel=meter.fuel, point_id=meter.point_id, serial=meter.serial_number, tariff_code=meter.tariff_code, valid_from=meter.valid_from, valid_to=meter.valid_to, synced_at=started)
            meter_records = 0
            for reading in client.consumption(meter, period_from=period_from):
                upsert_consumption(conn, meter_id, reading); counts[meter.fuel] += 1; meter_records += 1
            if not meter_records:
                empty_meters += 1
                continue
            conn.execute("DELETE FROM tariff_rates WHERE meter_id=? AND julianday(valid_from)<julianday(?) AND (valid_to IS NULL OR julianday(valid_to)>julianday(?))", (meter_id, period_to, period_from))
            for kind in ("standard-unit-rates", "standing-charges"):
                for rate in client.tariff_rates(meter, kind, period_from=period_from, period_to=period_to): upsert_rate(conn, meter_id, kind, rate)
        if empty_meters:
            status = "partial" if sum(counts.values()) else "no_data"
            conn.rollback()
            conn.execute("UPDATE sync_history SET ended_at=?,status=?,electricity_records=?,gas_records=? WHERE id=?", (utc_now(), status, counts["electricity"], counts["gas"], sync_id))
            conn.commit()
            return {"status": status, "meters": len(meters), "has_readings": bool(sum(counts.values())), "empty_meters": empty_meters, **counts}
        recompute_daily_costs(conn)
        conn.execute("UPDATE sync_history SET ended_at=?,status=?,electricity_records=?,gas_records=? WHERE id=?", (utc_now(), "success", counts["electricity"], counts["gas"], sync_id))
        conn.commit()
        return {"status": "success", "meters": len(meters), "has_readings": True, "empty_meters": 0, **counts}
    except Exception as exc:
        conn.rollback()
        conn.execute("UPDATE sync_history SET ended_at=?,status=?,error_category=? WHERE id=?", (utc_now(), "failed", type(exc).__name__, sync_id)); conn.commit()
        raise
    finally: conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Octopus Energy collection and analysis")
    parser.add_argument("--database", default="data/octopus_energy.sqlite3")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--report-dir", default="reports/energy")
    parser.add_argument("--dashboard-dir", default="reports/energy/dashboard")
    args = parser.parse_args()
    try:
        env = {**load_env(Path(args.env_file)), **os.environ}
        result = sync(Path(args.database), env["ACCOUNT_NUMBER"], env["API_KEY"], args.days)
        if result["status"] != "success":
            print(f"Octopus read-only sync incomplete: status={result['status']}; previous data and outputs preserved")
            return 1
        write_reports(Path(args.database), Path(args.report_dir))
        write_dashboard(Path(args.database), Path(args.dashboard_dir))
        print("Octopus read-only sync succeeded: " + ", ".join(f"{key}={value}" for key, value in result.items()))
        return 0
    except (KeyError, OSError, OctopusAPIError, ValueError, sqlite3.Error) as exc:
        print(f"Octopus sync failed safely: {type(exc).__name__}")
        return 1


if __name__ == "__main__": raise SystemExit(main())
