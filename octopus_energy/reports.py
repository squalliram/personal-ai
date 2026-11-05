from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .analytics import anomalies, expensive_days, overnight_baseline, recompute_daily_costs, summary
from .storage import connect, get_gas_units, validate_gas_units

LONDON = ZoneInfo("Europe/London")


def _usage(value: float | None) -> str:
    return f"{value:.2f} kWh" if value is not None else "kWh unavailable (gas source units unverified)"


def _cost(value: float | None) -> str:
    return f"£{value:.2f}" if value is not None else "cost unavailable/incomplete"


def write_reports(database: Path, output_dir: Path, *, gas_units: str | None = None) -> tuple[Path, Path]:
    if gas_units is not None:
        validate_gas_units(gas_units)
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(database)
    try:
        with conn:
            recompute_daily_costs(conn, gas_units=gas_units)
        units = get_gas_units(conn)
        today = datetime.now(LONDON).date().isoformat()
        tariffs = [dict(r) for r in conn.execute("SELECT fuel,tariff_code FROM meters ORDER BY fuel")]
        totals = summary(conn)
        costly = expensive_days(conn, limit=5)
        baseline = overnight_baseline(conn)
        flagged = anomalies(conn)
        daily = [f"# Daily Energy Report — {today}", "", "## Stored tariff records"]
        daily += [f"- {r['fuel'].title()}: `{r['tariff_code'] or 'unavailable'}`" for r in tariffs]
        daily += ["", "## Available-period totals"]
        daily += [
            f"- {r['fuel'].title()}: {_usage(r['consumption_kwh'])}; {_cost(r['total_cost_gbp'])}" for r in totals
        ] or ["- No interval data available."]
        daily += [
            "",
            "Costs are unavailable when any required tariff coverage is missing. "
            + (
                "Gas readings are configured as kWh; no volume conversion is applied. "
                if units == "kwh"
                else "Gas source units are unverified, so gas kWh and unit costs are not calculated. "
            )
            + "Totals cover retrieved intervals, not necessarily complete billing days.",
        ]
        daily += ["", "## Overnight baseline"]
        daily += [
            f"- {r['fuel'].title()}: {r['average_interval_kwh']:.3f} kWh per overnight interval ({r['intervals']} intervals)"
            for r in baseline
        ] or ["- No overnight intervals available."]
        daily += ["", "## Consumption anomalies"]
        daily += [
            f"- {r['date']} {r['fuel']}: {r['consumption_kwh']:.2f} kWh (threshold {r['threshold_kwh']:.2f} kWh)"
            for r in flagged
        ] or ["- None detected by the 2σ consumption rule."]
        daily += [
            "",
            "Recommendations are suggestions based on aggregate meter data; they do not attribute usage to appliances.",
        ]
        daily_path = output_dir / f"{today}.md"
        daily_path.write_text("\n".join(daily) + "\n", encoding="utf-8")
        monthly = ["# Monthly Energy Cost Analysis", "", "## Highest-cost days"]
        monthly += [
            f"- {r['date']} {r['fuel']}: {_usage(r['consumption_kwh'])}; {_cost(r['total_cost_gbp'])}" for r in costly
        ] or ["- No complete cost data available."]
        monthly += [
            "",
            "Days with unavailable costs are excluded from this ranking. Costs include retrieved unit rates and one applicable daily standing charge per supply point with readings that day. Historical rate records are retained so rate and consumption changes can be analysed separately.",
        ]
        monthly_path = output_dir / "monthly-cost-analysis.md"
        monthly_path.write_text("\n".join(monthly) + "\n", encoding="utf-8")
        return daily_path, monthly_path
    finally:
        conn.close()
