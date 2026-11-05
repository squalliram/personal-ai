from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .analytics import anomalies, expensive_days, overnight_baseline, summary
from .storage import connect

LONDON = ZoneInfo("Europe/London")


def write_reports(database: Path, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(database)
    try:
        today = datetime.now(LONDON).date().isoformat()
        tariffs = [dict(r) for r in conn.execute("SELECT fuel,tariff_code FROM meters ORDER BY fuel")]
        totals = summary(conn)
        costly = expensive_days(conn, limit=5)
        baseline = overnight_baseline(conn)
        flagged = anomalies(conn)
        daily = [f"# Daily Energy Report — {today}", "", "## Active tariffs"]
        daily += [f"- {r['fuel'].title()}: `{r['tariff_code'] or 'unavailable'}`" for r in tariffs]
        daily += ["", "## Available-period totals"]
        daily += [
            f"- {r['fuel'].title()}: {r['consumption_kwh']:.2f} kWh; £{r['total_cost_gbp']:.2f}" for r in totals
        ] or ["- No interval data available."]
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
            f"- {r['date']} {r['fuel']}: {r['consumption_kwh']:.2f} kWh; £{r['total_cost_gbp']:.2f}" for r in costly
        ] or ["- No cost data available."]
        monthly += [
            "",
            "Costs include retrieved unit rates and one applicable daily standing charge per fuel/day. Historical rate records are retained so rate and consumption changes can be analysed separately.",
        ]
        monthly_path = output_dir / "monthly-cost-analysis.md"
        monthly_path.write_text("\n".join(monthly) + "\n", encoding="utf-8")
        return daily_path, monthly_path
    finally:
        conn.close()
