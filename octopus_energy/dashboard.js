'use strict';

const LONDON_TIMEZONE = 'Europe/London';
const DAY_MS = 24 * 60 * 60 * 1000;

function periodKey(date, period) {
  if (period === 'week') {
    const monday = new Date(date + 'T00:00:00Z');
    monday.setUTCDate(monday.getUTCDate() - (monday.getUTCDay() + 6) % 7);
    return monday.toISOString().slice(0, 10);
  }
  return date.slice(0, period === 'month' ? 7 : period === 'year' ? 4 : 10);
}

function groupRows(daily, fuel, period) {
  const grouped = new Map();
  for (const row of daily.filter(row => row.fuel === fuel)) {
    const key = periodKey(row.date, period);
    const group = grouped.get(key) || {key, kwh: 0, cost: 0, dates: new Set(), reasons: new Set()};
    group.kwh = group.kwh === null || row.kwh === null ? null : group.kwh + row.kwh;
    group.cost = group.cost === null || row.cost === null || row.cost_status === 'incomplete' ? null : group.cost + row.cost;
    if (row.cost_reason) {
      const reasons = Array.isArray(row.cost_reason) ? row.cost_reason : String(row.cost_reason).split(',');
      reasons.forEach(reason => group.reasons.add(reason.trim()));
    }
    group.dates.add(row.date);
    grouped.set(key, group);
  }
  return [...grouped.values()].sort((a, b) => a.key.localeCompare(b.key)).slice(-90);
}

function londonDate(now = new Date()) {
  return new Intl.DateTimeFormat('en-CA', {timeZone: LONDON_TIMEZONE, year: 'numeric', month: '2-digit', day: '2-digit'}).format(now);
}

function periodEnd(key, period) {
  const start = new Date(key + (period === 'month' ? '-01' : period === 'year' ? '-01-01' : '') + 'T00:00:00Z');
  const end = new Date(start);
  if (period === 'week') end.setUTCDate(end.getUTCDate() + 7);
  else if (period === 'month') end.setUTCMonth(end.getUTCMonth() + 1);
  else if (period === 'year') end.setUTCFullYear(end.getUTCFullYear() + 1);
  else end.setUTCDate(end.getUTCDate() + 1);
  return {start, end};
}

function trendChange(groups, period, today = londonDate()) {
  const last = groups.at(-1), prior = groups.at(-2);
  if (!last || !prior || last.kwh === null || prior.kwh === null || prior.kwh === 0) return null;
  for (const group of [prior, last]) {
    const {start, end} = periodEnd(group.key, period);
    if (end.toISOString().slice(0, 10) > today || group.dates.size !== Math.round((end - start) / DAY_MS)) return null;
  }
  if (periodEnd(prior.key, period).end.getTime() !== periodEnd(last.key, period).start.getTime()) return null;
  return (last.kwh - prior.kwh) / prior.kwh * 100;
}

function readingState(data, fuel, now = Date.now()) {
  const latest = data.latest_reading_at?.[fuel];
  if (!latest) return {state: 'warning', text: 'No meter-reading timestamp available for ' + fuel + '.'};
  const age = now - Date.parse(latest);
  if (!Number.isFinite(age) || age < -5 * 60 * 1000) return {state: 'warning', text: 'Meter-reading timestamp is invalid. Check the source sync.'};
  if (age > 48 * 60 * 60 * 1000) return {state: 'warning', text: 'Readings are older than 48 hours. Refresh the source sync before making decisions.'};
  if (now - Date.parse(data.generated_at) > DAY_MS) return {state: 'warning', text: 'Published snapshot is older than 24 hours. Refresh the source sync and publish again.'};
  return {state: 'ready', text: 'Recent recorded data, not a live meter feed. Costs are estimates.'};
}

function validDate(value) {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    Number.isFinite(Date.parse(value)) && new Date(value + 'T00:00:00Z').toISOString().slice(0, 10) === value;
}

