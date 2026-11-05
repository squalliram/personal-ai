'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../octopus_energy/dashboard.js'), 'utf8');
const pure = vm.createContext({Intl});
vm.runInContext(source, pure);
const call = (name, ...args) => pure[name](...args);
const data = () => ({
  generated_at: new Date().toISOString(),
  latest_reading_at: {electricity: new Date().toISOString()},
  last_successful_sync_at: new Date().toISOString(),
  daily: [
    {date: '2026-10-31', fuel: 'electricity', kwh: 1, cost: 1},
    {date: '2026-11-01', fuel: 'electricity', kwh: 2, cost: 2},
    {date: '2026-11-02', fuel: 'electricity', kwh: 3, cost: 3},
  ],
  hourly: [{fuel: 'electricity', hour: 12, kwh: 2}],
  notice: 'Synthetic whole-home fixture',
});

assert.equal(call('periodKey', '2026-11-01', 'week'), '2026-10-26');
assert.equal(call('periodKey', '2027-01-01', 'week'), '2026-12-28');
assert.equal(call('periodKey', '2026-03-29', 'week'), '2026-03-23');
const weeks = call('groupRows', data().daily, 'electricity', 'week');
assert.equal(weeks.length, 2);
assert.equal(weeks[0].kwh, 3);
assert.equal(weeks[0].cost, 3);
assert.equal(weeks[1].key, '2026-11-02');
assert.equal(call('groupRows', [...data().daily].reverse(), 'electricity', 'week')[0].key, '2026-10-26');
assert.equal(call('groupRows', [{date: '2026-11-01', fuel: 'gas', kwh: 2, cost: null}], 'gas', 'day')[0].cost, null);
const unknown = {date: '2026-11-03', fuel: 'gas', kwh: null, cost: null, cost_status: 'incomplete', cost_reason: 'unknown_gas_units'};
const gasMonth = call('groupRows', [{date: '2026-11-01', fuel: 'gas', kwh: 2, cost: 1}, unknown], 'gas', 'month');
assert.equal(gasMonth[0].kwh, null, 'unknown usage must not become a partial or zero total');
assert.equal(gasMonth[0].cost, null);
assert.equal(call('validateData', {...data(), daily: [unknown]}).daily[0].kwh, null);
const days = call('groupRows', data().daily, 'electricity', 'day');
assert.equal(call('trendChange', days, 'day', '2026-11-05'), 50);
assert.equal(call('trendChange', days, 'day', '2026-11-02'), null);
assert.equal(call('trendChange', call('groupRows', [data().daily[0], data().daily[2]], 'electricity', 'day'), 'day', '2026-11-05'), null);
assert.equal(call('trendChange', call('groupRows', [{date: '2026-11-01', fuel: 'gas', kwh: 0, cost: 0}, {date: '2026-11-02', fuel: 'gas', kwh: 1, cost: 1}], 'gas', 'day'), 'day', '2026-11-05'), null);
assert.equal(call('trendChange', weeks, 'week', '2026-11-05'), null);
assert.equal(call('londonDate', new Date('2026-07-01T23:30:00Z')), '2026-07-02');
const now = Date.parse('2026-11-05T12:00:00Z');
const timestamp = milliseconds => new Date(now - milliseconds).toISOString();
for (const [age, state] of [[48 * 3600000, 'ready'], [48 * 3600000 + 1, 'warning'], [-300001, 'warning']]) {
  assert.equal(call('readingState', {generated_at: timestamp(0), latest_reading_at: {electricity: timestamp(age)}}, 'electricity', now).state, state);
}
assert.equal(call('readingState', {generated_at: timestamp(86400001), latest_reading_at: {electricity: timestamp(0)}}, 'electricity', now).state, 'warning');
assert.equal(call('readingState', data(), 'gas', now).state, 'warning');
for (const bad of ['2026-02-30', '2026-13-01', '<script>']) assert.equal(call('validDate', bad), false);
assert.equal(call('validateData', data()).daily.length, 3);
for (const bad of [null, {}, {...data(), daily: [{date: '<img>', fuel: 'electricity', kwh: 1, cost: 1}]}, {...data(), hourly: [{fuel: 'electricity', hour: 24, kwh: 2}]}, {...data(), daily: [data().daily[0], data().daily[0]]}, {...data(), daily: [{...data().daily[0], cost_reason: [123]}]}]) {
  assert.throws(() => call('validateData', bad));
}

function completeGroup(key, period, kwh, cost = 1) {
  const {start, end} = call('periodEnd', key, period);
  const dates = new Set();
  for (let date = start.getTime(); date < end.getTime(); date += 86400000) dates.add(new Date(date).toISOString().slice(0, 10));
  return {key, kwh, cost, dates, reasons: new Set()};
}

