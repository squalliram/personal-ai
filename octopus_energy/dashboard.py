"""Privacy-preserving interactive dashboard export."""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo
from .storage import connect

LONDON = ZoneInfo("Europe/London")

def dashboard_data(database: Path) -> dict:
    conn = connect(database)
    try:
        daily = [dict(r) for r in conn.execute("SELECT date,fuel,ROUND(consumption_kwh,3) kwh,ROUND(total_cost_gbp,2) cost FROM daily_costs ORDER BY date")]
        raw = conn.execute("SELECT m.fuel,c.interval_start,c.consumption_kwh FROM consumption c JOIN meters m ON m.id=c.meter_id ORDER BY c.interval_start").fetchall()
        hours = defaultdict(lambda: defaultdict(list))
        for r in raw:
            d=datetime.fromisoformat(r['interval_start'].replace('Z','+00:00')).astimezone(LONDON)
            hours[r['fuel']][d.hour].append(float(r['consumption_kwh'])*2)
        hourly=[{'fuel':f,'hour':h,'kwh':round(mean(v),3)} for f,hs in hours.items() for h,v in hs.items()]
        return {'generated_at':datetime.now(LONDON).isoformat(),'timezone':'Europe/London','daily':daily,'hourly':hourly,
                'notice':'Whole-home meter data cannot identify individual appliances. Add smart-plug data for device attribution.'}
    finally: conn.close()

HTML='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Energy intelligence</title><style>
:root{--bg:#07111f;--card:#111c2e;--ink:#e8eef8;--muted:#9aacc2;--line:#273a55;--e:#60a5fa;--g:#fb923c;--accent:#2dd4bf}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px system-ui}main{max-width:1200px;margin:auto;padding:32px 18px}h1{margin:0;font-size:clamp(2rem,5vw,3.5rem)}h2{font-size:1.1rem}.muted{color:var(--muted);line-height:1.5}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin:20px 0}.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px}.wide{grid-column:span 2}.number{font-size:2rem;font-weight:750}button{color:var(--ink);background:#172640;border:1px solid var(--line);border-radius:8px;padding:9px 12px;margin:2px;cursor:pointer}.active{background:var(--accent);color:#06201d;border-color:var(--accent)}svg{width:100%;height:290px;display:block}.legend{color:var(--muted);font-size:.9rem}.tip{border-left:4px solid var(--accent);padding-left:12px}@media(max-width:700px){.wide{grid-column:span 1}}
</style><main><p class="muted">READ-ONLY OCTOPUS INTELLIGENCE</p><h1>Energy decisions, not just readings.</h1><p id="updated" class="muted"></p><section class="card"><b>Fuel</b><p><button data-fuel="electricity" class="active">Electricity</button><button data-fuel="gas">Gas</button></p><b>Period</b><p><button data-period="day" class="active">Daily</button><button data-period="week">Weekly</button><button data-period="month">Monthly</button><button data-period="year">Yearly</button></p></section><section class="grid" id="cards"></section><section class="card wide"><h2 id="title">Consumption</h2><svg id="chart" viewBox="0 0 900 290"></svg></section><section class="grid"><article class="card"><h2>Best next action</h2><p id="action" class="tip"></p></article><article class="card"><h2>Power Down</h2><p class="muted">During a confirmed event, delay flexible loads such as laundry, dishwasher, tumble dryer, cooking, EV charging and immersion heating. Do not turn off safety, medical, heating-frost-protection or essential devices.</p></article><article class="card"><h2>Device insight</h2><p id="notice" class="muted"></p></article></section></main><script>
let D,f='electricity',p='day';const $=s=>document.querySelector(s),money=n=>'£'+n.toFixed(2);function group(){let a=D.daily.filter(x=>x.fuel===f),o={};for(let x of a){let k=p==='day'?x.date:p==='week'?x.date.slice(0,8)+String(Math.ceil(+x.date.slice(8,10)/7)):p==='month'?x.date.slice(0,7):x.date.slice(0,4);let z=o[k]||={kwh:0,cost:0};z.kwh+=x.kwh;z.cost+=x.cost;o[k]=z}return Object.entries(o).map(([key,v])=>({key,...v})).slice(-90)}function draw(a){let s=$('#chart'),w=900,h=290,max=Math.max(...a.map(x=>x.kwh),1),d=a.map((x,i)=>`${i?'L':'M'}${45+i*830/Math.max(a.length-1,1)} ${250-x.kwh/max*210}`).join(' ');s.innerHTML=`<path d="M45 20V250H875" stroke="#41536c" fill="none"/><path d="${d}" stroke="${f==='electricity'?'#60a5fa':'#fb923c'}" stroke-width="3" fill="none"/><text x="4" y="25" fill="#9aacc2">${max.toFixed(1)} kWh</text><text x="45" y="280" fill="#9aacc2">older</text><text x="830" y="280" fill="#9aacc2">latest</text>`}function render(){let a=group(),last=a.at(-1)||{kwh:0,cost:0},prior=a.at(-2),change=prior?((last.kwh-prior.kwh)/prior.kwh*100):null;$('#updated').textContent='Updated '+new Date(D.generated_at).toLocaleString('en-GB',{timeZone:D.timezone});$('#cards').innerHTML=`<article class=card><h2>${f} ${p}</h2><div class=number>${last.kwh.toFixed(1)} kWh</div><p class=muted>${money(last.cost)} estimated cost</p></article><article class=card><h2>Trend</h2><div class=number>${change===null?'–':(change>=0?'+':'')+change.toFixed(1)+'%'}</div><p class=muted>vs preceding ${p}</p></article><article class=card><h2>Action window</h2><div class=number>${f==='electricity'?peak():'Review heating'}</div><p class=muted>${f==='electricity'?'Highest average hour; move flexible load away unless Octopus confirms Power Up.':'Compare cold-day demand and heating schedules.'}</p></article>`;$('#title').textContent=f[0].toUpperCase()+f.slice(1)+' '+p+' consumption';$('#action').textContent=f==='electricity'?'Move flexible loads out of your peak hour, then use confirmed Octopus Power Up/Happy Hour slots for them.':'If gas rises versus the prior period, first check heating times, thermostat settings, hot-water demand and draughts.';draw(a)}function peak(){let a=D.hourly.filter(x=>x.fuel==='electricity');if(!a.length)return'No data';let x=a.reduce((a,b)=>a.kwh>b.kwh?a:b);return String(x.hour).padStart(2,'0')+':00'}fetch('data.json').then(r=>r.json()).then(x=>{D=x;$('#notice').textContent=D.notice;document.querySelectorAll('[data-fuel]').forEach(b=>b.onclick=()=>{f=b.dataset.fuel;document.querySelectorAll('[data-fuel]').forEach(x=>x.classList.toggle('active',x===b));render()});document.querySelectorAll('[data-period]').forEach(b=>b.onclick=()=>{p=b.dataset.period;document.querySelectorAll('[data-period]').forEach(x=>x.classList.toggle('active',x===b));render()});render()});
</script>'''

def write_dashboard(database:Path, output_dir:Path)->Path:
    output_dir.mkdir(parents=True,exist_ok=True)
    (output_dir/'data.json').write_text(json.dumps(dashboard_data(database)),encoding='utf-8')
    out=output_dir/'index.html';out.write_text(HTML,encoding='utf-8');return out
