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
  if (period === 'latest-day') {
    const start = new Date(key);
    return {start, end: new Date(start.getTime() + 3600000)};
  }
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
  if (period === 'latest-day') {
    return completePeriod(last, period, today) && completePeriod(prior, period, today) && prior.end === last.start ?
      (last.kwh - prior.kwh) / prior.kwh * 100 : null;
  }
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

function validInstant(value) {
  return typeof value === 'string' &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) &&
    validDate(value.slice(0, 10)) && Number(value.slice(11, 13)) < 24 && Number.isFinite(Date.parse(value));
}

function validRequestId(value) {
  return typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);
}

// Inputs are validated UTCZ timestamps. Date.parse loses the runner's microseconds.
function compareUtcTimestamps(left, right) {
  const leftSeconds = left.slice(0, 19), rightSeconds = right.slice(0, 19);
  if (leftSeconds !== rightSeconds) return leftSeconds < rightSeconds ? -1 : 1;
  const fraction = value => value[19] === '.' ? value.slice(20, -1) : '';
  const leftFraction = fraction(left), rightFraction = fraction(right);
  const width = Math.max(leftFraction.length, rightFraction.length);
  const a = leftFraction.padEnd(width, '0'), b = rightFraction.padEnd(width, '0');
  return a < b ? -1 : a > b ? 1 : 0;
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
  if (data.latest_day !== undefined) {
    if (!data.latest_day || typeof data.latest_day !== 'object' || Array.isArray(data.latest_day)) throw new Error('Invalid latest day');
    for (const fuel of fuels) {
      const day = data.latest_day[fuel];
      if (day === null || day === undefined) continue;
      if (!validDate(day.date) || day.complete !== true || day.cost_basis !== 'usage_excludes_standing_charge' ||
          !Array.isArray(day.hours) || ![23, 24, 25].includes(day.hours.length) || day.date >= londonDate(new Date(data.generated_at))) throw new Error('Invalid latest day');
      let previous = null;
      for (const row of day.hours) {
        if (!row || !validInstant(row.start) || !validInstant(row.end) ||
            Date.parse(row.end) - Date.parse(row.start) !== 3600000 || (previous && Date.parse(row.start) !== previous) ||
            londonDate(new Date(row.start)) !== day.date || !Number.isFinite(row.kwh) ||
            !(row.cost === null || Number.isFinite(row.cost))) throw new Error('Invalid latest hour');
        previous = Date.parse(row.end);
      }
      const clock = value => new Intl.DateTimeFormat('en-GB', {timeZone: LONDON_TIMEZONE, hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}).format(new Date(value));
      const next = new Date(day.date + 'T00:00:00Z'); next.setUTCDate(next.getUTCDate() + 1);
      if (clock(day.hours[0].start) !== '00:00' || clock(day.hours.at(-1).end) !== '00:00' ||
          londonDate(new Date(day.hours.at(-1).end)) !== next.toISOString().slice(0, 10)) throw new Error('Incomplete latest day');
    }
  }
  return data;
}

function viewGroups(data, fuel, period) {
  if (period !== 'latest-day') return groupRows(data.daily, fuel, period);
  const day = data.latest_day?.[fuel];
  return day ? day.hours.map(row => ({...row, key: row.start, dates: new Set([day.date]), reasons: new Set()})) : [];
}

function validateRefreshStatus(status) {
  const results = [null, 'running', 'success', 'no_data', 'partial', 'failed'];
  const categories = [null, 'invalid_configuration', 'lock_unavailable', 'inherited_lock_invalid', 'request_claim_failed',
    'sync_failed', 'sync_timeout', 'provider_error', 'database_error', 'io_error', 'validation_error', 'publish_failed',
    'publish_timeout', 'status_publish_failed', 'status_publish_timeout', 'interrupted', 'release_check_failed',
    'superseded_release', 'release_head_mismatch'];
  const timestamp = value => value === null || (validInstant(value) && value.endsWith('Z'));
  if (!status || status.schema_version !== 1 || !results.includes(status.result) ||
      ![null, 'pending', 'running'].includes(status.request_state) || !categories.includes(status.error_category) ||
      !timestamp(status.last_attempt_at) || !timestamp(status.last_success_at) || !status.latest_reading_at ||
      Array.isArray(status.latest_reading_at) ||
      (status.request_id !== undefined && status.request_id !== null && !validRequestId(status.request_id)) ||
      !['electricity', 'gas'].every(fuel => timestamp(status.latest_reading_at[fuel]))) throw new Error('Invalid refresh status');
  return {...status, request_id: status.request_id ?? null};
}

