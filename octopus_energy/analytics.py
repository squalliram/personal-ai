from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")


def _local_date(instant: str) -> str:
    return datetime.fromisoformat(instant.replace("Z", "+00:00")).astimezone(LONDON).date().isoformat()


def _rate(conn: sqlite3.Connection, meter_id: int, kind: str, instant: str) -> float:
    """Rate of the tariff whose agreement covers `instant`, so a previous tariff's rows never apply."""
    row = conn.execute(
        "SELECT r.value_inc_vat FROM tariff_rates r JOIN agreements a ON a.meter_id=r.meter_id AND a.tariff_code=r.tariff_code"
        " WHERE r.meter_id=? AND r.kind=? AND a.valid_from<=? AND (a.valid_to IS NULL OR a.valid_to>?)"
        " AND r.valid_from<=? AND (r.valid_to IS NULL OR r.valid_to>?) ORDER BY a.valid_from DESC, r.valid_from DESC LIMIT 1",
        (meter_id, kind, instant, instant, instant, instant),
    ).fetchone()
    return float(row[0]) if row else 0.0


def recompute_daily_costs(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM daily_costs")
    rows = conn.execute(
        "SELECT c.*, m.id meter_id, m.fuel FROM consumption c JOIN meters m ON m.id=c.meter_id"
    ).fetchall()
    totals: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
    for row in rows:
        key = (_local_date(row["interval_start"]), row["fuel"])
        totals[key][0] += row["consumption_kwh"]
        totals[key][1] += (
            row["consumption_kwh"] * _rate(conn, row["meter_id"], "standard-unit-rates", row["interval_start"]) / 100
        )
    for (day, fuel), (kwh, unit_cost) in totals.items():
        meter_ids = [r[0] for r in conn.execute("SELECT id FROM meters WHERE fuel=?", (fuel,))]
        standing = sum(_rate(conn, meter_id, "standing-charges", day + "T00:00:00Z") / 100 for meter_id in meter_ids)
        conn.execute(
            "INSERT INTO daily_costs VALUES(?,?,?,?,?,?)", (day, fuel, kwh, unit_cost, standing, unit_cost + standing)
        )


def summary(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> list[dict]:
    where, params = "", []
    if start and end:
        where, params = " WHERE date>=? AND date<?", [start, end]
    return [
        dict(row)
        for row in conn.execute(
            "SELECT fuel, SUM(consumption_kwh) consumption_kwh, ROUND(SUM(total_cost_gbp), 2) total_cost_gbp FROM daily_costs"
            + where
            + " GROUP BY fuel",
            params,
        )
    ]


def expensive_days(conn: sqlite3.Connection, fuel: str | None = None, limit: int = 10) -> list[dict]:
    sql, params = "SELECT * FROM daily_costs", []
    if fuel:
        sql += " WHERE fuel=?"
        params.append(fuel)
    sql += " ORDER BY total_cost_gbp DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def anomalies(conn: sqlite3.Connection, fuel: str | None = None) -> list[dict]:
    sql, params = "SELECT date, fuel, consumption_kwh FROM daily_costs", []
    if fuel:
        sql += " WHERE fuel=?"
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
        "SELECT m.fuel, c.interval_start, c.consumption_kwh FROM consumption c JOIN meters m ON m.id=c.meter_id"
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