function validateData(data) {
  if (!data || !Number.isFinite(Date.parse(data.generated_at)) || !Array.isArray(data.daily) || !Array.isArray(data.hourly)) throw new Error('Invalid dashboard data');
  const fuels = ['electricity', 'gas'];
  const seen = new Set();
  for (const row of data.daily) {
    const key = row?.fuel + ':' + row?.date;
    if (!row || !fuels.includes(row.fuel) || !validDate(row.date) || !(row.kwh === null || Number.isFinite(row.kwh)) ||
        !(row.cost === null || Number.isFinite(row.cost)) || seen.has(key)) throw new Error('Invalid daily data');
    if (row.cost_status !== undefined && !['complete', 'incomplete'].includes(row.cost_status)) throw new Error('Invalid cost status');
    if (row.cost_reason !== undefined && row.cost_reason !== null && typeof row.cost_reason !== 'string' &&
        !(Array.isArray(row.cost_reason) && row.cost_reason.every(reason => typeof reason === 'string'))) throw new Error('Invalid cost reason');
    seen.add(key);
  }
  for (const row of data.hourly) {
    if (!row || !fuels.includes(row.fuel) || !Number.isInteger(row.hour) || row.hour < 0 || row.hour > 23 || !Number.isFinite(row.kwh)) throw new Error('Invalid hourly data');
  }
  return data;
}