function refreshFailureMessage(category) {
  if (category === 'superseded_release') return 'A newer update is publishing. The current snapshot is kept.';
  if (['sync_failed', 'sync_timeout', 'provider_error'].includes(category)) return 'Octopus could not be fetched. Try again later.';
  if (['publish_failed', 'publish_timeout', 'status_publish_failed', 'status_publish_timeout'].includes(category)) return 'The update could not be published.';
  if (['lock_unavailable', 'inherited_lock_invalid'].includes(category)) return 'The server is busy with another update.';
  if (category === 'validation_error') return 'New readings could not be validated.';
  if (category === 'interrupted') return 'The update was interrupted.';
  return 'The server could not complete this update. Please try again later.';
}

function completePeriod(group, period, today = londonDate()) {
  if (period === 'latest-day') return group.dates.size === 1 && [...group.dates][0] < today;
  const {start, end} = periodEnd(group.key, period);
  return end.toISOString().slice(0, 10) <= today && group.dates.size === Math.round((end - start) / DAY_MS);
}

function anomalyModel(groups, period, today = londonDate()) {
  // Same mean + 2 population-SD rule as analytics.anomalies, in chart-period kWh.
  const eligible = groups.slice(-90).filter(group => group.kwh !== null && completePeriod(group, period, today));
  const high = new Set();
  if (eligible.length < 7) return {high, samples: eligible.length, baseline: null, threshold: null};
  const baseline = eligible.reduce((sum, group) => sum + group.kwh, 0) / eligible.length;
  const deviation = Math.sqrt(eligible.reduce((sum, group) => sum + (group.kwh - baseline) ** 2, 0) / eligible.length);
  const threshold = baseline + 2 * deviation;
  if (deviation > 0) eligible.filter(group => group.kwh > threshold).forEach(group => high.add(group.key));
  return {high, samples: eligible.length, baseline, threshold};
}

function periodLabel(key, period, compact = false) {
  if (period === 'latest-day') return new Intl.DateTimeFormat('en-GB', {
    timeZone: LONDON_TIMEZONE, ...(compact ? {} : {day: 'numeric', month: 'short', year: 'numeric'}),
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZoneName: 'short',
  }).format(new Date(key));
  const options = period === 'year' ? {year: 'numeric'} :
    period === 'month' ? {month: compact ? 'short' : 'long', year: 'numeric'} :
      {day: 'numeric', month: compact ? 'short' : 'long', year: 'numeric'};
  const label = new Intl.DateTimeFormat('en-GB', {timeZone: LONDON_TIMEZONE, ...options}).format(periodEnd(key, period).start);
  return period === 'week' && !compact ? 'Week commencing ' + label : label;
}

function periodRangeLabel(key, period) {
  const {start, end} = periodEnd(key, period);
  if (period === 'latest-day') return `${periodLabel(start.toISOString(), period)} → ${periodLabel(end.toISOString(), period)} Europe/London (end exclusive)`;
  // Keys are London calendar dates, not UTC instants. Midnight labels also hold on 23/25-hour DST days.
  const label = date => periodLabel(date.toISOString().slice(0, 10), 'day');
  return `${label(start)} 00:00 → ${label(end)} 00:00 Europe/London (end exclusive)`;
}

function numberLabel(value) {
  return value.toLocaleString('en-GB', {maximumFractionDigits: 3});
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[character]);
}

function contributorEstimate(group, period, fuel, hourly, baseline) {
  const {start, end} = periodEnd(group.key, period);
  const month = new Date((start.getTime() + end.getTime()) / 2).getUTCMonth() + 1;
  const season = period === 'year' ? 'mixed-season' : month <= 3 || month >= 10 ? 'cool-season' :
    month >= 6 && month <= 8 ? 'warm-season' : 'mild-season';
  const hours = hourly.filter(row => row.fuel === fuel && row.kwh > 0);
  const peak = hours.length ? hours.reduce((a, b) => a.kwh >= b.kwh ? a : b) : null;
  const overnight = peak && (peak.hour < 6 || peak.hour === 23);
  const largeUplift = baseline <= 0 || (group.kwh - baseline) / baseline >= 0.5;
  const contributors = [];
  if (fuel === 'gas') {
    contributors.push(season === 'cool-season' ? 'Space heating and hot-water demand are possibilities in the cooler calendar season.' :
      'Hot-water demand and space heating are possibilities; no weather or thermostat data is available.');
    if (peak) contributors.push(overnight ? 'Scheduled heating or hot-water cycles could fit the overnight historical profile.' :
      'Heating or hot-water schedules could fit the daytime/evening historical profile.');
  } else {
    contributors.push(peak ? overnight ? 'Continuous or scheduled overnight loads, such as standby, refrigeration or charging if present.' :
      'Cooking, hot-water heating or other scheduled household loads, if present.' :
      'Whole-home electrical demand; without an hourly profile, individual load types cannot be narrowed down.');
    if (season === 'cool-season') contributors.push('Electric space or water heating, if used, is a cool-season possibility.');
    if (season === 'warm-season') contributors.push('Cooling or extra refrigeration demand, if present, is a warm-season possibility.');
  }
  contributors.push(largeUplift ? 'The large uplift could fit overlapping loads or sustained demand, not a proven device cause.' :
    'The smaller uplift could fit longer run times or regular loads overlapping, not a proven device cause.');
  return {season, peakHour: peak?.hour ?? null, largeUplift, contributors};
}

