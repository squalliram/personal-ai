from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .analytics import recompute_daily_costs
from .client import Agreement, OctopusAPIError, OctopusClient, utc_now
from .dashboard import write_dashboard
from .reports import write_reports
from .storage import (
    connect,
    upsert_account,
    upsert_agreements,
    upsert_consumption,
    upsert_meter,
    upsert_rate,
    utc_iso,
)


def load_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
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


def _parse(instant: str) -> datetime:
    return datetime.fromisoformat(instant.replace("Z", "+00:00"))


def sync(database: Path, account_number: str, api_key: str, days: int = 400) -> dict:
    started = utc_now()
    conn = connect(database)
    conn.execute("INSERT INTO sync_history(started_at,status) VALUES(?,?)", (started, "running"))
    sync_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    counts = {"electricity": 0, "gas": 0}
    try:
        client = OctopusClient(api_key)
        account = client.account(account_number)
        meters = client.meters(account)
        if not meters:
            raise OctopusAPIError("Account returned no supported smart meters")
        upsert_account(conn, account_number, started)
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
            counts[meter.fuel] += upsert_consumption(
                conn, meter_id, client.consumption(meter, period_from=consumption_from)
            )
            agreements = meter.agreements or (
                (Agreement(meter.tariff_code, meter.valid_from, meter.valid_to),) if meter.tariff_code else ()
            )
            upsert_agreements(conn, meter_id, [(a.tariff_code, a.valid_from, a.valid_to) for a in agreements])
            for agreement in agreements:
                window_from = default_from
                if agreement.valid_from and _parse(agreement.valid_from) > _parse(default_from):
                    window_from = utc_iso(agreement.valid_from)
                window_to = utc_iso(agreement.valid_to)
                if window_to and _parse(window_to) <= _parse(window_from):
                    continue
                for kind in ("standard-unit-rates", "standing-charges"):
                    # Only this agreement's window counts: the same tariff code can recur in another agreement.
                    latest = conn.execute(
                        "SELECT valid_from, valid_to FROM tariff_rates WHERE meter_id=? AND tariff_code=? AND kind=?"
                        " AND valid_from>=? AND (? IS NULL OR valid_from<?) ORDER BY valid_from DESC LIMIT 1",
                        (meter_id, agreement.tariff_code, kind, window_from, window_to, window_to),
                    ).fetchone()
                    if latest and window_to and (latest[1] is None or latest[1] >= window_to):
                        continue
                    rates_from = latest[0] if latest else window_from
                    rates = client.tariff_rates(
                        meter,
                        kind,
                        period_from=rates_from,
                        period_to=agreement.valid_to,
                        tariff_code=agreement.tariff_code,
                    )
                    upsert_rate(conn, meter_id, kind, rates, tariff_code=agreement.tariff_code)
        recompute_daily_costs(conn)
        conn.execute(
            "UPDATE sync_history SET ended_at=?,status=?,electricity_records=?,gas_records=? WHERE id=?",
            (utc_now(), "success", counts["electricity"], counts["gas"], sync_id),
        )
        conn.commit()
        return {"status": "success", "meters": len(meters), **counts}
    except Exception as exc:
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
    args = parser.parse_args()
    env = {**load_env(Path(args.env_file)), **os.environ}
    try:
        result = sync(Path(args.database), env["ACCOUNT_NUMBER"], env["API_KEY"], args.days)
        write_reports(Path(args.database), Path(args.report_dir))
        write_dashboard(Path(args.database), Path(args.dashboard_dir))
        print("Octopus read-only sync succeeded: " + ", ".join(f"{key}={value}" for key, value in result.items()))
        return 0
    except (KeyError, OSError, OctopusAPIError, ValueError) as exc:
        print(f"Octopus sync failed safely: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
