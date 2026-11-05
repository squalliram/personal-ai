from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
GAS_UNIT_CHOICES = ("kwh", "unknown")


def validate_gas_units(value: str) -> str:
    if value not in GAS_UNIT_CHOICES:
        raise ValueError("Gas units must be kwh or unknown")
    return value


def get_gas_units(conn: sqlite3.Connection) -> str:
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='energy_settings'").fetchone()
    if table is None:
        return "unknown"
    row = conn.execute("SELECT value FROM energy_settings WHERE name='gas_units'").fetchone()
    return validate_gas_units(row[0]) if row else "unknown"


def set_gas_units(conn: sqlite3.Connection, value: str) -> None:
    """Write unit metadata inside the caller's recompute transaction."""
    validate_gas_units(value)
    conn.execute("CREATE TABLE IF NOT EXISTS energy_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO energy_settings(name,value) VALUES('gas_units',?) "
        "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
        (value,),
    )


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        if any(
            row["notnull"]
            for row in conn.execute("PRAGMA table_info(daily_costs)")
            if row["name"] not in {"date", "fuel"}
        ):
            # These are derived values. Rebuild them from retained readings/rates
            # instead of retaining legacy zeros and unverified gas-kWh amounts.
            from .analytics import recompute_daily_costs

            with conn:
                conn.execute("BEGIN")
                conn.execute("DROP TABLE daily_costs")
                conn.execute(
                    "CREATE TABLE daily_costs (date TEXT NOT NULL, fuel TEXT NOT NULL, consumption_kwh REAL, unit_cost_gbp REAL, standing_charge_gbp REAL, total_cost_gbp REAL, PRIMARY KEY(date,fuel))"
                )
                recompute_daily_costs(conn)
    except Exception:
        conn.close()
        raise
    return conn


def upsert_account(conn: sqlite3.Connection, account_number: str, synced_at: str) -> None:
    conn.execute(
        "INSERT INTO accounts(account_number, payload_json, synced_at) VALUES(?,?,?) ON CONFLICT(account_number) DO UPDATE SET synced_at=excluded.synced_at",
        (account_number, "{}", synced_at),
    )


def upsert_meter(
    conn: sqlite3.Connection,
    *,
    fuel: str,
    point_id: str,
    serial: str,
    tariff_code: str | None,
    valid_from: str | None,
    valid_to: str | None,
    synced_at: str,
) -> int:
    conn.execute(
        "INSERT INTO meters(fuel,point_id,serial_number,tariff_code,valid_from,valid_to,synced_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(fuel,point_id,serial_number) DO UPDATE SET tariff_code=excluded.tariff_code,valid_from=excluded.valid_from,valid_to=excluded.valid_to,synced_at=excluded.synced_at",
        (fuel, point_id, serial, tariff_code, valid_from, valid_to, synced_at),
    )
    return conn.execute(
        "SELECT id FROM meters WHERE fuel=? AND point_id=? AND serial_number=?", (fuel, point_id, serial)
    ).fetchone()[0]


def upsert_consumption(conn: sqlite3.Connection, meter_id: int, readings: dict | Iterable[dict]) -> int:
    if isinstance(readings, dict):
        readings = (readings,)
    rows = [(meter_id, r["interval_start"], r["interval_end"], r["consumption"]) for r in readings]
    conn.executemany(
        "INSERT INTO consumption(meter_id,interval_start,interval_end,consumption_kwh) VALUES(?,?,?,?) ON CONFLICT(meter_id,interval_start,interval_end) DO UPDATE SET consumption_kwh=excluded.consumption_kwh",
        rows,
    )
    return len(rows)


def upsert_rate(conn: sqlite3.Connection, meter_id: int, kind: str, rates: dict | Iterable[dict]) -> int:
    if isinstance(rates, dict):
        rates = (rates,)
    rows = [(meter_id, kind, r["valid_from"], r.get("valid_to"), r["value_inc_vat"]) for r in rates]
    conn.executemany(
        "INSERT INTO tariff_rates(meter_id,kind,valid_from,valid_to,value_inc_vat) VALUES(?,?,?,?,?) ON CONFLICT(meter_id,kind,valid_from) DO UPDATE SET valid_to=excluded.valid_to,value_inc_vat=excluded.value_inc_vat",
        rows,
    )
    return len(rows)