function tooltipContent(group, period, fuel, hourly, model) {
  const usage = group.kwh === null ? 'Usage unavailable' : numberLabel(group.kwh) + ' kWh';
  const cost = group.cost === null ? 'Cost unavailable' : group.cost > 0 && group.cost < 0.005 ? 'Under £0.01 estimated cost' : '£' + group.cost.toFixed(2) + ' estimated cost';
  let detail = '';
  if (model.high.has(group.key)) {
    const estimate = contributorEstimate(group, period, fuel, hourly, model.baseline);
    const excess = group.kwh - model.baseline;
    const percent = model.baseline > 0 ? ` (${numberLabel(excess / model.baseline * 100)}%)` : '';
    const timing = estimate.peakHour === null ? 'No positive hourly profile is available.' :
      `Historical profile peak ${String(estimate.peakHour).padStart(2, '0')}:00 Europe/London; not this period's measured timing.`;
    detail = `<section class="high-point"><h4>Unusually high point</h4><p>${numberLabel(excess)} kWh${percent} above the mean.
      Cutoff ${numberLabel(model.threshold)} kWh · ${model.samples} complete periods.</p>
      <h4>Likely contributors · estimates, not measured</h4><ul>${estimate.contributors.map(text => `<li>${escapeHtml(text)}</li>`).join('')}</ul>
      <p class="muted">Heuristic: ${fuel}, ${estimate.season}, ${estimate.largeUplift ? 'at least 50% uplift or non-positive baseline' : 'under 50% uplift'}.
      ${escapeHtml(timing)} No device-level data: these are possibilities to investigate, not appliance attribution.</p></section>`;
  }
  const coverage = completePeriod(group, period) ? '' : '<p class="muted">Partial period or missing dates; no high-point assessment.</p>';
  return `<h3>${escapeHtml(periodLabel(group.key, period))}</h3><p class="tooltip-range">${escapeHtml(periodRangeLabel(group.key, period))}</p>
    <dl><div><dt>Energy used</dt><dd>${usage}</dd></div><div><dt>${period === 'latest-day' ? 'Usage cost (standing charge excluded)' : 'Cost'}</dt><dd>${cost}</dd></div></dl>${coverage}${detail}`;
}

function chartLayout(groups) {
  const left = 90, right = 928, top = 28, bottom = 312;
  const values = groups.filter(group => group.kwh !== null).map(group => group.kwh);
  const low = Math.min(0, ...values), high = Math.max(0, ...values);
  const roughStep = (high - low || 1) / 4;
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const step = [1, 2, 2.5, 5, 10].find(factor => factor * magnitude >= roughStep) * magnitude;
  const min = Math.floor(low / step) * step, max = Math.ceil(high / step) * step || step;
  const y = value => bottom - (value - min) / (max - min) * (bottom - top);
  const ticks = Array.from({length: Math.round((max - min) / step) + 1}, (_, index) => {
    const value = Number((min + index * step).toPrecision(12));
    return {value, y: y(value)};
  });
  const points = groups.map((group, index) => ({group, index,
    x: groups.length === 1 ? (left + right) / 2 : left + index * (right - left) / (groups.length - 1),
    y: group.kwh === null ? null : y(group.kwh)}));
  return {left, right, top, bottom, ticks, points};
}

