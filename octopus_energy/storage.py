from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
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
    _normalize_consumption_times(conn)
    return conn


def utc_iso(instant: str | None) -> str | None:
    """Canonical UTC text (`...Z`), so SQLite string comparisons order `Z` and `+01:00` instants correctly."""
    if not instant:
        return instant
    parsed = datetime.fromisoformat(instant.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_consumption_times(conn: sqlite3.Connection) -> None:
    """Rewrite readings stored with a UTC offset (e.g. BST `+01:00`) in canonical UTC form."""
    rows = conn.execute(
        "SELECT meter_id,interval_start,interval_end,consumption_kwh FROM consumption"
        " WHERE interval_start NOT LIKE '%Z' OR interval_end NOT LIKE '%Z'"
    ).fetchall()
    for meter_id, start, end, kwh in rows:
        conn.execute(
            "DELETE FROM consumption WHERE meter_id=? AND interval_start=? AND interval_end=?", (meter_id, start, end)
        )
        upsert_consumption(conn, meter_id, [{"interval_start": start, "interval_end": end, "consumption": kwh}])
    if rows:
        conn.commit()


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
        (fuel, point_id, serial, tariff_code, utc_iso(valid_from), utc_iso(valid_to), synced_at),
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
        [(meter_id, code, utc_iso(valid_from) or "", utc_iso(valid_to)) for code, valid_from, valid_to in agreements],
    )


def upsert_consumption(conn: sqlite3.Connection, meter_id: int, readings: Iterable[dict]) -> int:
    rows = [(meter_id, utc_iso(r["interval_start"]), utc_iso(r["interval_end"]), r["consumption"]) for r in readings]
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
    rows = [
        (meter_id, tariff_code, kind, utc_iso(r["valid_from"]), utc_iso(r.get("valid_to")), r["value_inc_vat"])
        for r in rates
    ]
    conn.executemany(
        "INSERT INTO tariff_rates(meter_id,tariff_code,kind,valid_from,valid_to,value_inc_vat) VALUES(?,?,?,?,?,?) ON CONFLICT(meter_id,tariff_code,kind,valid_from) DO UPDATE SET valid_to=excluded.valid_to,value_inc_vat=excluded.value_inc_vat",
        rows,
    )
    return len(rows)
