from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        if any(row["notnull"] for row in conn.execute("PRAGMA table_info(daily_costs)")
               if row["name"] not in {"date", "fuel"}):
            # These are derived values. Rebuild them from retained readings/rates
            # instead of retaining legacy zeros and unverified gas-kWh amounts.
            from .analytics import recompute_daily_costs
            with conn:
                conn.execute("BEGIN")
                conn.execute("DROP TABLE daily_costs")
                conn.execute("CREATE TABLE daily_costs (date TEXT NOT NULL, fuel TEXT NOT NULL, consumption_kwh REAL, unit_cost_gbp REAL, standing_charge_gbp REAL, total_cost_gbp REAL, PRIMARY KEY(date,fuel))")
                recompute_daily_costs(conn)
    except Exception:
        conn.close()
        raise
    return conn


def upsert_account(conn: sqlite3.Connection, account_number: str, synced_at: str) -> None:
    conn.execute("INSERT INTO accounts(account_number, payload_json, synced_at) VALUES(?,?,?) ON CONFLICT(account_number) DO UPDATE SET synced_at=excluded.synced_at", (account_number, "{}", synced_at))


def upsert_meter(conn: sqlite3.Connection, *, fuel: str, point_id: str, serial: str, tariff_code: str | None, valid_from: str | None, valid_to: str | None, synced_at: str) -> int:
    conn.execute("INSERT INTO meters(fuel,point_id,serial_number,tariff_code,valid_from,valid_to,synced_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(fuel,point_id,serial_number) DO UPDATE SET tariff_code=excluded.tariff_code,valid_from=excluded.valid_from,valid_to=excluded.valid_to,synced_at=excluded.synced_at", (fuel, point_id, serial, tariff_code, valid_from, valid_to, synced_at))
    return conn.execute("SELECT id FROM meters WHERE fuel=? AND point_id=? AND serial_number=?", (fuel, point_id, serial)).fetchone()[0]


def upsert_consumption(conn: sqlite3.Connection, meter_id: int, reading: dict) -> None:
    conn.execute("INSERT INTO consumption(meter_id,interval_start,interval_end,consumption_kwh) VALUES(?,?,?,?) ON CONFLICT(meter_id,interval_start,interval_end) DO UPDATE SET consumption_kwh=excluded.consumption_kwh", (meter_id, reading["interval_start"], reading["interval_end"], reading["consumption"]))


def upsert_rate(conn: sqlite3.Connection, meter_id: int, kind: str, rate: dict) -> None:
    conn.execute("INSERT INTO tariff_rates(meter_id,kind,valid_from,valid_to,value_inc_vat) VALUES(?,?,?,?,?) ON CONFLICT(meter_id,kind,valid_from) DO UPDATE SET valid_to=excluded.valid_to,value_inc_vat=excluded.value_inc_vat", (meter_id, kind, rate["valid_from"], rate.get("valid_to"), rate["value_inc_vat"]))