function initializeDashboard() {
  let data = null, fuel = 'electricity', period = 'day', busy = false, loadError = false;
  let refreshBusy = false, lastRefreshStatus = null;
  const $ = selector => document.querySelector(selector);
  const formatted = value => value ? new Date(value).toLocaleString('en-GB', {timeZone: LONDON_TIMEZONE}) : 'unavailable';
  const money = value => value === null ? 'Cost unavailable' : value > 0 && value < 0.005 ? 'Under £0.01 estimated cost' : '£' + value.toFixed(2) + ' estimated cost';
  const setStatus = (state, message) => { $('#status').dataset.state = state; $('#status').textContent = message; };
  let repositionTooltip = () => {};

  function draw(groups) {
    const chart = $('#chart'), tooltip = $('#chart-tooltip'), stage = $('#chart-stage');
    const focusedKey = chart.contains(document.activeElement) ? document.activeElement.dataset.periodKey : null;
    tooltip.hidden = true;
    repositionTooltip = () => {};
    chart.onpointermove = chart.onclick = chart.onkeydown = null;
    stage.onpointerleave = null;
    $('#chart-scroll').onscroll = null;
    if (!groups.some(row => row.kwh !== null)) {
      chart.innerHTML = '';
      chart.setAttribute('aria-label', 'No numeric ' + fuel + ' usage available to plot.');
      return;
    }
    const layout = chartLayout(groups), model = anomalyModel(groups, period);
    const {left, right, top, bottom, points, ticks} = layout;
    const xTitle = {day: 'Date', week: 'Week commencing', month: 'Month', year: 'Year', 'latest-day': 'Time'}[period] + ' (Europe/London)';
    let previousKnown = false;
    const path = points.map(point => {
      if (point.y === null) { previousKnown = false; return ''; }
      const segment = `${previousKnown ? 'L' : 'M'}${point.x} ${point.y}`;
      previousKnown = true;
      return segment;
    }).join(' ');
    const tickIndices = [...new Set(Array.from({length: Math.min(6, groups.length)}, (_, index) =>
      Math.round(index * (groups.length - 1) / Math.max(Math.min(6, groups.length) - 1, 1))))];
    const pointMarkup = points.filter(point => point.y !== null).map((point, index) => {
      const group = point.group, high = model.high.has(group.key);
      const label = `${periodLabel(group.key, period)}, ${numberLabel(group.kwh)} kWh, ${money(group.cost)}${high ? ', unusually high' : ''}`;
      return `<g class="chart-point" data-point-index="${point.index}" data-period-key="${group.key}" role="button" tabindex="${index === 0 ? 0 : -1}"
        aria-label="${escapeHtml(label)}" aria-describedby="chart-tooltip">
        <circle class="point-hit" cx="${point.x}" cy="${point.y}" r="16"/>
        ${high ? `<circle class="point-high" cx="${point.x}" cy="${point.y}" r="8"/>` : ''}
        <circle class="point-dot" cx="${point.x}" cy="${point.y}" r="3.5"/></g>`;
    }).join('');
    chart.dataset.fuel = fuel;
    chart.innerHTML = `<g aria-hidden="true">${ticks.map(tick => `<line class="chart-grid" x1="${left}" x2="${right}" y1="${tick.y}" y2="${tick.y}"/>
      <text class="chart-tick" x="${left - 14}" y="${tick.y + 5}" text-anchor="end">${numberLabel(tick.value)}</text>`).join('')}
      <path class="chart-axis" d="M${left} ${top}V${bottom}H${right}"/>
      ${tickIndices.map(index => `<line class="chart-axis" x1="${points[index].x}" x2="${points[index].x}" y1="${bottom}" y2="${bottom + 6}"/>
        <text class="chart-tick" x="${points[index].x}" y="${bottom + 26}" text-anchor="${groups.length > 1 && index === groups.length - 1 ? 'end' : 'middle'}">${escapeHtml(periodLabel(groups[index].key, period, true))}</text>`).join('')}
      <text class="chart-axis-title" x="${(left + right) / 2}" y="382" text-anchor="middle">${xTitle}</text>
      <text class="chart-axis-title" transform="translate(22 170) rotate(-90)" text-anchor="middle">Energy used (kWh)</text>
      <path class="chart-series" d="${path}"/></g>${pointMarkup}
      <g id="chart-crosshair" visibility="hidden" aria-hidden="true">
      <line id="chart-crosshair-x" class="chart-crosshair" y1="${top}" y2="${bottom}"/>
      <line id="chart-crosshair-y" class="chart-crosshair" x1="${left}" x2="${right}"/>
      <circle id="chart-active-point" class="chart-marker" r="7"/></g>`;
    chart.setAttribute('aria-label', `${fuel} ${period} consumption, ${groups[0].key} to ${groups.at(-1).key}. ${xTitle}; Energy used (kWh).`);
    const elements = [...chart.querySelectorAll('[data-point-index]')];
    const crosshair = chart.querySelector('#chart-crosshair');
    let selected = null;
    function positionTooltip(point) {
      const bounds = chart.getBoundingClientRect(), area = stage.getBoundingClientRect();
      const viewport = window.visualViewport;
      const viewportTop = viewport?.offsetTop ?? 0, viewportLeft = viewport?.offsetLeft ?? 0;
      const viewportHeight = viewport?.height ?? window.innerHeight;
      const viewportWidth = viewport?.width ?? window.innerWidth;
      const leftEdge = Math.max(area.left, viewportLeft) + 8;
      const rightEdge = Math.min(area.left + area.width, viewportLeft + viewportWidth) - 8;
      tooltip.style.maxWidth = Math.max(0, rightEdge - leftEdge) + 'px';
      tooltip.style.maxHeight = Math.max(0, Math.min(440, viewportHeight * 0.7, viewportHeight - 16)) + 'px';
      const box = tooltip.getBoundingClientRect();
      const x = bounds.left + point.x / 960 * bounds.width;
      const y = bounds.top + point.y / 400 * bounds.height;
      const above = y - box.height - 16;
      const preferredTop = above >= viewportTop + 8 ? above : y + 16;
      const clientTop = Math.max(viewportTop + 8, Math.min(preferredTop, viewportTop + viewportHeight - box.height - 8));
      // Rects are viewport-relative already; subtract the stage rect, not document scroll.
      tooltip.style.left = (Math.max(leftEdge, Math.min(x - box.width / 2, rightEdge - box.width)) - area.left) + 'px';
      tooltip.style.top = (clientTop - area.top) + 'px';
    }
    function hide() {
      tooltip.hidden = true;
      crosshair.setAttribute('visibility', 'hidden');
    }
    function show(index) {
      const point = points[index];
      if (!point || point.y === null) { hide(); return; }
      if (selected === index && !tooltip.hidden) { positionTooltip(point); return; }
      selected = index;
      elements.forEach(element => element.setAttribute('tabindex', Number(element.dataset.pointIndex) === index ? '0' : '-1'));
      const vertical = chart.querySelector('#chart-crosshair-x'), horizontal = chart.querySelector('#chart-crosshair-y');
      vertical.setAttribute('x1', point.x); vertical.setAttribute('x2', point.x);
      horizontal.setAttribute('y1', point.y); horizontal.setAttribute('y2', point.y);
      const marker = chart.querySelector('#chart-active-point');
      marker.setAttribute('cx', point.x); marker.setAttribute('cy', point.y);
      crosshair.setAttribute('visibility', 'visible');
      tooltip.innerHTML = tooltipContent(point.group, period, fuel, data.hourly, model);
      tooltip.hidden = false;
      positionTooltip(point);
    }
    function focusPoint(position) {
      const element = elements[Math.max(0, Math.min(position, elements.length - 1))];
      element.focus();
      show(Number(element.dataset.pointIndex));
    }
    elements.forEach(element => {
      element.onfocus = () => show(Number(element.dataset.pointIndex));
      element.onblur = hide;
    });
    function pointerIndex(event) {
      const bounds = chart.getBoundingClientRect();
      const x = (event.clientX - bounds.left) / bounds.width * 960;
      const edgeTolerance = 8; // SVG units: covers the outer marker/ring, not the chart gutters.
      if (x < left - edgeTolerance || x > right + edgeTolerance) return null;
      const clampedX = Math.max(left, Math.min(right, x));
      return groups.length === 1 ? 0 : Math.round((clampedX - left) / (right - left) * (groups.length - 1));
    }
    chart.onpointermove = event => {
      if (event.pointerType === 'touch') return;
      const index = pointerIndex(event);
      if (index === null) hide(); else show(index);
    };
    chart.onclick = event => {
      const index = pointerIndex(event);
      const position = elements.findIndex(element => Number(element.dataset.pointIndex) === index);
      if (position >= 0) focusPoint(position); else hide();
    };
    chart.onkeydown = event => {
      const position = Math.max(0, elements.indexOf(document.activeElement));
      const moves = {ArrowRight: position + 1, ArrowDown: position + 1, ArrowLeft: position - 1, ArrowUp: position - 1, Home: 0, End: elements.length - 1};
      if (Object.hasOwn(moves, event.key)) { event.preventDefault(); focusPoint(moves[event.key]); }
      else if (event.key === 'Escape') { event.preventDefault(); hide(); }
      else if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); focusPoint(position); }
    };
    stage.onpointerleave = () => { if (!chart.contains(document.activeElement)) hide(); };
    repositionTooltip = () => { if (selected !== null && !tooltip.hidden) positionTooltip(points[selected]); };
    $('#chart-scroll').onscroll = repositionTooltip;
    if (focusedKey) {
      const position = elements.findIndex(element => element.dataset.periodKey === focusedKey);
      focusPoint(position < 0 ? 0 : position);
    }
  }

  function render() {
    if (!data) return;
    const groups = viewGroups(data, fuel, period);
    const latestDay = period === 'latest-day' ? data.latest_day?.[fuel] : null;
    const last = latestDay ? {key: latestDay.date, reasons: new Set(),
      kwh: groups.reduce((sum, row) => sum + row.kwh, 0),
      cost: groups.some(row => row.cost === null) ? null : groups.reduce((sum, row) => sum + row.cost, 0)} : groups.at(-1);
    const change = trendChange(groups, period);
    const hours = data.hourly.filter(row => row.fuel === fuel);
    const peak = hours.length ? hours.reduce((a, b) => a.kwh >= b.kwh ? a : b) : null;
    const peakLabel = peak ? String(peak.hour).padStart(2, '0') + ':00' : 'No profile';
    const showHourlyPeak = fuel === 'electricity' || peak !== null;
    const freshness = readingState(data, fuel);
    const reasons = {missing_unit_rate: 'missing unit rates', missing_standing_charge: 'missing standing charges', unknown_gas_units: 'unverified gas units'};
    const reason = last ? [...last.reasons].map(value => reasons[value] || 'incomplete source data').join(', ') : '';
    if (loadError) setStatus('error', 'Reload failed. Showing the last loaded snapshot; it may be out of date. Check access and retry.');
    else if (period === 'latest-day' && !last) setStatus('warning', 'No complete London day is available for ' + fuel + '. Use the daily view; source coverage or confirmed gas units may be unavailable.');
    else if (!last) setStatus('warning', 'No recorded ' + fuel + ' data. Run an authenticated source sync and publish the dashboard.');
    else if (last.kwh === null) setStatus('warning', 'Usage unavailable: source units are unverified. Do not treat raw gas volume as kWh.');
    else if (last.cost === null && freshness.state === 'ready') setStatus('warning', 'Usage is available, but costs are unavailable because source pricing is incomplete.');
    else setStatus(freshness.state, freshness.text);
    $('#updated').textContent = `Data as of · Latest electricity reading ${formatted(data.latest_reading_at?.electricity)} · Latest gas reading ${formatted(data.latest_reading_at?.gas)} · Last successful sync ${formatted(lastRefreshStatus?.last_success_at || data.last_successful_sync_at)} · Snapshot exported ${formatted(data.generated_at)} · Europe/London`;
    $('#notice').textContent = data.notice || 'Whole-home meter data cannot identify individual appliances.';
    $('#cards').innerHTML = `<article class="card"><h2>${fuel} ${period === 'latest-day' ? 'latest available day' : period}${period === 'week' ? ' (Monday start)' : ''}</h2><div class="number">${last ? last.kwh === null ? 'Usage unavailable' : last.kwh.toFixed(1) + ' kWh' : 'No data'}</div><p class="muted">${last ? money(last.cost) : 'Cost unavailable'}${reason ? ' (' + reason + ')' : ''}${last ? ' · ' + last.key : ''}${period === 'latest-day' ? ' · Usage cost excludes standing charge.' : ''}</p></article><article class="card"><h2>Recorded-period trend</h2><div class="number">${change === null ? 'Not comparable' : (change >= 0 ? '+' : '') + change.toFixed(1) + '%'}</div><p class="muted">${period === 'latest-day' ? 'Compares adjacent recorded hours in the complete London day.' : 'Requires adjacent past periods with all dates present. Missing intervals are not adjusted.'}</p></article><article class="card"><h2>Historical peak hour</h2><div class="number">${showHourlyPeak ? peakLabel : 'Review heating'}</div><p class="muted">${showHourlyPeak ? 'Europe/London, averaged across available readings. Not a cheapest-rate prediction.' : 'Compare cold-day demand and heating schedules.'}</p></article>`;
    $('#title').textContent = fuel[0].toUpperCase() + fuel.slice(1) + ' ' + period + ' recorded consumption';
    $('#range').textContent = last ? `${groups[0].key} to ${last.key} · Up to 90 recorded periods. Latest period may be partial.` : 'No data for this fuel.';
    if (period === 'latest-day') {
      $('#title').textContent = fuel[0].toUpperCase() + fuel.slice(1) + ' latest available day';
      const dateLabel = latestDay ? new Intl.DateTimeFormat('en-GB', {timeZone: 'UTC', day: 'numeric', month: 'short', year: 'numeric'}).format(new Date(latestDay.date + 'T00:00:00Z')) : '';
      $('#range').textContent = latestDay ? `Latest available day: ${dateLabel} · ${groups.length} hourly periods · Usage cost excludes standing charge. Full recorded coverage for supply points observed that day.` : 'No complete London day available for this fuel.';
    }
    $('#action').textContent = loadError || !last || last.kwh === null || last.cost === null || freshness.state !== 'ready' ? 'Refresh and check meter data and tariff rates before making decisions. Confirm any event invitation in your Octopus account.' :
      fuel === 'electricity' ? 'For reward events, follow only your Octopus-confirmed invitation. Outside events, check your tariff before moving flexible loads; your peak-demand hour is not necessarily your highest-cost hour.' :
      'If gas demand rises, check heating times, thermostat settings, hot-water demand and draughts. Keep essential heating and frost protection on.';
    draw(groups);
  }

  async function load() {
    if (busy) return false;
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
      return true;
    } catch (_) {
      loadError = true;
      if (data) render();
      else setStatus('error', location.protocol === 'file:' ? 'Serve this directory over loopback HTTP to load data.json. See the dashboard operations guide.' : 'Cannot load dashboard data. Check authentication, network access and published files, then retry.');
      return false;
    } finally {
      clearTimeout(timeout);
      busy = false;
      $('#refresh').disabled = refreshBusy;
    }
  }

  async function refreshJson(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(new URL(path, document.baseURI), {
        cache: 'no-store', credentials: 'same-origin', redirect: 'error', ...options, signal: controller.signal,
      });
      return {response, body: await response.json()};
    } finally { clearTimeout(timeout); }
  }

  async function requestRefresh() {
    if (refreshBusy || busy) return;
    refreshBusy = true;
    $('#refresh').disabled = true;
    const message = (state, text) => { $('#refresh-status').dataset.state = state; $('#refresh-status').textContent = text; };
    const finish = (state, text) => {
      refreshBusy = false;
      $('#refresh').disabled = busy;
      message(state, text);
    };
    let beforeReadings = data ? {...data.latest_reading_at} : null;
    const finishTerminal = (status, previous = false) => {
      const prefix = previous ? 'Previous attempt: ' : '';
      const queued = status.request_state === 'pending' ?
        status.result === 'success' ? ' Another update is queued.' : ' A retry is queued.' : '';
      if (status.result === 'failed' || status.result === 'partial') {
        const neutral = status.error_category === 'superseded_release';
        finish(neutral ? 'requested' : 'error', prefix +
          (status.result === 'partial' ? 'Only part of the source was available. ' : '') +
          refreshFailureMessage(status.error_category) + (neutral ? '' : ' Old data is kept.') + queued);
      } else {
        const newer = ['electricity', 'gas'].some(kind => Date.parse(data?.latest_reading_at?.[kind]) > Date.parse(beforeReadings[kind] || '1970-01-01T00:00:00Z'));
        const text = status.result === 'no_data' || !newer ?
          'No newer readings from Octopus yet. A 1–2 day meter-data lag is normal. The latest published snapshot is kept.' :
          'Updated. New latest-reading timestamps and the last successful sync are shown in Data as of.';
        finish('ready', prefix + text + queued);
      }
    };
    let baseline;
    message('requested', 'Requested: preparing a background fetch.');
    try {
      const initial = await refreshJson('status.json');
      if (!initial.response.ok) throw new Error('Status unavailable');
      baseline = validateRefreshStatus(initial.body);
      beforeReadings ??= baseline.latest_reading_at;
      const reply = await refreshJson('refresh.php', {method: 'POST', headers: {'X-Energy-Refresh': '1'}});
      if (reply.body?.state === 'auth_unavailable') {
        finish('error', 'Protected refresh authentication is unavailable on this hosting handler. The existing snapshot is kept.'); return;
      }
      if (reply.response.status === 429 && reply.body?.state === 'rate_limited') {
        const retry = Number.isFinite(reply.body.retry_after) ? Math.max(1, Math.min(300, Math.ceil(reply.body.retry_after))) : 300;
        finish('warning', `Rate limited. Try again in ${retry} seconds. The existing snapshot is kept.`); return;
      }
      if (!reply.response.ok || !['pending', 'already_running', 'retry_queued'].includes(reply.body?.state)) throw new Error('Request unavailable');
      const timestamp = value => value === null || (validInstant(value) && value.endsWith('Z'));
      if (!Object.hasOwn(reply.body, 'request_id') || !Object.hasOwn(reply.body, 'last_attempt_at') ||
          !(reply.body.request_id === null || validRequestId(reply.body.request_id)) ||
          !timestamp(reply.body.last_attempt_at)) throw new Error('Invalid request acknowledgement');
      const requestId = reply.body.request_id;
      const acknowledgedAttempt = reply.body.last_attempt_at;
      const retryAttempt = reply.body.state === 'retry_queued' ? reply.body.last_attempt_at : null;
      if (reply.body.state !== 'pending' && acknowledgedAttempt === null) throw new Error('Missing acknowledged attempt');
      const joining = reply.body.state === 'already_running';
      const untracked = () => finish('warning', 'An earlier request is queued or delayed, but its outcome cannot be tracked here. Check Data as of later; the existing snapshot is kept.');
      if (reply.body.state === 'pending' && requestId === null) { untracked(); return; }
      const deadline = Date.now() + 10 * 60 * 1000;
      message(joining ? 'running' : 'requested', joining ? 'Already running: fetching from Octopus.' : 'Requested. Waiting for the background runner.');
      let observedSuccess = baseline.last_success_at;
      async function poll() {
        if (Date.now() >= deadline) {
          finish('warning', 'Unable to confirm this request’s outcome. It may still be queued or delayed. The existing snapshot is kept; check Data as of or try again later.'); return;
        }
        try {
          const result = await refreshJson('status.json');
          if (!result.response.ok) throw new Error('Status unavailable');
          const status = validateRefreshStatus(result.body);
          lastRefreshStatus = status;
          const successChanged = status.last_success_at !== null &&
            (observedSuccess === null || compareUtcTimestamps(status.last_success_at, observedSuccess) > 0);
          if (successChanged) {
            if (busy) { setTimeout(poll, 2000); return; }
            if (!await load()) { finish('error', 'The update finished, but the published snapshot could not be reloaded. Old data is kept.'); return; }
            observedSuccess = status.last_success_at;
          }
          const attemptOrder = status.last_attempt_at !== null && acknowledgedAttempt !== null ?
            compareUtcTimestamps(status.last_attempt_at, acknowledgedAttempt) : null;
          const tracked = requestId !== null && status.request_id === requestId && status.last_attempt_at !== null &&
            (reply.body.state === 'pending' || (attemptOrder !== null && attemptOrder >= 0));
          const activeJoin = joining && requestId === null && attemptOrder === 0;
          const legacyRetry = retryAttempt !== null && requestId === null && status.request_id === null &&
            attemptOrder === 0 && status.request_state === 'pending' &&
            ['no_data', 'partial', 'failed'].includes(status.result);
          const matches = tracked || activeJoin || legacyRetry;
          if ((status.request_state === 'running' || status.result === 'running') && matches) {
            message('running', 'Fetching from Octopus…');
          } else {
            const previousRetry = reply.body.state === 'retry_queued' && matches && attemptOrder === 0 &&
              status.request_state === 'pending' && ['no_data', 'partial', 'failed'].includes(status.result);
            if (['success', 'no_data', 'partial', 'failed'].includes(status.result) && matches) {
              finishTerminal(status, previousRetry); return;
            }
            if (reply.body.state === 'retry_queued' && requestId === null) { untracked(); return; }
            message('requested', status.result === 'running' || status.request_state === 'running' ?
              'Requested. Another update is running; this request is still queued.' :
              'Requested. Waiting for the background runner to handle this request.');
          }
          setTimeout(poll, 2000);
        } catch (_) { finish('error', 'Refresh status is unavailable. The existing snapshot is kept; check again later.'); }
      }
      await poll();
    } catch (_) { finish('error', 'The refresh could not be requested. The existing snapshot is kept; check access and try again.'); }
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
  $('#refresh').onclick = requestRefresh;
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') load(); });
  setInterval(() => { if (document.visibilityState === 'visible') load(); }, 5 * 60 * 1000);
  for (const event of ['resize', 'scroll']) {
    window.addEventListener(event, () => repositionTooltip(), {passive: true});
    window.visualViewport?.addEventListener(event, () => repositionTooltip(), {passive: true});
  }
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(() => repositionTooltip()).observe($('#chart-tooltip'));
  }
  load();
}

if (typeof document !== 'undefined') initializeDashboard();
