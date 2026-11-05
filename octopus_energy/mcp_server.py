"""Read-only energy tools over the MCP stdio transport."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

from .analytics import anomalies, expensive_days, overnight_baseline, summary

TOOLS = ("get_energy_summary", "get_daily_consumption", "get_monthly_bill_comparison", "get_tariff_details", "get_expensive_days", "get_overnight_baseline", "detect_energy_anomalies", "get_savings_recommendations")
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
DATE_SCHEMA = {"type": "string", "format": "date", "description": "Europe/London date, YYYY-MM-DD; end is exclusive."}
FUEL_SCHEMA = {"type": "string", "enum": ["electricity", "gas"]}
TOOL_PROPERTIES = {
    "get_energy_summary": {"start": DATE_SCHEMA, "end": DATE_SCHEMA},
    "get_daily_consumption": {"start": DATE_SCHEMA, "end": DATE_SCHEMA},
    "get_monthly_bill_comparison": {},
    "get_tariff_details": {},
    "get_expensive_days": {"fuel": FUEL_SCHEMA, "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 10}},
    "get_overnight_baseline": {},
    "detect_energy_anomalies": {"fuel": FUEL_SCHEMA},
    "get_savings_recommendations": {},
}
TOOL_DESCRIPTIONS = {
    "get_energy_summary": "Read available-period totals, optionally bounded by start and/or end dates.",
    "get_daily_consumption": "Read daily consumption and estimated costs within a start/end date range.",
    "get_monthly_bill_comparison": "Read monthly consumption and estimated cost totals, not actual bills.",
    "get_tariff_details": "Read stored tariff details.",
    "get_expensive_days": "Read the highest estimated-cost days.",
    "get_overnight_baseline": "Read the overnight whole-home consumption baseline.",
    "detect_energy_anomalies": "Read unusual whole-home consumption days.",
    "get_savings_recommendations": "Read stored savings suggestions, if available.",
}


class RefreshRequiredError(RuntimeError):
    pass


def dispatch(name: str, arguments: dict, database: Path) -> list[dict]:
    if name not in TOOLS: raise ValueError("Unknown read-only energy tool")
    if not isinstance(arguments, dict): raise ValueError("Arguments must be an object")
    conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if any(row["notnull"] for row in conn.execute("PRAGMA table_info(daily_costs)")
               if row["name"] not in {"date", "fuel"}):
            # Read tools must not migrate legacy derived zeros/gas quantities.
            raise RefreshRequiredError
        if name == "get_energy_summary": return summary(conn, arguments.get("start"), arguments.get("end"))
        if name == "get_daily_consumption":
            return [dict(r) for r in conn.execute("SELECT date,fuel,consumption_kwh,total_cost_gbp FROM daily_costs WHERE date>=? AND date<? ORDER BY date", (arguments["start"], arguments["end"]))]
        if name == "get_monthly_bill_comparison":
            return [dict(r) for r in conn.execute(
                "SELECT substr(date,1,7) month,fuel,"
                " CASE WHEN COUNT(consumption_kwh)=COUNT(*) THEN SUM(consumption_kwh) END consumption_kwh,"
                " CASE WHEN COUNT(total_cost_gbp)=COUNT(*) THEN ROUND(SUM(total_cost_gbp),2) END total_cost_gbp"
                " FROM daily_costs GROUP BY month,fuel ORDER BY month DESC")]
        if name == "get_tariff_details": return [dict(r) for r in conn.execute("SELECT fuel,tariff_code,valid_from,valid_to FROM meters")]
        if name == "get_expensive_days": return expensive_days(conn, arguments.get("fuel"), int(arguments.get("limit", 10)))
        if name == "get_overnight_baseline": return overnight_baseline(conn)
        if name == "detect_energy_anomalies": return anomalies(conn, arguments.get("fuel"))
        return [dict(r) for r in conn.execute("SELECT category,recommendation,estimated_gbp FROM savings_recommendations ORDER BY created_at DESC")]
    finally: conn.close()


def tool_definitions() -> list[dict]:
    definitions = []
    for name in TOOLS:
        schema = {"type": "object", "properties": TOOL_PROPERTIES[name], "additionalProperties": False}
        if name == "get_daily_consumption":
            schema["required"] = ["start", "end"]
        definitions.append({"name": name, "description": TOOL_DESCRIPTIONS[name], "inputSchema": schema,
                            "annotations": {"readOnlyHint": True, "openWorldHint": False}})
    return definitions


def valid_arguments(name: str, arguments: object) -> bool:
    if not isinstance(arguments, dict) or arguments.keys() - TOOL_PROPERTIES[name].keys():
        return False
    if name == "get_daily_consumption" and not {"start", "end"} <= arguments.keys():
        return False
    for key, value in arguments.items():
        if key in {"start", "end"}:
            if not isinstance(value, str):
                return False
            try:
                if date.fromisoformat(value).isoformat() != value:
                    return False
            except ValueError:
                return False
        elif key == "fuel" and value not in ("electricity", "gas"):
            return False
        elif key == "limit" and (type(value) is not int or not 1 <= value <= 1000):
            return False
    return not {"start", "end"} <= arguments.keys() or arguments["start"] < arguments["end"]


def error_response(request_id: str | int | None, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class MCPServer:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.negotiated = False
        self.ready = False

    def handle(self, request: object) -> dict | None:
        if not isinstance(request, dict):
            return error_response(None, -32600, "Invalid request")
        request_id = request.get("id")
        if (request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str)
                or ("id" in request and type(request_id) not in (str, int))):
            return error_response(None, -32600, "Invalid request")
        method = request["method"]
        if "id" not in request:
            if method == "notifications/initialized" and self.negotiated:
                self.ready = True
            return None
        params = request.get("params", {})
        if not isinstance(params, dict):
            return error_response(request_id, -32602, "Invalid parameters")
        if method == "initialize":
            info = params.get("clientInfo")
            if (self.negotiated or not isinstance(params.get("protocolVersion"), str)
                    or not params["protocolVersion"] or not isinstance(params.get("capabilities"), dict)
                    or not isinstance(info, dict)
                    or not all(isinstance(info.get(key), str) and info[key] for key in ("name", "version"))):
                return error_response(request_id, -32602, "Invalid initialization parameters")
            version = params["protocolVersion"]
            self.negotiated = True
            result = {"protocolVersion": version if version in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "personal-ai-octopus-energy", "version": "1.0.0"}}
        elif method == "ping":
            result = {}
        elif method not in ("tools/list", "tools/call"):
            return error_response(request_id, -32601, "Method not found")
        elif not self.ready:
            return error_response(request_id, -32002, "Server not initialized")
        elif method == "tools/list":
            if params.get("cursor") is not None:
                return error_response(request_id, -32602, "Invalid parameters")
            result = {"tools": tool_definitions()}
        else:
            name, arguments = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or name not in TOOLS or not valid_arguments(name, arguments):
                return error_response(request_id, -32602, "Invalid tool parameters")
            try:
                rows = dispatch(name, arguments, self.database)
                result = {"content": [{"type": "text", "text": json.dumps(rows, allow_nan=False)}]}
            except RefreshRequiredError:
                result = {"content": [{"type": "text", "text": "Energy database requires a local refresh before queries are available."}], "isError": True}
            except (OSError, sqlite3.Error):
                result = {"content": [{"type": "text", "text": "Energy database is unavailable."}], "isError": True}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def reject_nonfinite(value: str) -> None:
    raise ValueError("Invalid JSON number")


def main() -> int:
    server = MCPServer(Path(sys.argv[1] if len(sys.argv) > 1 else "data/octopus_energy.sqlite3"))
    for line in sys.stdin:
        try:
            request = json.loads(line, parse_constant=reject_nonfinite)
        except (ValueError, RecursionError):
            response = error_response(None, -32700, "Parse error")
        else:
            try:
                response = server.handle(request)
            except Exception:
                request_id = request.get("id") if isinstance(request, dict) else None
                response = error_response(request_id, -32603, "Internal error")
        if response is not None:
            print(json.dumps(response, separators=(",", ":"), allow_nan=False), flush=True)
    return 0


if __name__ == "__main__": raise SystemExit(main())
