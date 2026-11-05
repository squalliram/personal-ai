"""Minimal stdio JSON-RPC MCP-compatible read-only tool dispatcher."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .analytics import anomalies, expensive_days, overnight_baseline, summary
from .storage import connect

TOOLS = (
    "get_energy_summary",
    "get_daily_consumption",
    "get_monthly_bill_comparison",
    "get_tariff_details",
    "get_expensive_days",
    "get_overnight_baseline",
    "detect_energy_anomalies",
    "get_savings_recommendations",
)


def dispatch(name: str, arguments: dict, database: Path) -> list[dict]:
    if name not in TOOLS:
        raise ValueError("Unknown read-only energy tool")
    if not isinstance(arguments, dict):
        raise ValueError("Arguments must be an object")
    conn = connect(database)
    try:
        if name == "get_energy_summary":
            return summary(conn, arguments.get("start"), arguments.get("end"))
        if name == "get_daily_consumption":
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT date,fuel,consumption_kwh,total_cost_gbp FROM daily_costs WHERE date>=? AND date<? ORDER BY date",
                    (arguments["start"], arguments["end"]),
                )
            ]
        if name == "get_monthly_bill_comparison":
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT substr(date,1,7) month,fuel,SUM(consumption_kwh) consumption_kwh,ROUND(SUM(total_cost_gbp),2) total_cost_gbp FROM daily_costs GROUP BY month,fuel ORDER BY month DESC"
                )
            ]
        if name == "get_tariff_details":
            return [dict(r) for r in conn.execute("SELECT fuel,tariff_code,valid_from,valid_to FROM meters")]
        if name == "get_expensive_days":
            return expensive_days(conn, arguments.get("fuel"), int(arguments.get("limit", 10)))
        if name == "get_overnight_baseline":
            return overnight_baseline(conn)
        if name == "detect_energy_anomalies":
            return anomalies(conn, arguments.get("fuel"))
        return [
            dict(r)
            for r in conn.execute(
                "SELECT category,recommendation,estimated_gbp FROM savings_recommendations ORDER BY created_at DESC"
            )
        ]
    finally:
        conn.close()


def main() -> int:
    database = Path(sys.argv[1] if len(sys.argv) > 1 else "data/octopus_energy.sqlite3")
    for line in sys.stdin:
        try:
            request = json.loads(line)
            name = request["params"]["name"]
            result = dispatch(name, request["params"].get("arguments", {}), database)
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "result": {"content": [{"type": "text", "text": json.dumps(result)}]},
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            print(
                json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32602, "message": str(exc)}}), flush=True
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
