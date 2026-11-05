"""Generate a self-contained, read-only energy dashboard from local meter data."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")


def _local(instant: str) -> datetime:
    return datetime.fromisoformat(instant.replace("Z", "+00:00")).astimezone(LONDON)


def _rows(conn: sqlite3.Connection, query: str, parameters: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(query, parameters)]


def dashboard_data(database: Path) -> dict:
    """Return display-safe aggregates; never export account, meter, or serial identifiers."""
    from .storage import connect

    conn = connect(database)
    try:
        daily = _rows(conn, """SELECT date,fuel,ROUND(consumption_kwh,3) consumption_kwh,
            ROUND(total_cost_gbp,2) total_cost_gbp FROM daily_costs
            WHERE date >= date('now','-89 days') ORDER BY date""")
        raw = _rows(conn, """SELECT m.fuel,c.interval_start,c.consumption_kwh
            FROM consumption c JOIN meters m ON m.id=c.meter_id ORDER BY c.interval_start""")
        hours: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
        weekdays: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
        for row in raw:
            point = _local(row["interval_start"])
            hours[row["fuel"]][point.hour].append(float(row["consumption_kwh"]))
            weekdays[row["fuel"]][point.weekday()].append(float(row["consumption_kwh"]))
        hourly = [{"fuel": fuel, "hour": hour, "average_kwh": round(mean(values) * 2, 3), "intervals": len(values)}
                  for fuel, values_by_hour in hours.items() for hour, values in sorted(values_by_hour.items())]
        by_weekday = [{"fuel": fuel, "weekday": weekday, "average_kwh": round(mean(values) * 48, 3)}
                      for fuel, values_by_day in weekdays.items() for weekday, values in sorted(values_by_day.items())]
        electricity_hours = {row["hour"]: row["average_kwh"] for row in hourly if row["fuel"] == "electricity"}
        peak_hour = max(electricity_hours, key=electricity_hours.get) if electricity_hours else None
        peak_value = electricity_hours.get(peak_hour, 0) if peak_hour is not None else 0
        recent = [{"at": _local(row["interval_start"]).isoformat(), "fuel": row["fuel"], "kwh": round(float(row["consumption_kwh"]), 3)}
                  for row in raw[-672:]]
        tariffs = _rows(conn, "SELECT fuel,tariff_code,synced_at FROM meters ORDER BY fuel")
        return {
            "generated_at": datetime.now(LONDON).isoformat(), "timezone": "Europe/London",
            "daily": daily, "hourly": hourly, "weekdays": by_weekday, "recent": recent,
            "tariffs": tariffs,
            "insights": {
                "electricity_peak_hour": peak_hour,
                "electricity_peak_kwh_per_hour": peak_value,
                "power_down_tip": "For a Power Down event, move flexible electric loads away from the invited window; do not turn off safety-critical equipment.",
                "happy_hour_tip": "Use confirmed Happy Hour or Power Up slots for flexible loads such as laundry, dishwashing, EV charging, or pre-heating. Check the Octopus app for the exact slot and eligibility.",
                "device_limit": "Smart-meter intervals show whole-home demand. They cannot identify individual devices without separate plug-level or circuit-level monitoring.",
            },
        }
    finally:
        conn.close()


HTML = """<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Energy usage dashboard</title><style>
:root{--ink:#17233a;--muted:#64748b;--card:#fff;--line:#e2e8f0;--electric:#2563eb;--gas:#ea580c;--accent:#0f766e}*{box-sizing:border-box}body{margin:0;background:#f5f8fc;color:var(--ink);font:16px system-ui,-apple-system,sans-serif}main{max-width:1180px;margin:auto;padding:32px 20px 64px}h1{margin:0;font-size:clamp(2rem,5vw,3.4rem)}h2{margin:0 0 14px;font-size:1.15rem}.lede,.note{color:var(--muted);line-height:1.5}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px;margin:24px 0}.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;box-shadow:0 4px 16px #0f172a0a}.number{font-size:2rem;font-weight:750;margin:.35rem 0}.wide{grid-column:span 2}svg{width:100%;height:270px;display:block}.legend{display:flex;gap:18px;color:var(--muted);font-size:.9rem}.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}.electric{background:var(--electric)}.gas{background:var(--gas)}button{border:1px solid var(--line);background:white;padding:9px 12px;border-radius:8px;cursor:pointer}.selected{background:var(--ink);color:white}.actions li{margin:10px 0;line-height:1.45}.warning{border-left:4px solid #f59e0b;padding-left:12px}@media(max-width:720px){.wide{grid-column:span 1}main{padding:20px 14px}}
</style></head><body><main><header><p class=\"lede\">READ-ONLY OCTOPUS INTELLIGENCE</p><h1>Energy usage, made actionable.</h1><p class=\"lede\" id=\"updated\"></p></header>
<section class=\"grid\" id=\"summary\"></section>
<section class=\"grid\"><article class=\"card wide\"><h2>Daily consumption</h2><div class=\"legend\"><span><i class=\"dot electric\"></i>Electricity</span><span><i class=\"dot gas\"></i>Gas</span></div><svg id=\"daily\" viewBox=\"0 0 900 270\" role=\"img\" aria-label=\"Daily consumption chart\"></svg></article><article class=\"card\"><h2>When energy is used</h2><p class=\"lede\">Average kWh per hour. Choose a fuel.</p><p><button class=\"selected\" data-fuel=\"electricity\">Electricity</button> <button data-fuel=\"gas\">Gas</button></p><svg id=\"hourly\" viewBox=\"0 0 430 270\" role=\"img\" aria-label=\"Hourly usage chart\"></svg></article></section>
<section class=\"grid\"><article class=\"card\"><h2>Power Down playbook</h2><ul class=\"actions\"><li>Opt in through Octoplus/Saving Sessions; the dashboard cannot opt in for you.</li><li id=\"peak\"></li><li>Before an invited window, delay flexible loads. Keep medical, safety and essential devices running.</li></ul></article><article class=\"card\"><h2>Weekend Happy Hours / Power Up</h2><ul class=\"actions\"><li>Use the Octopus app to confirm the exact time, eligibility and any cap.</li><li>Move flexible loads into the confirmed slot: laundry, dishwasher, EV charging or pre-heating.</li><li>Schedule only appliances you can safely run unattended.</li></ul></article><article class=\"card\"><h2>Device insight</h2><p class=\"note warning\" id=\"device\"></p><p class=\"note\">Add smart plugs or circuit monitoring if appliance-level analysis is important.</p></article></section>
<section class=\"card\"><h2>How to use this board</h2><p class=\"note\">Use the daily chart to spot changes, then the hourly chart to identify your flexible-load window. Rates and events change: verify Saving Sessions and Happy Hours in Octopus before acting. This dashboard makes suggestions only; it never switches tariffs, controls appliances, or changes your account.</p></section>
</main><script>
const ns='http://www.w3.org/2000/svg';let D;const q=s=>document.querySelector(s);const money=x=>'£'+x.toFixed(2);function el(n,a={}){let x=document.createElementNS(ns,n);Object.entries(a).forEach(([k,v])=>x.setAttribute(k,v));return x}function path(points,max,w,h){return points.map((v,i)=>`${i?'L':'M'}${42+i*(w-60)/Math.max(points.length-1,1)} ${h-35-(v/max)*(h-60)}`).join(' ')}function line(svg,points,color,max,clear=true){let w=+svg.viewBox.baseVal.width,h=+svg.viewBox.baseVal.height;if(clear){svg.innerHTML='';svg.append(el('path',{d:'M42 15V'+(h-35)+'H'+(w-15),stroke:'#cbd5e1','stroke-width':1,fill:'none'}));svg.append(el('text',{x:2,y:20,fill:'#64748b'})).textContent=max.toFixed(1)+' kWh';svg.append(el('text',{x:42,y:h-8,fill:'#64748b'})).textContent='older';svg.append(el('text',{x:w-48,y:h-8,fill:'#64748b'})).textContent='latest'}svg.append(el('path',{d:path(points,max,w,h),stroke:color,'stroke-width':3,fill:'none'}))}function renderHourly(fuel){let a=D.hourly.filter(x=>x.fuel===fuel).sort((a,b)=>a.hour-b.hour),p=Array.from({length:24},(_,h)=>(a.find(x=>x.hour===h)||{average_kwh:0}).average_kwh),max=Math.max(...p,1);line(q('#hourly'),p,fuel==='electricity'?'#2563eb':'#ea580c',max);q('#peak').textContent=`Your highest average electricity hour is ${D.insights.electricity_peak_hour===null?'not yet available':String(D.insights.electricity_peak_hour).padStart(2,'0')+':00–'+String((D.insights.electricity_peak_hour+1)%24).padStart(2,'0')+':00'} (${D.insights.electricity_peak_kwh_per_hour} kWh/hour). Avoid adding flexible loads then unless an Octopus Power Up/Happy Hour specifically applies.`}function render(){q('#updated').textContent='Updated '+new Date(D.generated_at).toLocaleString('en-GB',{timeZone:D.timezone})+' · '+D.timezone;let latest={};D.daily.forEach(x=>latest[x.fuel]=x);q('#summary').innerHTML=['electricity','gas'].map(f=>{let x=latest[f];return `<article class=card><h2>${f[0].toUpperCase()+f.slice(1)} latest day</h2><div class=number>${x?x.consumption_kwh.toFixed(1):'–'} kWh</div><p class=lede>${x?money(x.total_cost_gbp)+' estimated cost':'No data yet'}</p></article>`}).join('')+`<article class=card><h2>Next action</h2><div class=number>Plan flexible loads</div><p class=lede>Use a confirmed Octopus event, not a guessed time.</p></article>`;let by={};D.daily.forEach(x=>{(by[x.date]??={})[x.fuel]=x.consumption_kwh});let days=Object.values(by).slice(-45),e=days.map(x=>x.electricity||0),g=days.map(x=>x.gas||0),m=Math.max(...e,...g,1);line(q('#daily'),e,'#2563eb',m);line(q('#daily'),g,'#ea580c',m,false);renderHourly('electricity');q('#device').textContent=D.insights.device_limit;document.querySelectorAll('[data-fuel]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-fuel]').forEach(x=>x.classList.remove('selected'));b.classList.add('selected');renderHourly(b.dataset.fuel)})}fetch('data.json').then(r=>r.json()).then(x=>{D=x;render()}).catch(()=>q('#updated').textContent='No dashboard data found. Run the energy sync first.');
</script></body></html>"""


def write_dashboard(database: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "data.json").write_text(json.dumps(dashboard_data(database), separators=(",", ":")), encoding="utf-8")
    index = output_dir / "index.html"
    index.write_text(HTML, encoding="utf-8")
    return index
