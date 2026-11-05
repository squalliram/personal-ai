from __future__ import annotations

import sqlite3
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

from .storage import get_gas_units, set_gas_units, validate_gas_units

LONDON = ZoneInfo("Europe/London")


def _instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("Energy timestamps must include an offset")
    return instant.astimezone(timezone.utc)


def _local_date(instant: str) -> str:
    return _instant(instant).astimezone(LONDON).date().isoformat()


class _Rates:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.rows: dict[tuple[int, str], list[tuple]] = defaultdict(list)
        for row in conn.execute("SELECT * FROM tariff_rates"):
            self.rows[row["meter_id"], row["kind"]].append(
                (
                    _instant(row["valid_from"]),
                    _instant(row["valid_to"]) if row["valid_to"] else None,
                    float(row["value_inc_vat"]),
                )
            )
        for rows in self.rows.values():
            rows.sort(key=lambda row: row[0])
        self.starts = {key: [row[0] for row in rows] for key, rows in self.rows.items()}

    def value(self, meter_id: int, kind: str, start: datetime, end: datetime | None = None) -> float | None:
        key = (meter_id, kind)
        rows = self.rows.get(key, [])
        starts = self.starts.get(key, [])
        cursor, value = start, None
        while True:
            index = bisect_right(starts, cursor) - 1
            applicable = next((row for row in reversed(rows[: index + 1]) if row[1] is None or row[1] > cursor), None)
            if applicable is None:
                return None
            if value is not None and applicable[2] != value:
                # Usage within an interval is unknown; never invent a split
                # when a rate changes partway through that interval.
                return None
            value = applicable[2]
            if end is None:
                return value
            boundary = min(end, applicable[1] or end)
            if index + 1 < len(rows):
                boundary = min(boundary, starts[index + 1])
            if boundary >= end:
                return value
            cursor = boundary


def _rate(conn: sqlite3.Connection, meter_id: int, kind: str, instant: str) -> float | None:
    return _Rates(conn).value(meter_id, kind, _instant(instant))


def recompute_daily_costs(conn: sqlite3.Connection, gas_units: str | None = None) -> None:
    units = validate_gas_units(gas_units) if gas_units is not None else None
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT energy_recompute")
    try:
        if units is None:
            units = get_gas_units(conn)
        set_gas_units(conn, units)
        _recompute_daily_costs(conn, units)
    except Exception:
        conn.execute("ROLLBACK TO energy_recompute")
        conn.execute("RELEASE energy_recompute")
        if started_transaction:
            conn.rollback()
        raise
    conn.execute("RELEASE energy_recompute")


def _recompute_daily_costs(conn: sqlite3.Connection, gas_units: str) -> None:
    conn.execute("DELETE FROM daily_costs")
    rows = conn.execute("SELECT c.*, m.fuel, m.point_id FROM consumption c JOIN meters m ON m.id=c.meter_id").fetchall()
    rates = _Rates(conn)
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for row in rows:
        groups[_local_date(row["interval_start"]), row["fuel"]].append(row)
    for (day, fuel), readings in groups.items():
        kwh = sum(row["consumption_kwh"] for row in readings) if fuel == "electricity" or gas_units == "kwh" else None
        unit_cost = 0.0 if kwh is not None else None
        points: dict[str, list] = defaultdict(list)
        for row in readings:
            points[row["point_id"]].append(row)
            if unit_cost is not None:
                rate = rates.value(
                    row["meter_id"],
                    "standard-unit-rates",
                    _instant(row["interval_start"]),
                    _instant(row["interval_end"]),
                )
                unit_cost = unit_cost + row["consumption_kwh"] * rate / 100 if rate is not None else None
        standing = 0.0
        midnight = datetime.fromisoformat(day).replace(tzinfo=LONDON).astimezone(timezone.utc)
        for point_readings in points.values():
            # A replacement meter does not introduce another standing charge.
            # Agreement dates are not meter installation dates. Only supplies
            # evidenced by readings for this day participate in this charge.
            first = min(point_readings, key=lambda row: _instant(row["interval_start"]))
            rate = rates.value(first["meter_id"], "standing-charges", midnight)
            if rate is None:
                standing = None
                break
            standing += rate / 100
        total = unit_cost + standing if unit_cost is not None and standing is not None else None
        conn.execute("INSERT INTO daily_costs VALUES(?,?,?,?,?,?)", (day, fuel, kwh, unit_cost, standing, total))


def summary(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> list[dict]:
    where, params = "", []
    filters = []
    if start:
        filters.append("date>=?")
        params.append(start)
    if end:
        filters.append("date<?")
        params.append(end)
    if filters:
        where = " WHERE " + " AND ".join(filters)
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT fuel, CASE WHEN COUNT(consumption_kwh)=COUNT(*) THEN SUM(consumption_kwh) END consumption_kwh,"
            " CASE WHEN COUNT(total_cost_gbp)=COUNT(*) THEN ROUND(SUM(total_cost_gbp),2) END total_cost_gbp"
            " FROM daily_costs" + where + " GROUP BY fuel",
            params,
        )
    ]
    if get_gas_units(conn) != "kwh":
        for row in rows:
            if row["fuel"] == "gas":
                row["consumption_kwh"] = row["total_cost_gbp"] = None
    return rows


def expensive_days(conn: sqlite3.Connection, fuel: str | None = None, limit: int = 10) -> list[dict]:
    sql, params = (
        "SELECT * FROM daily_costs WHERE total_cost_gbp IS NOT NULL AND (fuel!='gas' OR ?='kwh')",
        [get_gas_units(conn)],
    )
    if fuel:
        sql += " AND fuel=?"
        params.append(fuel)
    sql += " ORDER BY total_cost_gbp DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def anomalies(conn: sqlite3.Connection, fuel: str | None = None) -> list[dict]:
    sql, params = (
        "SELECT date, fuel, consumption_kwh FROM daily_costs "
        "WHERE consumption_kwh IS NOT NULL AND (fuel!='gas' OR ?='kwh')",
        [get_gas_units(conn)],
    )
    if fuel:
        sql += " AND fuel=?"
        params.append(fuel)
    data = [dict(r) for r in conn.execute(sql, params)]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in data:
        grouped[row["fuel"]].append(row)
    found = []
    for rows in grouped.values():
        values = [r["consumption_kwh"] for r in rows]
        if len(values) < 7 or pstdev(values) == 0:
            continue
        threshold = mean(values) + 2 * pstdev(values)
        found.extend({**r, "threshold_kwh": round(threshold, 3)} for r in rows if r["consumption_kwh"] > threshold)
    return found


def overnight_baseline(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT m.fuel, c.interval_start, c.consumption_kwh FROM consumption c "
        "JOIN meters m ON m.id=c.meter_id WHERE m.fuel='electricity' OR ?='kwh'",
        (get_gas_units(conn),),
    ).fetchall()
    groups: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        hour = datetime.fromisoformat(row["interval_start"].replace("Z", "+00:00")).astimezone(LONDON).hour
        if 0 <= hour < 5:
            groups[row["fuel"]].append(row["consumption_kwh"])
    return [
        {"fuel": fuel, "average_interval_kwh": round(mean(values), 4), "intervals": len(values)}
        for fuel, values in groups.items()
        if values
    ]