const anomalyDays = [10, 10, 10, 10, 10, 10, 100].map((value, index) => completeGroup(`2026-10-0${index + 1}`, 'day', value));
const anomaly = call('anomalyModel', anomalyDays, 'day', '2026-11-05');
assert.equal(anomaly.samples, 7);
assert.equal(anomaly.baseline, 160 / 7);
assert.ok(Math.abs(anomaly.threshold - (160 + 180 * Math.sqrt(6)) / 7) < 1e-10, 'reuse population, not sample, standard deviation');
assert.deepEqual([...anomaly.high], ['2026-10-07']);
assert.equal(call('anomalyModel', anomalyDays.slice(1), 'day', '2026-11-05').threshold, null);
const boundary = [10, 10, 10, 10, 10, 10, 8, 12].map((value, index) => completeGroup(`2026-10-0${index + 1}`, 'day', value));
assert.equal(call('anomalyModel', boundary, 'day', '2026-11-05').threshold, 12);
assert.equal(call('anomalyModel', boundary, 'day', '2026-11-05').high.size, 0, 'equal to cutoff is not unusually high');
boundary.at(-1).kwh = 12.001;
assert.equal(call('anomalyModel', boundary, 'day', '2026-11-05').high.size, 1);
assert.equal(call('anomalyModel', anomalyDays.map(group => ({...group, kwh: 10})), 'day', '2026-11-05').high.size, 0);
assert.equal(call('anomalyModel', anomalyDays.map((group, index) => ({...group, kwh: index ? group.kwh : null})), 'day', '2026-11-05').threshold, null);
assert.equal(call('anomalyModel', anomalyDays, 'day', '2026-10-07').threshold, null, 'ongoing period is excluded');
for (const period of ['week', 'month', 'year']) {
  const keys = period === 'week' ? Array.from({length: 7}, (_, index) => new Date(Date.UTC(2026, 7, 3 + index * 7)).toISOString().slice(0, 10)) :
    period === 'month' ? ['2025-01', '2025-02', '2025-03', '2025-04', '2025-05', '2025-06', '2025-07'] :
      ['2018', '2019', '2020', '2021', '2022', '2023', '2024'];
  const groups = keys.map((key, index) => completeGroup(key, period, index === 6 ? 100 : 10));
  assert.equal(call('anomalyModel', groups, period, '2026-11-05').high.has(keys[6]), true);
  groups[0].dates.delete([...groups[0].dates][0]);
  assert.equal(call('anomalyModel', groups, period, '2026-11-05').threshold, null, 'missing-date aggregate is not a comparable period');
}
const cappedRows = Array.from({length: 100}, (_, index) => ({date: new Date(Date.UTC(2025, 0, index + 1)).toISOString().slice(0, 10), fuel: 'electricity', kwh: index < 10 ? 1000 : 10, cost: 1}));
const capped = call('groupRows', cappedRows, 'electricity', 'day');
assert.equal(capped.length, 90);
assert.equal(call('anomalyModel', cappedRows.map(row => completeGroup(row.date, 'day', row.kwh)), 'day', '2026-11-05').baseline, 10);
assert.equal(call('anomalyModel', call('groupRows', [...cappedRows, ...cappedRows.map(row => ({...row, fuel: 'gas', kwh: 10000}))], 'electricity', 'day'), 'day', '2026-11-05').baseline, 10);
assert.match(call('periodRangeLabel', '2026-03-29', 'day'), /29 March 2026 00:00 → 30 March 2026 00:00 Europe\/London/);
assert.match(call('periodRangeLabel', '2026-10-25', 'day'), /25 October 2026 00:00 → 26 October 2026 00:00/);
assert.match(call('periodRangeLabel', '2026-12-28', 'week'), /4 January 2027 00:00/);
assert.match(call('periodRangeLabel', '2024-02', 'month'), /1 February 2024 00:00 → 1 March 2024 00:00/);
assert.match(call('periodRangeLabel', '2024', 'year'), /1 January 2024 00:00 → 1 January 2025 00:00/);
const gasEstimate = call('contributorEstimate', anomalyDays.at(-1), 'day', 'gas', [{fuel: 'gas', hour: 5, kwh: 2}, {fuel: 'electricity', hour: 19, kwh: 100}], 10);
assert.equal(gasEstimate.peakHour, 5);
assert.equal(gasEstimate.season, 'cool-season');
assert.equal(gasEstimate.largeUplift, true);
assert.match(gasEstimate.contributors.join(' '), /heating.*hot-water.*overnight/i);
const summer = call('contributorEstimate', completeGroup('2026-07-01', 'day', 14.9), 'day', 'electricity', [], 10);
assert.equal(summer.season, 'warm-season');
assert.equal(summer.peakHour, null);
assert.equal(summer.largeUplift, false);
assert.match(summer.contributors.join(' '), /without an hourly profile.*Cooling/);
assert.equal(call('contributorEstimate', completeGroup('2026-07-01', 'day', 15), 'day', 'electricity', [], 10).largeUplift, true);
assert.equal(call('contributorEstimate', completeGroup('2024', 'year', 100), 'year', 'gas', [], 10).season, 'mixed-season');
const highTooltip = call('tooltipContent', {...anomalyDays.at(-1), cost: null}, 'day', 'gas', [{fuel: 'gas', hour: 5, kwh: 2}], anomaly);
assert.match(highTooltip, /100 kWh/);
assert.match(highTooltip, /Cost unavailable/);
assert.doesNotMatch(highTooltip, /£0\.00/);
assert.match(highTooltip, /Likely contributors · estimates, not measured/);
assert.match(highTooltip, /05:00 Europe\/London; not this period&#39;s measured timing/);
assert.match(highTooltip, /No device-level data/);
assert.doesNotMatch(call('tooltipContent', anomalyDays[0], 'day', 'gas', [], anomaly), /Likely contributors|Unusually high/);
const flatLayout = call('chartLayout', [completeGroup('2026-10-01', 'day', 0)]);
assert.equal(flatLayout.points[0].x, (flatLayout.left + flatLayout.right) / 2);
assert.equal(Number.isFinite(flatLayout.points[0].y), true);
const negativeLayout = call('chartLayout', [completeGroup('2026-10-01', 'day', -2), completeGroup('2026-10-02', 'day', 1)]);
assert.equal(negativeLayout.points.every(point => point.y >= negativeLayout.top && point.y <= negativeLayout.bottom), true);

function element(dataset = {}) {
  return {dataset, textContent: '', innerHTML: '', disabled: false, hidden: true, attributes: {}, style: {},
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getBoundingClientRect() { return {left: 100, top: 50, width: 360, height: 180}; },
    classList: {toggle() {}}};
}

function browserHarness(fetchImpl, protocol = 'https:') {
  const ids = Object.fromEntries(['status', 'refresh', 'updated', 'cards', 'chart', 'title', 'range', 'action', 'notice', 'chart-tooltip', 'chart-stage', 'chart-scroll'].map(id => ['#' + id, element()]));
  const fuels = ['electricity', 'gas'].map(fuel => element({fuel}));
  const periods = ['day', 'week', 'month', 'year'].map(period => element({period}));
  const listeners = {}, timers = new Map(), intervals = [];
  const windowListeners = {}, viewportListeners = {}, resizeObservers = [];
  const window = {
    innerWidth: 1280, innerHeight: 900, scrollY: 0,
    addEventListener: (event, callback) => { windowListeners[event] = callback; },
    visualViewport: {width: 1280, height: 900, offsetLeft: 0, offsetTop: 0,
      addEventListener: (event, callback) => { viewportListeners[event] = callback; }},
  };
  class ResizeObserver {
    constructor(callback) { this.callback = callback; resizeObservers.push(this); }
    observe(target) { this.target = target; }
  }
  const requests = [];
  const document = {
    baseURI: 'https://private.test/energy-usage/', visibilityState: 'visible',
    activeElement: null,
    querySelector: selector => ids[selector],
    querySelectorAll: selector => selector === '[data-fuel]' ? fuels : periods,
    addEventListener: (event, callback) => { listeners[event] = callback; },
  };
  const focus = element => {
    if (document.activeElement === element) return;
    const previous = document.activeElement;
    document.activeElement = element;
    if (previous?.onblur) previous.onblur();
    if (element?.onfocus) element.onfocus();
  };
  for (const button of [...fuels, ...periods, ids['#refresh']]) {
    button.focus = () => focus(button);
    let onclick;
    Object.defineProperty(button, 'onclick', {get: () => onclick, set: callback => { onclick = (...args) => { button.focus(); return callback(...args); }; }});
  }
  let pointElements = [], vectors = {}, chartMarkup = '';
  const chart = ids['#chart'];
  Object.defineProperty(chart, 'innerHTML', {
    get: () => chartMarkup,
    set: value => {
      if (pointElements.includes(document.activeElement)) document.activeElement = null;
      chartMarkup = value;
      pointElements = [...value.matchAll(/<g class="chart-point"([^>]*)>/g)].map(match => {
        const attributes = Object.fromEntries([...match[1].matchAll(/([-\w]+)="([^"]*)"/g)].map(attribute => [attribute[1], attribute[2]]));
        const point = element({pointIndex: attributes['data-point-index'], periodKey: attributes['data-period-key']});
        point.attributes = attributes;
        point.focus = () => focus(point);
        return point;
      });
      vectors = Object.fromEntries(['chart-crosshair', 'chart-crosshair-x', 'chart-crosshair-y', 'chart-active-point'].map(id => ['#' + id, element()]));
      vectors['#chart-crosshair'].attributes.visibility = 'hidden';
    },
  });
  chart.querySelectorAll = () => pointElements;
  chart.querySelector = selector => vectors[selector];
  chart.contains = element => pointElements.includes(element);
  chart.getBoundingClientRect = ids['#chart-stage'].getBoundingClientRect = () => ({left: 100, top: 50, width: 960, height: 400});
  let timerId = 0;
  const context = vm.createContext({
    document, window, ResizeObserver, location: {protocol}, URL, AbortController, Intl,
    fetch: (...args) => { requests.push(args); return fetchImpl(...args); },
    setTimeout: callback => { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
    setInterval: (callback, duration) => { intervals.push({callback, duration}); },
  });
  vm.runInContext(source, context);
  return {ids, fuels, periods, listeners, timers, intervals, requests, document, window, windowListeners, viewportListeners, resizeObservers, get points() { return pointElements; }, get vectors() { return vectors; }};
}

const response = fixture => ({ok: true, json: async () => fixture});
const flush = () => new Promise(resolve => setImmediate(resolve));

function clickChart(page, target, clientX, pointerType = 'mouse') {
  let stopped = false;
  const event = {target, clientX, pointerType, stopPropagation() { stopped = true; }};
  if (target.onclick) target.onclick(event);
  if (!stopped) page.ids['#chart'].onclick(event);
}

(async () => {
  let resolve, next = () => new Promise(done => { resolve = done; });
  const page = browserHarness((...args) => next(...args));
  assert.match(page.ids['#status'].textContent, /Loading/);
  assert.equal(page.ids['#refresh'].disabled, true);
  page.ids['#refresh'].onclick();
  assert.equal(page.requests.length, 1, 'overlapping refresh must not issue a second request');
  assert.equal(page.requests[0][0].href, 'https://private.test/energy-usage/data.json');
  assert.equal(page.requests[0][1].cache, 'no-store');
  assert.equal(page.requests[0][1].credentials, 'same-origin');
  resolve(response(data()));
  await flush();
  assert.equal(page.ids['#status'].dataset.state, 'ready');
  assert.equal(page.ids['#refresh'].disabled, false);
  assert.equal(page.timers.size, 0);
  assert.match(page.ids['#updated'].textContent, /Latest electricity reading/);
  const chart = page.ids['#chart'], tooltip = page.ids['#chart-tooltip'];
  assert.match(chart.innerHTML, /Date \(Europe\/London\)/);
  assert.match(chart.innerHTML, /Energy used \(kWh\)/);
  assert.equal(page.points.length, 3);
  assert.equal(page.points.filter(point => point.attributes.tabindex === '0').length, 1);
  assert.equal(page.points[0].attributes.role, 'button');
  assert.equal(page.points[0].attributes['aria-describedby'], 'chart-tooltip');
  assert.match(page.points[0].attributes['aria-label'], /31 October 2026, 1 kWh, £1\.00/);
  assert.equal(page.document.fonts, undefined, 'interaction must not depend on a loaded font or FontFace API');
  chart.onpointermove({clientX: 1028, pointerType: 'mouse'});
  assert.equal(tooltip.hidden, false);
  assert.match(tooltip.innerHTML, /2 November 2026 00:00 → 3 November 2026 00:00/);
  assert.match(tooltip.innerHTML, /3 kWh|£3\.00/);
  assert.doesNotMatch(tooltip.innerHTML, /Likely contributors/);
  assert.equal(page.vectors['#chart-crosshair'].attributes.visibility, 'visible');
  assert.equal(page.vectors['#chart-active-point'].attributes.cx, '928');
  const originalBounds = chart.getBoundingClientRect;
  for (const width of [960, 720]) {
    chart.getBoundingClientRect = () => ({left: 100, top: 50, width, height: width * 400 / 960});
    const hover = x => chart.onpointermove({clientX: 100 + x / 960 * width, pointerType: 'mouse'});
    for (const x of [88, 86.5, 82]) {
      hover(x);
      assert.equal(tooltip.hidden, false, 'outer half of first marker stays hoverable');
      assert.equal(page.vectors['#chart-active-point'].attributes.cx, '90');
      assert.equal(page.vectors['#chart-crosshair'].attributes.visibility, 'visible');
    }
    for (const x of [930, 931.5, 936]) {
      hover(x);
      assert.equal(tooltip.hidden, false, 'outer half of last marker stays hoverable');
      assert.equal(page.vectors['#chart-active-point'].attributes.cx, '928');
      assert.equal(page.vectors['#chart-crosshair'].attributes.visibility, 'visible');
    }
    for (const x of [81.999, 936.001, 0, 960]) {
      hover(x);
      assert.equal(tooltip.hidden, true, 'outside the endpoint tolerance must not select a phantom period');
      assert.equal(page.vectors['#chart-crosshair'].attributes.visibility, 'hidden');
    }
  }
  chart.getBoundingClientRect = originalBounds;
  page.ids['#chart-stage'].onpointerleave();
  assert.equal(tooltip.hidden, true);
  page.points[0].focus();
  assert.match(tooltip.innerHTML, /31 October 2026/);
  let prevented = false;
  const key = value => { prevented = false; chart.onkeydown({key: value, preventDefault() { prevented = true; }}); };
  key('ArrowRight');
  assert.equal(prevented, true);
  assert.equal(page.document.activeElement, page.points[1]);
  assert.match(tooltip.innerHTML, /1 November 2026/);
  key('Home');
  assert.equal(page.document.activeElement, page.points[0]);
  key('End');
  assert.equal(page.document.activeElement, page.points[2]);
  key('ArrowRight');
  assert.equal(page.document.activeElement, page.points[2], 'navigation clamps at the final point');
  key('ArrowUp');
  assert.equal(page.document.activeElement, page.points[1]);
  key('ArrowDown');
  assert.equal(page.document.activeElement, page.points[2]);
  key('ArrowLeft');
  assert.equal(page.document.activeElement, page.points[1]);
  assert.equal(page.points.filter(point => point.attributes.tabindex === '0').length, 1);
  key('Escape');
  assert.equal(tooltip.hidden, true);
  assert.equal(page.vectors['#chart-crosshair'].attributes.visibility, 'hidden');
  assert.equal(page.document.activeElement, page.points[1], 'Escape dismisses without moving focus');
  key('Enter');
  assert.equal(tooltip.hidden, false);
  key('Escape'); key(' ');
  assert.equal(tooltip.hidden, false);
  key('Tab');
  assert.equal(prevented, false, 'native Tab can leave the chart; never trap keyboard focus');
  key('Escape');
  chart.onpointermove({clientX: 190, pointerType: 'touch'});
  assert.equal(tooltip.hidden, true, 'touch hover is not a selection');
  chart.onclick({clientX: 190});
  assert.equal(page.document.activeElement, page.points[0]);
  assert.equal(tooltip.hidden, false, 'tap selects a point');
  clickChart(page, page.points[2], 1028);
  assert.equal(page.document.activeElement, page.points[2]);
  page.ids['#chart-stage'].onpointerleave();
  assert.equal(tooltip.hidden, false, 'a focused tooltip remains available to keyboard users');
  const previousPoint = page.document.activeElement;
  const refreshed = data();
  refreshed.daily.at(-1).kwh = 4;
  next = async () => response(refreshed);
  page.intervals[0].callback();
  await flush();
  assert.notEqual(page.document.activeElement, previousPoint);
  assert.equal(page.document.activeElement.dataset.periodKey, '2026-11-02', 'auto-refresh restores the focused period after rebuilding SVG');
  assert.match(tooltip.innerHTML, /4 kWh/);
  page.ids['#chart-scroll'].onscroll();
  assert.match(tooltip.style.left, /px$/);
  page.periods[1].onclick();
  assert.match(page.ids['#cards'].innerHTML, /Monday start/);
  assert.match(page.ids['#range'].textContent, /2026-10-26 to 2026-11-02/);
  assert.equal(page.periods[1].attributes['aria-pressed'], 'true');
  page.fuels[1].onclick();
  assert.match(page.ids['#status'].textContent, /No recorded gas data/);
  assert.match(page.ids['#cards'].innerHTML, /No data/);
  assert.equal(page.ids['#chart'].innerHTML, '');
  page.fuels[0].onclick();
  next = async () => ({ok: false});
  await page.ids['#refresh'].onclick();
  assert.equal(page.ids['#status'].dataset.state, 'error');
  assert.match(page.ids['#status'].textContent, /last loaded snapshot/);
  page.fuels[1].onclick();
  assert.equal(page.ids['#status'].dataset.state, 'error', 'filter changes must not hide a failed reload');
  page.fuels[0].onclick();
  const stale = data();
  stale.latest_reading_at.electricity = '2020-01-01T00:00:00Z';
  next = async () => response(stale);
  await page.ids['#refresh'].onclick();
  assert.match(page.ids['#status'].textContent, /older than 48 hours/);
  assert.match(page.ids['#action'].textContent, /Refresh and check/);
  const missingCost = data();
  missingCost.daily.at(-1).cost = null;
  next = async () => response(missingCost);
  page.periods[0].onclick();
  await page.ids['#refresh'].onclick();
  assert.match(page.ids['#cards'].innerHTML, /Cost unavailable/);
  page.points.at(-1).focus();
  assert.match(tooltip.innerHTML, /Cost unavailable/);
  assert.doesNotMatch(tooltip.innerHTML, /£0\.00/);
  const confirmedGas = data();
  confirmedGas.latest_reading_at.gas = new Date().toISOString();
  confirmedGas.daily.push({date: '2026-11-02', fuel: 'gas', kwh: 4, cost: 1});
  confirmedGas.hourly.push(...Array.from({length: 24}, (_, hour) => ({fuel: 'gas', hour, kwh: hour === 5 ? 3 : 0.2})));
  next = async () => response(confirmedGas);
  await page.ids['#refresh'].onclick();
  assert.match(page.ids['#cards'].innerHTML, /Historical peak hour<\/h2><div class="number">12:00<\/div>/);
  page.fuels[1].onclick();
  assert.match(page.ids['#cards'].innerHTML, /Historical peak hour<\/h2><div class="number">05:00<\/div>/);
  assert.match(page.ids['#cards'].innerHTML, /Europe\/London, averaged across available readings/);
  assert.doesNotMatch(page.ids['#cards'].innerHTML, /Review heating/);
  page.fuels[0].onclick();
  assert.match(page.ids['#cards'].innerHTML, /Historical peak hour<\/h2><div class="number">12:00<\/div>/);
  const unverifiedGas = {...data(), daily: [unknown]};
  unverifiedGas.latest_reading_at.gas = new Date().toISOString();
  next = async () => response(unverifiedGas);
  await page.ids['#refresh'].onclick();
  page.fuels[1].onclick();
  assert.match(page.ids['#status'].textContent, /source units are unverified/);
  assert.match(page.ids['#cards'].innerHTML, /Usage unavailable/);
  assert.match(page.ids['#cards'].innerHTML, /unverified gas units/);
  assert.match(page.ids['#cards'].innerHTML, /Historical peak hour<\/h2><div class="number">Review heating<\/div>/);
  assert.match(page.ids['#cards'].innerHTML, /Compare cold-day demand and heating schedules/);
  assert.doesNotMatch(page.ids['#cards'].innerHTML, /05:00/);
  assert.doesNotMatch(page.ids['#cards'].innerHTML, /0\.0 kWh|£0\.00/);
  assert.equal(page.ids['#chart'].innerHTML, '');
  page.fuels[0].onclick();
  const empty = {...data(), daily: [], hourly: [], latest_reading_at: {}};
  next = async () => response(empty);
  await page.ids['#refresh'].onclick();
  assert.match(page.ids['#status'].textContent, /No recorded electricity data/);
  assert.match(page.ids['#cards'].innerHTML, /Historical peak hour<\/h2><div class="number">No profile<\/div>/);
  assert.doesNotMatch(page.ids['#cards'].innerHTML, /£0\.00/);
  assert.equal(page.intervals[0].duration, 300000);
  page.document.visibilityState = 'hidden';
  const count = page.requests.length;
  page.intervals[0].callback();
  assert.equal(page.requests.length, count);
  page.document.visibilityState = 'visible';
  page.listeners.visibilitychange();
  await flush();
  assert.equal(page.requests.length, count + 1);

  const axesPage = browserHarness(async () => response(data()));
  await flush();
  for (const [index, title, range] of [
    [0, 'Date', /31 October 2026 00:00 → 1 November 2026 00:00/],
    [1, 'Week commencing', /26 October 2026 00:00 → 2 November 2026 00:00/],
    [2, 'Month', /1 October 2026 00:00 → 1 November 2026 00:00/],
    [3, 'Year', /1 January 2026 00:00 → 1 January 2027 00:00/],
  ]) {
    axesPage.periods[index].onclick();
    assert.ok(axesPage.ids['#chart'].innerHTML.includes(title + ' (Europe/London)'));
    assert.match(axesPage.ids['#chart'].innerHTML, /Energy used \(kWh\)/);
    axesPage.points[0].focus();
    assert.match(axesPage.ids['#chart-tooltip'].innerHTML, range);
    assert.match(axesPage.ids['#chart-tooltip'].innerHTML, /kWh/);
  }
  const gapData = data();
  gapData.daily[1].kwh = gapData.daily[1].cost = null;
  const gapPage = browserHarness(async () => response(gapData));
  await flush();
  assert.equal(gapPage.points.length, 2);
  const path = gapPage.ids['#chart'].innerHTML.match(/class="chart-series" d="([^"]*)"/)[1];
  assert.equal((path.match(/M/g) || []).length, 2, 'null usage remains a gap, not zero or a connected interpolation');
  gapPage.points[0].focus();
  gapPage.ids['#chart'].onkeydown({key: 'ArrowRight', preventDefault() {}});
  assert.equal(gapPage.document.activeElement.dataset.pointIndex, '2', 'keyboard skips unavailable usage');
  gapPage.ids['#chart'].onpointermove({clientX: 609, pointerType: 'mouse'});
  assert.equal(gapPage.ids['#chart-tooltip'].hidden, true, 'hovering the missing period must not borrow another period value');
  const highData = {...data(), daily: anomalyDays.map(group => ({date: group.key, fuel: 'gas', kwh: group.kwh, cost: null})),
    hourly: [{fuel: 'gas', hour: 5, kwh: 2}], latest_reading_at: {gas: new Date().toISOString()}};
  const highPage = browserHarness(async () => response(highData));
  await flush();
  highPage.fuels[1].onclick();
  highPage.points.at(-1).focus();
  assert.match(highPage.ids['#chart-tooltip'].innerHTML, /Likely contributors · estimates, not measured/);
  assert.match(highPage.ids['#chart-tooltip'].innerHTML, /heating and hot-water/);
  assert.match(highPage.points.at(-1).attributes['aria-label'], /unusually high/);
  assert.equal((highPage.ids['#chart'].innerHTML.match(/class="point-high"/g) || []).length, 1);
  highPage.points[0].focus();
  assert.doesNotMatch(highPage.ids['#chart-tooltip'].innerHTML, /Likely contributors|Unusually high/);
  const mobilePage = browserHarness(async () => response(highData));
  await flush();
  mobilePage.fuels[1].onclick();
  const mobileTooltip = mobilePage.ids['#chart-tooltip'], mobileChart = mobilePage.ids['#chart'];
  mobilePage.window.innerWidth = mobilePage.window.visualViewport.width = 390;
  mobilePage.window.innerHeight = mobilePage.window.visualViewport.height = 844;
  mobilePage.window.scrollY = 1400;
  let stageTop = 394, contentHeight = 440;
  mobilePage.ids['#chart-stage'].getBoundingClientRect = () => ({left: 16, top: stageTop, width: 358, height: 300});
  mobileChart.getBoundingClientRect = () => ({left: -324, top: stageTop, width: 720, height: 300});
  mobileTooltip.getBoundingClientRect = () => ({
    width: Math.min(342, Number.parseFloat(mobileTooltip.style.maxWidth) || 342),
    height: Math.min(contentHeight, Number.parseFloat(mobileTooltip.style.maxHeight) || 440),
  });
  const assertVisibleTooltip = () => {
    const viewport = mobilePage.window.visualViewport;
    const visibleTop = viewport?.offsetTop ?? 0, visibleLeft = viewport?.offsetLeft ?? 0;
    const visibleHeight = viewport?.height ?? mobilePage.window.innerHeight;
    const visibleWidth = viewport?.width ?? mobilePage.window.innerWidth;
    const box = mobileTooltip.getBoundingClientRect();
    const top = stageTop + Number.parseFloat(mobileTooltip.style.top);
    const left = 16 + Number.parseFloat(mobileTooltip.style.left);
    assert.equal(mobileTooltip.hidden, false);
    assert.ok(top >= visibleTop + 8, 'tall tooltip must not clip above the visible viewport');
    assert.ok(top + box.height <= visibleTop + visibleHeight - 8, 'tall tooltip must not clip its disclaimer below the visible viewport');
    assert.ok(left >= visibleLeft + 8 && left + box.width <= visibleLeft + visibleWidth - 8);
    assert.match(mobileTooltip.innerHTML, /No device-level data: these are possibilities to investigate/);
  };
  clickChart(mobilePage, mobilePage.points.at(-1), 372, 'touch');
  assertVisibleTooltip();
  stageTop = -16;
  mobilePage.windowListeners.scroll();
  assertVisibleTooltip();
  mobilePage.window.visualViewport.offsetTop = 120;
  mobilePage.window.visualViewport.height = 320;
  mobilePage.viewportListeners.resize();
  assertVisibleTooltip();
  assert.equal(mobileTooltip.style.maxHeight, '224px', 'short visual viewports use internal scrolling rather than clipping content');
  mobilePage.window.visualViewport.offsetTop = 0;
  mobilePage.window.visualViewport.height = 844;
  stageTop = 394;
  contentHeight = 180;
  mobilePage.viewportListeners.scroll();
  assertVisibleTooltip();
  contentHeight = 440;
  assert.equal(mobilePage.resizeObservers[0].target, mobileTooltip);
  mobilePage.resizeObservers[0].callback();
  assertVisibleTooltip();
  mobilePage.window.visualViewport = null;
  mobilePage.window.innerHeight = 600;
  mobilePage.windowListeners.resize();
  assertVisibleTooltip();
  const capPage = browserHarness(async () => response({...data(), daily: cappedRows}));
  await flush();
  assert.equal(capPage.points.length, 90);
  for (const [x, endpoint] of [[82, '90'], [936, '928']]) {
    capPage.ids['#chart'].onpointermove({clientX: 100 + x, pointerType: 'mouse'});
    assert.equal(capPage.ids['#chart-tooltip'].hidden, false, 'edge tolerance clamps even with the maximum 90-point density');
    assert.equal(capPage.vectors['#chart-active-point'].attributes.cx, endpoint);
  }
  assert.equal((capPage.ids['#chart'].innerHTML.match(/text-anchor="(?:middle|end)">\d+ \w+ 2025/g) || []).length, 6);
  for (const period of ['day', 'week']) {
    const rows = period === 'day' ? cappedRows : Array.from({length: 637}, (_, index) => ({
      date: new Date(Date.UTC(2024, 0, index + 1)).toISOString().slice(0, 10), kwh: 1, cost: 1,
    }));
    const densePage = browserHarness(async () => response({...data(), daily: rows.map(row => ({...row, fuel: 'gas'}))}));
    await flush();
    densePage.fuels[1].onclick();
    if (period === 'week') densePage.periods[1].onclick();
    assert.equal(densePage.points.length, 90);
    const denseChart = densePage.ids['#chart'];
    for (const [pointerType, index] of [['mouse', 30], ['touch', 60]]) {
      denseChart.onkeydown({key: 'Escape', preventDefault() {}});
      const x = 90 + index * (928 - 90) / 89;
      const clientX = 100 + x;
      denseChart.onpointermove({clientX, pointerType});
      if (pointerType === 'touch') assert.equal(densePage.ids['#chart-tooltip'].hidden, true);
      // A later neighbour's 16-unit hit circle overlaps this visible point's centre.
      const visible = densePage.points[index], overlapping = densePage.points[index + 1];
      clickChart(densePage, overlapping, clientX, pointerType);
      assert.equal(densePage.document.activeElement.dataset.periodKey, visible.dataset.periodKey,
        `${pointerType} on dense ${period} chart must select the visually nearest period, not the overlapping DOM target`);
      assert.equal(densePage.ids['#chart-tooltip'].hidden, false);
      assert.ok(Math.abs(Number(densePage.vectors['#chart-active-point'].attributes.cx) - x) < 1e-9);
      assert.equal(densePage.points.filter(point => point.attributes.tabindex === '0').length, 1);
      denseChart.onkeydown({key: 'ArrowRight', preventDefault() {}});
      assert.equal(densePage.document.activeElement, overlapping, 'keyboard navigation continues from the selected visible point');
    }
  }

  for (const fixture of [null, {ok: false}, {ok: true, json: async () => { throw new Error('Not JSON'); }}, response({})]) {
    const broken = browserHarness(async () => {
      if (fixture === null) throw new Error('Offline');
      return fixture;
    });
    await flush();
    assert.equal(broken.ids['#status'].dataset.state, 'error');
    assert.match(broken.ids['#status'].textContent, /Cannot load/);
    assert.equal(broken.ids['#refresh'].disabled, false);
  }
  const file = browserHarness(async () => { throw new Error('file fetch blocked'); }, 'file:');
  await flush();
  assert.match(file.ids['#status'].textContent, /loopback HTTP/);
  const stalled = browserHarness((_, options) => new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('Timeout')))));
  [...stalled.timers.values()][0]();
  await flush();
  assert.equal(stalled.ids['#status'].dataset.state, 'error');
  assert.equal(stalled.ids['#refresh'].disabled, false);
  console.log('Dashboard JavaScript checks passed: axes, period tooltips, high-only estimated contributors, keyboard/tap/hover, nulls, grouping, freshness, reloads, errors and polling.');
})().catch(error => { console.error(error); process.exitCode = 1; });
