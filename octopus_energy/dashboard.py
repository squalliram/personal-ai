"""Privacy-preserving static, interactive dashboard export."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .storage import connect, get_gas_units

LONDON = ZoneInfo("Europe/London")


def dashboard_data(database: Path) -> dict:
    conn = connect(database)
    try:
        gas_units = get_gas_units(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(daily_costs)")}
        quality = ",cost_status,cost_reason" if {"cost_status", "cost_reason"} <= columns else ""
        daily = [
            dict(row)
            for row in conn.execute(
                "SELECT date,fuel,ROUND(consumption_kwh,3) kwh,"
                "ROUND(total_cost_gbp,2) cost,unit_cost_gbp,standing_charge_gbp"
                f"{quality} FROM daily_costs ORDER BY date,fuel"
            )
        ]
        for row in daily:
            unit_cost, standing_cost = row.pop("unit_cost_gbp"), row.pop("standing_charge_gbp")
            if row["fuel"] == "gas" and gas_units != "kwh":
                row["kwh"] = row["cost"] = unit_cost = None
            if not quality:
                reasons = []
                if row["fuel"] == "gas" and row["kwh"] is None:
                    reasons.append("unknown_gas_units")
                elif unit_cost is None:
                    reasons.append("missing_unit_rate")
                if standing_cost is None:
                    reasons.append("missing_standing_charge")
                row["cost_status"] = "incomplete" if row["cost"] is None else "complete"
                row["cost_reason"] = ",".join(reasons) or None
        raw = conn.execute(
            "SELECT m.fuel,c.interval_start,c.interval_end,c.consumption_kwh "
            "FROM consumption c JOIN meters m ON m.id=c.meter_id "
            "ORDER BY c.interval_start"
        ).fetchall()
        hours = defaultdict(lambda: defaultdict(lambda: [0.0, []]))
        latest = {}
        for row in raw:
            start = datetime.fromisoformat(row["interval_start"].replace("Z", "+00:00")).astimezone(timezone.utc)
            end = datetime.fromisoformat(row["interval_end"].replace("Z", "+00:00")).astimezone(timezone.utc)
            duration = (end - start).total_seconds()
            if duration <= 0:
                continue
            fuel = row["fuel"]
            latest[fuel] = max(latest.get(fuel, end), end)
            # Weight by actual duration, including hourly readings and DST changes.
            cursor = start
            while cursor < end:
                boundary = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                stop = min(end, boundary)
                seconds = (stop - cursor).total_seconds()
                local = cursor.astimezone(LONDON)
                # Gas is kWh only after an explicit, persisted source-unit confirmation.
                if fuel == "electricity" or gas_units == "kwh":
                    bucket = hours[fuel][local.hour]
                    bucket[0] += float(row["consumption_kwh"]) * seconds / duration
                    bucket[1].append((cursor, stop))
                cursor = stop

        def covered_hours(spans):
            seconds = 0.0
            covered_until = None
            for start, end in sorted(spans):
                # Concurrent meters add energy, not duplicate household time.
                uncovered = start if covered_until is None else max(start, covered_until)
                seconds += max(0.0, (end - uncovered).total_seconds())
                covered_until = end if covered_until is None else max(end, covered_until)
            return seconds / 3600

        hourly = [
            {"fuel": fuel, "hour": hour, "kwh": round(kwh / covered_hours(spans), 3)}
            for fuel, buckets in sorted(hours.items())
            for hour, (kwh, spans) in sorted(buckets.items())
        ]
        sync = conn.execute(
            "SELECT ended_at FROM sync_history WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "generated_at": datetime.now(LONDON).isoformat(),
            "timezone": "Europe/London",
            "latest_reading_at": {fuel: end.astimezone(LONDON).isoformat() for fuel, end in latest.items()},
            "last_successful_sync_at": sync["ended_at"] if sync else None,
            "daily": daily,
            "hourly": hourly,
            "notice": "Whole-home meter data cannot identify individual appliances. Add smart-plug data for device attribution.",
        }
    finally:
        conn.close()


SCRIPT = Path(__file__).with_name("dashboard.js").read_text(encoding="utf-8")

HTML = (
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Energy intelligence</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,600;0,700;1,400;1,600&amp;family=DM+Sans:wght@300;400;500&amp;family=Syne:wght@400;500;600;700;800&amp;display=swap">
<style>
:root{color-scheme:dark;--bg:#08080a;--card:#0f0f13;--raised:#16161d;--ink:#f2f2f5;--muted:#a8a8c0;--line:#3a3a52;--e:#2ee8cc;--g:#e8cc9a;--accent:#c9a96e;--gold-light:#e8cc9a;--serif:'Cormorant Garamond',Georgia,serif;--sans:'Syne',system-ui,sans-serif;--body:'DM Sans',system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px var(--body);line-height:1.6}
main{max-width:1280px;margin:auto;padding:5rem 2rem}h1{margin:0 0 1rem;font:400 clamp(2.6rem,6vw,5rem)/1.06 var(--serif);max-width:960px}h1 em{color:var(--accent);font-weight:400}h2,h3,h4{font-family:var(--sans);line-height:1.35}h2{font-size:1.05rem}h3{font-size:1.1rem}h4{font-size:.9rem}
.eyebrow{color:var(--accent);font:600 .75rem var(--sans);letter-spacing:.16em;margin:0 0 1.25rem}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:1.5rem;margin:1.5rem 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:1.5rem}.number{font:600 2.15rem var(--sans);color:var(--gold-light)}
button{color:var(--ink);background:var(--raised);border:1px solid var(--muted);border-radius:8px;padding:11px 16px;margin:3px;min-height:44px;font:500 .9rem var(--body);cursor:pointer}
button:hover{border-color:var(--accent)}button:disabled{opacity:.6;cursor:wait}.active{background:var(--accent);color:var(--bg);border-color:var(--accent)}
button:focus-visible,a:focus-visible{outline:2px solid var(--e);outline-offset:4px}.tip{border-left:3px solid var(--accent);padding-left:14px}
a{color:var(--e)}[data-state="warning"],[data-state="error"]{color:var(--gold-light)}
.chart-card{margin-top:1.5rem}.chart-card h2{font-size:1.3rem}.chart-stage{position:relative}.chart-scroll{overflow-x:auto}
#chart{display:block;width:100%;min-width:720px;height:auto;color:var(--e)}#chart[data-fuel="gas"]{color:var(--g)}
.chart-axis{stroke:var(--muted);fill:none;stroke-width:1.2}.chart-grid{stroke:var(--line);stroke-dasharray:3 6}
.chart-tick{fill:var(--muted);font:14px var(--body)}.chart-axis-title{fill:var(--ink);font:500 15px var(--body)}
.chart-series{fill:none;stroke:currentColor;stroke-width:2.5;stroke-linejoin:round;stroke-linecap:round}
.chart-point{cursor:crosshair;outline:none}.point-hit{fill:transparent}.point-dot{fill:currentColor}.point-high{fill:var(--card);stroke:var(--accent);stroke-width:2}
.chart-point:focus .point-dot{stroke:var(--ink);stroke-width:3}.chart-crosshair{stroke:var(--muted);stroke-width:1;stroke-dasharray:4 5;pointer-events:none}
.chart-marker{fill:var(--card);stroke:currentColor;stroke-width:3;pointer-events:none}#chart-crosshair{pointer-events:none}
.chart-tooltip{position:absolute;z-index:2;width:360px;max-width:calc(100% - 16px);max-height:min(70vh,440px);overflow:auto;background:var(--raised);border:1px solid var(--accent);border-radius:12px;padding:18px;box-shadow:0 16px 50px #0009;font-size:.875rem}
.chart-tooltip[hidden]{display:none}.chart-tooltip h3{margin:0 0 8px;color:var(--gold-light)}.chart-tooltip h4{margin:14px 0 6px}.chart-tooltip p{margin:8px 0}.tooltip-range{color:var(--muted);font-size:.8rem}
.chart-tooltip dl{margin:12px 0}.chart-tooltip dl div{display:flex;justify-content:space-between;gap:16px}.chart-tooltip dt{color:var(--muted)}.chart-tooltip dd{margin:0;text-align:right;font-weight:500}.chart-tooltip ul{padding-left:18px}.chart-tooltip li{margin:6px 0}
.high-point{border-top:1px solid var(--line)}.high-point h4{color:var(--gold-light)}#chart-help,#chart-method{font-size:.85rem}
@media(max-width:640px){main{padding:3rem 1rem}.card{padding:1rem}.grid{gap:1rem}.chart-tooltip{font-size:.8rem}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
</style></head><body><main>
<p class="eyebrow">READ-ONLY OCTOPUS INTELLIGENCE</p><h1>Energy decisions,<br><em>not just readings.</em></h1>
<p id="status" role="status" aria-live="polite">Loading dashboard data…</p>
<p id="updated" class="muted"></p>
<button id="refresh" type="button">Reload published data</button>
<p class="muted">Checks this published snapshot every 5 minutes. This page does not sync Octopus or provide live meter readings.</p>
<section class="card" aria-label="Chart filters"><b>Fuel</b><p>
<button data-fuel="electricity" class="active" aria-pressed="true">Electricity</button>
<button data-fuel="gas" aria-pressed="false">Gas</button></p><b>Period</b><p>
<button data-period="day" class="active" aria-pressed="true">Daily</button>
<button data-period="week" aria-pressed="false">Weekly</button>
<button data-period="month" aria-pressed="false">Monthly</button>
<button data-period="year" aria-pressed="false">Yearly</button></p></section>
<section class="grid" id="cards"></section>
<section class="card chart-card"><h2 id="title">Consumption</h2>
<p id="range" class="muted"></p>
<p id="chart-help" class="muted">Hover or tap a point for its recorded period.
Keyboard: Tab to a point, arrows to move, Home/End for endpoints, Enter/Space to show, Escape to dismiss.
Ringed points are unusually high; gaps mean usage is unavailable. Scroll the chart horizontally on small screens.</p>
<div class="chart-stage" id="chart-stage"><div class="chart-scroll" id="chart-scroll">
<svg id="chart" viewBox="0 0 960 400" role="group" aria-label="Consumption chart" aria-describedby="chart-help chart-method"></svg>
</div><div id="chart-tooltip" class="chart-tooltip" role="tooltip" aria-live="polite" aria-atomic="true" hidden></div></div>
<p id="chart-method" class="muted">High-point rule: strictly above mean + 2 × population standard deviation,
reusing the energy analytics rule. Calculated separately for each fuel and selected period in kWh per period,
using at least 7 complete past numeric periods from the latest 90 plotted periods (including the candidate).
Null, missing-date and ongoing periods are excluded. Equal values produce no high points.
Likely contributors appear only on high points and are estimates, not measured devices:
fuel, calendar season at the period midpoint (whole years are mixed-season), historical hourly peak where available,
and uplift of at least 50% versus a smaller uplift. Season is not observed weather; an hourly profile is not that point's timing.</p></section>
<section class="grid"><article class="card"><h2>Suggested next action</h2><p id="action" class="tip"></p></article>
<article class="card"><h2>Confirmed Octopus events only</h2><p class="muted">No live event feed is connected.
For an invited Power Down session, delay flexible demand such as laundry or EV charging.
For Power Up or Happy Hour, move flexible demand into the exact window confirmed in your Octopus account.
Never turn off medical, frost-protection or essential devices. Do not increase usage just to chase a reward.</p>
<p><a href="https://octopus.energy/saving-sessions/" target="_blank" rel="noopener noreferrer">Octopus event guidance</a></p></article>
<article class="card"><h2>Device insight</h2><p id="notice" class="muted"></p></article></section>
<noscript>Enable JavaScript to load this private dashboard.</noscript></main><script>"""
    + SCRIPT
    + "</script></body></html>"
)


def write_dashboard(database: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "data.json").write_text(json.dumps(dashboard_data(database)), encoding="utf-8")
    out = output_dir / "index.html"
    out.write_text(HTML, encoding="utf-8")
    return out