function initializeDashboard() {
  let data = null, fuel = 'electricity', period = 'day', busy = false, loadError = false;
  const $ = selector => document.querySelector(selector);
  const formatted = value => value ? new Date(value).toLocaleString('en-GB', {timeZone: LONDON_TIMEZONE}) : 'unavailable';
  const money = value => value === null ? 'Cost unavailable' : '£' + value.toFixed(2) + ' estimated cost';
  const setStatus = (state, message) => { $('#status').dataset.state = state; $('#status').textContent = message; };

  function draw(groups) {
    const chart = $('#chart');
    if (!groups.some(row => row.kwh !== null)) { chart.innerHTML = ''; return; }
    const max = Math.max(...groups.filter(row => row.kwh !== null).map(row => row.kwh), 1);
    let previousKnown = false;
    const path = groups.map((row, index) => {
      if (row.kwh === null) { previousKnown = false; return ''; }
      const point = `${previousKnown ? 'L' : 'M'}${45 + index * 830 / Math.max(groups.length - 1, 1)} ${250 - row.kwh / max * 210}`;
      previousKnown = true;
      return point;
    }).join(' ');
    chart.innerHTML = `<path d="M45 20V250H875" stroke="#41536c" fill="none"/><path d="${path}" stroke="${fuel === 'electricity' ? '#60a5fa' : '#fb923c'}" stroke-width="3" fill="none"/><text x="4" y="25" fill="#9aacc2">${max.toFixed(1)} kWh</text>`;
    chart.setAttribute('aria-label', `${fuel} ${period} consumption, ${groups[0].key} to ${groups.at(-1).key}`);
  }

  function render() {
    if (!data) return;
    const groups = groupRows(data.daily, fuel, period), last = groups.at(-1);
    const change = trendChange(groups, period);
    const hours = data.hourly.filter(row => row.fuel === fuel);
    const peak = hours.length ? hours.reduce((a, b) => a.kwh >= b.kwh ? a : b) : null;
    const peakLabel = peak ? String(peak.hour).padStart(2, '0') + ':00' : 'No profile';
    const showHourlyPeak = fuel === 'electricity' || peak !== null;
    const freshness = readingState(data, fuel);
    const reasons = {missing_unit_rate: 'missing unit rates', missing_standing_charge: 'missing standing charges', unknown_gas_units: 'unverified gas units'};
    const reason = last ? [...last.reasons].map(value => reasons[value] || 'incomplete source data').join(', ') : '';
    if (loadError) setStatus('error', 'Reload failed. Showing the last loaded snapshot; it may be out of date. Check access and retry.');
    else if (!last) setStatus('warning', 'No recorded ' + fuel + ' data. Run an authenticated source sync and publish the dashboard.');
    else if (last.kwh === null) setStatus('warning', 'Usage unavailable: source units are unverified. Do not treat raw gas volume as kWh.');
    else if (last.cost === null && freshness.state === 'ready') setStatus('warning', 'Usage is available, but costs are unavailable because source pricing is incomplete.');
    else setStatus(freshness.state, freshness.text);
    $('#updated').textContent = `Snapshot exported ${formatted(data.generated_at)} · Latest ${fuel} reading ${formatted(data.latest_reading_at?.[fuel])} · Last successful sync ${formatted(data.last_successful_sync_at)} · Europe/London`;
    $('#notice').textContent = data.notice || 'Whole-home meter data cannot identify individual appliances.';
    $('#cards').innerHTML = `<article class="card"><h2>${fuel} ${period}${period === 'week' ? ' (Monday start)' : ''}</h2><div class="number">${last ? last.kwh === null ? 'Usage unavailable' : last.kwh.toFixed(1) + ' kWh' : 'No data'}</div><p class="muted">${last ? money(last.cost) : 'Cost unavailable'}${reason ? ' (' + reason + ')' : ''}${last ? ' · ' + last.key : ''}</p></article><article class="card"><h2>Recorded-period trend</h2><div class="number">${change === null ? 'Not comparable' : (change >= 0 ? '+' : '') + change.toFixed(1) + '%'}</div><p class="muted">Requires adjacent past periods with all dates present. Missing intervals are not adjusted.</p></article><article class="card"><h2>Historical peak hour</h2><div class="number">${showHourlyPeak ? peakLabel : 'Review heating'}</div><p class="muted">${showHourlyPeak ? 'Europe/London, averaged across available readings. Not a cheapest-rate prediction.' : 'Compare cold-day demand and heating schedules.'}</p></article>`;
    $('#title').textContent = fuel[0].toUpperCase() + fuel.slice(1) + ' ' + period + ' recorded consumption';
    $('#range').textContent = last ? `${groups[0].key} to ${last.key} · Up to 90 recorded periods. Latest period may be partial.` : 'No data for this fuel.';
    $('#action').textContent = loadError || !last || last.kwh === null || last.cost === null || freshness.state !== 'ready' ? 'Refresh and check meter data and tariff rates before making decisions. Confirm any event invitation in your Octopus account.' :
      fuel === 'electricity' ? 'For reward events, follow only your Octopus-confirmed invitation. Outside events, check your tariff before moving flexible loads; your peak-demand hour is not necessarily your highest-cost hour.' :
      'If gas demand rises, check heating times, thermostat settings, hot-water demand and draughts. Keep essential heating and frost protection on.';
    draw(groups);
  }

  async function load() {
    if (busy) return;
    busy = true;
    $('#refresh').disabled = true;
    if (!data) setStatus('loading', 'Loading dashboard data…');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(new URL('data.json', document.baseURI), {cache: 'no-store', credentials: 'same-origin', signal: controller.signal});
      if (!response.ok) throw new Error('Dashboard unavailable');
      data = validateData(await response.json());
      loadError = false;
      render();
    } catch (_) {
      loadError = true;
      if (data) render();
      else setStatus('error', location.protocol === 'file:' ? 'Serve this directory over loopback HTTP to load data.json. See the dashboard operations guide.' : 'Cannot load dashboard data. Check authentication, network access and published files, then retry.');
    } finally {
      clearTimeout(timeout);
      busy = false;
      $('#refresh').disabled = false;
    }
  }

  for (const kind of ['fuel', 'period']) {
    document.querySelectorAll(`[data-${kind}]`).forEach(button => button.onclick = () => {
      if (kind === 'fuel') fuel = button.dataset.fuel;
      else period = button.dataset.period;
      document.querySelectorAll(`[data-${kind}]`).forEach(other => {
        other.classList.toggle('active', other === button);
        other.setAttribute('aria-pressed', String(other === button));
      });
      render();
    });
  }
  $('#refresh').onclick = load;
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') load(); });
  setInterval(() => { if (document.visibilityState === 'visible') load(); }, 5 * 60 * 1000);
  load();
}

if (typeof document !== 'undefined') initializeDashboard();
