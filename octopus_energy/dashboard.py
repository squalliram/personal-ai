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
<title>Energy intelligence</title><style>
:root{--bg:#07111f;--card:#111c2e;--ink:#e8eef8;--muted:#9aacc2;--line:#273a55;--e:#60a5fa;--g:#fb923c;--accent:#2dd4bf}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px system-ui}
main{max-width:1200px;margin:auto;padding:32px 18px}h1{margin:0;font-size:clamp(2rem,5vw,3.5rem)}h2{font-size:1.1rem}
.muted{color:var(--muted);line-height:1.5}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin:20px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px}.number{font-size:2rem;font-weight:750}
button{color:var(--ink);background:#172640;border:1px solid var(--line);border-radius:8px;padding:9px 12px;margin:2px;cursor:pointer}
button:disabled{opacity:.6;cursor:wait}.active{background:var(--accent);color:#06201d;border-color:var(--accent)}
svg{width:100%;height:290px;display:block}.tip{border-left:4px solid var(--accent);padding-left:12px}
a{color:var(--e)}[data-state="warning"],[data-state="error"]{color:#fbbf24}
</style></head><body><main>
<p class="muted">READ-ONLY OCTOPUS INTELLIGENCE</p><h1>Energy decisions, not just readings.</h1>
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
<section class="card"><h2 id="title">Consumption</h2>
<p id="range" class="muted"></p><svg id="chart" viewBox="0 0 900 290" role="img" aria-label="Consumption chart"></svg></section>
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
