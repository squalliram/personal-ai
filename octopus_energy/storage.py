from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _drop_unscoped_rates(conn)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _drop_unscoped_rates(conn: sqlite3.Connection) -> None:
    """Drop pre-tariff-scoped rate rows; they are public tariff data and are re-fetched on the next sync."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tariff_rates)")}
    if columns and "tariff_code" not in columns:
        conn.execute("DROP TABLE tariff_rates")


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
    meter_id = conn.execute(
        "SELECT id FROM meters WHERE fuel=? AND point_id=? AND serial_number=?", (fuel, point_id, serial)
    ).fetchone()[0]
    if tariff_code:
        upsert_agreements(conn, meter_id, [(tariff_code, valid_from, valid_to)])
    return meter_id


def upsert_agreements(
    conn: sqlite3.Connection, meter_id: int, agreements: Iterable[tuple[str, str | None, str | None]]
) -> None:
    """Store (tariff_code, valid_from, valid_to) periods so costs use the tariff active at each interval."""
    conn.executemany(
        "INSERT INTO agreements(meter_id,tariff_code,valid_from,valid_to) VALUES(?,?,?,?) ON CONFLICT(meter_id,tariff_code,valid_from) DO UPDATE SET valid_to=excluded.valid_to",
        [(meter_id, code, valid_from or "", valid_to) for code, valid_from, valid_to in agreements],
    )


def upsert_consumption(conn: sqlite3.Connection, meter_id: int, readings: Iterable[dict]) -> int:
    rows = [(meter_id, r["interval_start"], r["interval_end"], r["consumption"]) for r in readings]
    conn.executemany(
        "INSERT INTO consumption(meter_id,interval_start,interval_end,consumption_kwh) VALUES(?,?,?,?) ON CONFLICT(meter_id,interval_start,interval_end) DO UPDATE SET consumption_kwh=excluded.consumption_kwh",
        rows,
    )
    return len(rows)


def upsert_rate(
    conn: sqlite3.Connection, meter_id: int, kind: str, rates: Iterable[dict], tariff_code: str | None = None
) -> int:
    """Store rates for `tariff_code`, defaulting to the meter's current tariff."""
    if tariff_code is None:
        tariff_code = conn.execute("SELECT tariff_code FROM meters WHERE id=?", (meter_id,)).fetchone()[0]
    rows = [(meter_id, tariff_code, kind, r["valid_from"], r.get("valid_to"), r["value_inc_vat"]) for r in rates]
    conn.executemany(
        "INSERT INTO tariff_rates(meter_id,tariff_code,kind,valid_from,valid_to,value_inc_vat) VALUES(?,?,?,?,?,?) ON CONFLICT(meter_id,tariff_code,kind,valid_from) DO UPDATE SET valid_to=excluded.valid_to,value_inc_vat=excluded.value_inc_vat",
        rows,
    )
    return len(rows)
