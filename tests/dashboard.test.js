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

function latestFixture(start = '2025-11-01T00:00:00Z', count = 24) {
  const fixture = data();
  fixture.latest_day = {electricity: {
    date: call('londonDate', new Date(start)), complete: true, cost_basis: 'usage_excludes_standing_charge',
    hours: Array.from({length: count}, (_, index) => ({
      start: new Date(Date.parse(start) + index * 3600000).toISOString(),
      end: new Date(Date.parse(start) + (index + 1) * 3600000).toISOString(), kwh: index + 1, cost: index === count - 1 ? null : (index + 1) * 0.2,
    })),
  }, gas: null};
  return fixture;
}

const REQUEST_A = 'a'.repeat(32), REQUEST_B = 'b'.repeat(32), REQUEST_C = 'c'.repeat(32);

function refreshAck(state = 'pending', requestId = REQUEST_C, attempt = '2026-01-01T00:00:00Z') {
  return {state, request_id: requestId, last_attempt_at: attempt};
}

function refreshStatus(changes = {}) {
  return {schema_version: 1, last_attempt_at: '2026-01-01T00:00:00Z', last_success_at: '2026-01-01T00:00:01Z',
    result: 'success', latest_reading_at: {electricity: '2025-12-31T23:30:00Z', gas: null},
    request_state: null, error_category: null, request_id: REQUEST_A, ...changes};
}

for (const [left, right, expected] of [
  ['2026-01-01T00:00:00Z', '2026-01-01T00:00:00.000000Z', 0],
  ['2026-01-01T00:00:00.123001Z', '2026-01-01T00:00:00.123999Z', -1],
  ['2026-01-01T00:00:00Z', '2026-01-01T00:00:00.000001Z', -1],
  ['2026-01-01T00:00:00.123456Z', '2026-01-01T00:00:00.123456000000000000Z', 0],
  ['2026-01-01T00:00:00.123456000000000001Z', '2026-01-01T00:00:00.123456000000000002Z', -1],
  ['2025-12-31T23:59:59.999999Z', '2026-01-01T00:00:00Z', -1],
]) {
  assert.equal(call('compareUtcTimestamps', left, right), expected);
  assert.equal(call('compareUtcTimestamps', right, left), expected === 0 ? 0 : -expected);
}

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
for (const [start, count] of [['2025-03-30T00:00:00Z', 23], ['2025-10-25T23:00:00Z', 25], ['2025-11-01T00:00:00Z', 24]]) {
  const fixture = latestFixture(start, count);
  call('validateData', fixture);
  const groups = call('viewGroups', fixture, 'electricity', 'latest-day');
  assert.equal(groups.length, count);
  assert.equal(call('completePeriod', groups[0], 'latest-day', '2026-01-01'), true);
  assert.equal(call('completePeriod', groups[0], 'latest-day', fixture.latest_day.electricity.date), false);
  assert.equal(call('trendChange', groups, 'latest-day', '2026-01-01'), (count - (count - 1)) / (count - 1) * 100);
  if (count === 25) {
    assert.match(call('periodLabel', groups[1].key, 'latest-day', true), /01:00 BST/);
    assert.match(call('periodLabel', groups[2].key, 'latest-day', true), /01:00 GMT/);
    assert.match(call('periodRangeLabel', groups[1].key, 'latest-day'), /01:00 BST → .*01:00 GMT/);
  }
}
for (const mutate of [
  fixture => { fixture.latest_day.electricity.complete = false; },
  fixture => { fixture.latest_day.electricity.hours.pop(); },
  fixture => { fixture.latest_day.electricity.hours[1] = fixture.latest_day.electricity.hours[0]; },
  fixture => { fixture.latest_day.electricity.hours[0].cost = 'synthetic-private-diagnostic'; },
  fixture => { fixture.latest_day.electricity.hours[0].start = '2025-11-01T00:00:00'; },
  fixture => { fixture.latest_day.electricity.cost_basis = 'includes_standing_charge'; },
  fixture => { fixture.latest_day.electricity.date = '2025-11-02'; },
]) {
  const fixture = latestFixture(); mutate(fixture);
  assert.throws(() => call('validateData', fixture));
}
assert.equal(call('validateRefreshStatus', refreshStatus()).result, 'success');
for (const change of [{schema_version: 2}, {result: 'synthetic-private-diagnostic'}, {request_state: 'unknown'},
  {error_category: 'raw-error'}, {last_attempt_at: '2026-02-30T00:00:00Z'},
  {last_success_at: '2026-01-01T00:00:00+00:00'}, {latest_reading_at: []}, {latest_reading_at: {electricity: null}},
  {request_id: 'not-a-request'}, {request_id: 'A'.repeat(32)}]) {
  assert.throws(() => call('validateRefreshStatus', refreshStatus(change)));
}
const legacyStatus = refreshStatus(); delete legacyStatus.request_id;
assert.equal(call('validateRefreshStatus', legacyStatus).request_id, null);

function element(dataset = {}) {
  return {dataset, textContent: '', innerHTML: '', disabled: false, hidden: true, attributes: {}, style: {},
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getBoundingClientRect() { return {left: 100, top: 50, width: 360, height: 180}; },
    classList: {toggle() {}}};
}

function browserHarness(fetchImpl, protocol = 'https:') {
  const ids = Object.fromEntries(['status', 'refresh-status', 'refresh', 'updated', 'cards', 'chart', 'title', 'range', 'action', 'notice', 'chart-tooltip', 'chart-stage', 'chart-scroll'].map(id => ['#' + id, element()]));
  const fuels = ['electricity', 'gas'].map(fuel => element({fuel}));
  const periods = ['day', 'week', 'month', 'year', 'latest-day'].map(period => element({period}));
  const listeners = {}, timers = new Map(), timerDurations = new Map(), intervals = [];
  const clock = {now: Date.now()};
  class HarnessDate extends Date { static now() { return clock.now; } }
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
    document, window, ResizeObserver, location: {protocol}, URL, AbortController, Intl, Date: HarnessDate,
    fetch: (...args) => { requests.push(args); return fetchImpl(...args); },
    setTimeout: (callback, duration) => { const id = ++timerId; timers.set(id, callback); timerDurations.set(id, duration); return id; },
    clearTimeout: id => { timers.delete(id); timerDurations.delete(id); },
    setInterval: (callback, duration) => { intervals.push({callback, duration}); },
  });
  vm.runInContext(source, context);
  return {ids, fuels, periods, listeners, timers, intervals, requests, document, window, windowListeners, viewportListeners, resizeObservers, clock,
    reload: async () => { listeners.visibilitychange(); await flush(); },
    tick: async (duration = 2000) => {
      const id = [...timers.keys()].find(id => timerDurations.get(id) === duration);
      assert.notEqual(id, undefined, 'expected a pending timer');
      const callback = timers.get(id); timers.delete(id); timerDurations.delete(id);
      await callback(); await flush();
    },
    get points() { return pointElements; }, get vectors() { return vectors; }};
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
  await page.reload();
  assert.equal(page.ids['#status'].dataset.state, 'error');
  assert.match(page.ids['#status'].textContent, /last loaded snapshot/);
  page.fuels[1].onclick();
  assert.equal(page.ids['#status'].dataset.state, 'error', 'filter changes must not hide a failed reload');
  page.fuels[0].onclick();
  const stale = data();
  stale.latest_reading_at.electricity = '2020-01-01T00:00:00Z';
  next = async () => response(stale);
  await page.reload();
  assert.match(page.ids['#status'].textContent, /older than 48 hours/);
  assert.match(page.ids['#action'].textContent, /Refresh and check/);
  const missingCost = data();
  missingCost.daily.at(-1).cost = null;
  next = async () => response(missingCost);
  page.periods[0].onclick();
  await page.reload();
  assert.match(page.ids['#cards'].innerHTML, /Cost unavailable/);
  page.points.at(-1).focus();
  assert.match(tooltip.innerHTML, /Cost unavailable/);
  assert.doesNotMatch(tooltip.innerHTML, /£0\.00/);
  const confirmedGas = data();
  confirmedGas.latest_reading_at.gas = new Date().toISOString();
  confirmedGas.daily.push({date: '2026-11-02', fuel: 'gas', kwh: 4, cost: 1});
  confirmedGas.hourly.push(...Array.from({length: 24}, (_, hour) => ({fuel: 'gas', hour, kwh: hour === 5 ? 3 : 0.2})));
  next = async () => response(confirmedGas);
  await page.reload();
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
  await page.reload();
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
  await page.reload();
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

  for (const [start, count] of [['2025-03-30T00:00:00Z', 23], ['2025-10-25T23:00:00Z', 25], ['2025-11-01T00:00:00Z', 24]]) {
    const fixture = latestFixture(start, count);
    const latestPage = browserHarness(async () => response(fixture));
    await flush();
    latestPage.periods[4].onclick();
    assert.equal(latestPage.points.length, count);
    assert.match(latestPage.ids['#chart'].innerHTML, /Time \(Europe\/London\)/);
    assert.match(latestPage.ids['#range'].textContent, /Latest available day:/);
    assert.match(latestPage.ids['#range'].textContent, /2025/);
    assert.ok(latestPage.ids['#range'].textContent.includes(count + ' hourly periods'));
    assert.match(latestPage.ids['#range'].textContent, /Usage cost excludes standing charge/);
    latestPage.points.at(-1).focus();
    const latestTooltip = latestPage.ids['#chart-tooltip'];
    assert.match(latestTooltip.innerHTML, /Cost unavailable/);
    assert.match(latestTooltip.innerHTML, /Usage cost \(standing charge excluded\)/);
    assert.doesNotMatch(latestTooltip.innerHTML, /£0\.00/);
    latestPage.ids['#chart'].onkeydown({key: 'Home', preventDefault() {}});
    assert.equal(latestPage.document.activeElement, latestPage.points[0]);
    assert.equal(latestPage.points.filter(point => point.attributes.tabindex === '0').length, 1);
    latestPage.ids['#chart'].onkeydown({key: 'Escape', preventDefault() {}});
    assert.equal(latestTooltip.hidden, true);
    if (count === 25) {
      latestPage.points[1].focus(); assert.match(latestTooltip.innerHTML, /01:00 BST/);
      latestPage.points[2].focus(); assert.match(latestTooltip.innerHTML, /01:00 GMT/);
    }
    latestPage.fuels[1].onclick();
    assert.equal(latestPage.points.length, 0);
    assert.match(latestPage.ids['#status'].textContent, /No complete London day/);
  }

  const baseline = refreshStatus();
  const running = refreshStatus({last_attempt_at: '2026-01-01T01:00:00Z', result: 'running', request_state: 'running', request_id: REQUEST_C});
  const completed = refreshStatus({last_attempt_at: running.last_attempt_at, last_success_at: '2026-01-01T01:00:01Z', request_id: REQUEST_C});
  let published = data(), dataLoads = 0;
  const statuses = [baseline, baseline, {...baseline, request_state: 'pending'}, running, completed];
  const manual = browserHarness(async (url, options) => {
    if (url.pathname.endsWith('data.json')) { dataLoads++; return response(published); }
    if (url.pathname.endsWith('status.json')) return response(statuses.shift());
    assert.equal(url.pathname, '/energy-usage/refresh.php');
    assert.equal(options.method, 'POST'); assert.equal(options.headers['X-Energy-Refresh'], '1');
    assert.equal(options.cache, 'no-store'); assert.equal(options.credentials, 'same-origin'); assert.equal(options.redirect, 'error');
    return {status: 202, ...response(refreshAck())};
  });
  await flush();
  await manual.ids['#refresh'].onclick();
  assert.equal(manual.ids['#refresh'].disabled, true);
  assert.match(manual.ids['#refresh-status'].textContent, /Requested/);
  assert.equal(dataLoads, 1, 'old terminal status must not reload or complete this request');
  const requestCount = manual.requests.length;
  await manual.ids['#refresh'].onclick();
  assert.equal(manual.requests.length, requestCount, 'repeated clicks do not submit overlapping requests');
  await manual.tick();
  assert.match(manual.ids['#refresh-status'].textContent, /Requested/);
  await manual.tick();
  assert.match(manual.ids['#refresh-status'].textContent, /Fetching from Octopus/);
  assert.equal(dataLoads, 1);
  published = data();
  published.latest_reading_at.electricity = new Date(Date.now() + 1000).toISOString();
  published.latest_reading_at.gas = new Date(Date.now() + 1000).toISOString();
  await manual.tick();
  assert.equal(dataLoads, 2, 'reload only when last_success_at advances');
  assert.equal(manual.ids['#refresh'].disabled, false);
  assert.match(manual.ids['#refresh-status'].textContent, /Updated/);
  assert.match(manual.ids['#updated'].textContent, /Data as of.*Latest electricity reading.*Latest gas reading.*Last successful sync/);
  manual.intervals[0].callback(); await flush();
  assert.equal(dataLoads, 3, 'passive snapshot reload remains separate');
  assert.equal(manual.requests.filter(([, options]) => options.method === 'POST').length, 1);
  assert.equal(manual.timers.size, 0);

  for (const terminal of ['failed', 'partial']) {
    let loads = 0;
    const states = terminal === 'failed' ? [baseline, baseline, running, {...running, result: terminal, request_state: null, error_category: 'sync_failed'}] :
      [running, {...running, result: terminal, request_state: null, error_category: 'provider_error'}];
    const failed = browserHarness(async url => {
      if (url.pathname.endsWith('data.json')) { loads++; return response(data()); }
      if (url.pathname.endsWith('status.json')) return response(states.shift());
      return {status: 202, ...response(refreshAck('already_running', REQUEST_C, running.last_attempt_at))};
    });
    await flush(); await failed.ids['#refresh'].onclick();
    if (terminal === 'failed') {
      assert.equal(failed.ids['#refresh'].disabled, true, 'already-running reply cannot reuse stale terminal status');
      await failed.tick(); await failed.tick();
    }
    assert.equal(loads, 1, 'failed and partial fetches retain the published snapshot');
    assert.equal(failed.ids['#refresh'].disabled, false);
    assert.equal(failed.ids['#refresh-status'].dataset.state, 'error');
    assert.match(failed.ids['#refresh-status'].textContent, /Old data is kept/);
    assert.doesNotMatch(failed.ids['#refresh-status'].textContent, /provider_error|sync_failed/);
  }

  async function refreshScenario(before, polled, reply = {state: 'pending'}, rawReply = false) {
    const stable = data(), newer = JSON.parse(JSON.stringify(stable));
    newer.latest_reading_at.electricity = new Date(Date.now() + 1000).toISOString();
    let loads = 0, statusReads = 0;
    const states = [before, ...polled];
    const id = reply.state === 'pending' ? before.request_id === REQUEST_C ? REQUEST_B : REQUEST_C : before.request_id;
    const acknowledgement = rawReply ? reply : {...refreshAck(reply.state, id, before.last_attempt_at), ...reply};
    const page = browserHarness(async url => {
      if (url.pathname.endsWith('data.json')) return response(++loads === 1 ? stable : newer);
      if (url.pathname.endsWith('status.json')) return response(states[Math.min(statusReads++, states.length - 1)]);
      return {status: 202, ...response(acknowledgement)};
    });
    await flush(); await page.ids['#refresh'].onclick();
    return {page, get loads() { return loads; }};
  }

  async function reusedIdAttemptFloor() {
    const stamps = [
      {name: 'whole seconds', old: '2026-01-01T01:00:00Z', equal: '2026-01-01T01:00:01Z', ack: '2026-01-01T01:00:01Z', newer: '2026-01-01T01:00:02Z'},
      {name: 'sub-millisecond', old: '2026-01-01T01:00:00.123001Z', equal: '2026-01-01T01:00:00.123500Z', ack: '2026-01-01T01:00:00.123500Z', newer: '2026-01-01T01:00:00.123999Z'},
      {name: 'legacy ACK', old: '2026-01-01T01:00:00.999999Z', equal: '2026-01-01T01:00:01.000000Z', ack: '2026-01-01T01:00:01Z', newer: '2026-01-01T01:00:01.000001Z'},
      {name: 'legacy status', old: '2026-01-01T01:00:00.999999Z', equal: '2026-01-01T01:00:01Z', ack: '2026-01-01T01:00:01.000000Z', newer: '2026-01-01T01:00:01.000001Z'},
    ];
    for (const state of ['already_running', 'retry_queued']) {
      for (const result of ['success', 'no_data', 'partial', 'failed']) {
        for (const times of stamps) {
          for (const position of ['old', 'equal', 'newer']) {
            const label = `reused-ID attempt-floor: ${state}/${result}/${times.name}/${position}`;
            const before = refreshStatus({request_id: REQUEST_A, last_attempt_at: times.old, result: 'running', request_state: 'running'});
            const terminal = {...before, result, request_state: 'pending', last_attempt_at: times[position],
              last_success_at: result === 'success' ? times[position] : before.last_success_at,
              error_category: result === 'failed' ? 'sync_failed' : null};
            const active = {...terminal, result: 'running', request_state: 'running', last_attempt_at: times.ack,
              last_success_at: position === 'old' && result === 'success' ? times.old : before.last_success_at};
            const accepted = {...terminal, last_attempt_at: position === 'old' ? times.ack : times[position]};
            if (result === 'success') accepted.last_success_at = accepted.last_attempt_at;
            const polled = position === 'old' ? [terminal, terminal, active, accepted] : [
              {...active, last_attempt_at: times[position]}, accepted,
            ];
            const scenario = await refreshScenario(before, polled, refreshAck(state, REQUEST_A, times.ack));
            const {page} = scenario;
            if (position === 'old') {
              for (let repeat = 0; repeat < 2; repeat++) {
                assert.equal(page.ids['#refresh'].disabled, true, label + ' must ignore the older terminal attempt');
                assert.match(page.ids['#refresh-status'].textContent, /Waiting/, label);
                assert.doesNotMatch(page.ids['#refresh-status'].textContent, /Updated|Previous attempt/, label);
                await page.tick();
              }
            }
            assert.equal(page.ids['#refresh'].disabled, true, label + ' running is not terminal');
            assert.match(page.ids['#refresh-status'].textContent, /Fetching from Octopus/, label);
            await page.tick();
            assert.equal(page.ids['#refresh'].disabled, false, label + ' must finish the acknowledged or newer attempt');
            assert.equal(page.timers.size, 0, label);
            assert.match(page.ids['#refresh-status'].textContent, result === 'success' ? /Updated.*Another update is queued/ :
              result === 'no_data' ? /No newer readings.*A retry is queued/ : /Old data is kept.*A retry is queued/, label);
            const previous = state === 'retry_queued' && position !== 'newer' && result !== 'success';
            assert.equal(page.ids['#refresh-status'].textContent.startsWith('Previous attempt:'), previous, label);
            assert.equal(scenario.loads, result === 'success' ? position === 'old' ? 3 : 2 : 1, label);
          }
        }
      }
    }
  }
  await reusedIdAttemptFloor();

  const successFloor = refreshStatus({last_success_at: '2026-01-01T00:00:01Z'});
  const successStatus = stamp => ({...successFloor, result: 'running', request_state: 'running',
    request_id: REQUEST_B, last_success_at: stamp});
  const successOrdering = await refreshScenario(successFloor, [
    successStatus('2026-01-01T00:00:01.000000Z'),
    successStatus('2026-01-01T00:00:00.999999Z'),
    successStatus('2026-01-01T00:00:01.000001Z'),
    successStatus('2026-01-01T00:00:01.000001Z'),
  ]);
  assert.equal(successOrdering.loads, 1, 'equivalent success timestamp representations do not reload');
  await successOrdering.page.tick();
  assert.equal(successOrdering.loads, 1, 'an older success publication does not reload');
  await successOrdering.page.tick();
  assert.equal(successOrdering.loads, 2, 'a microsecond success advance reloads the published snapshot');
  await successOrdering.page.tick();
  assert.equal(successOrdering.loads, 2, 'a repeated success publication reloads only once');
  assert.equal(successOrdering.page.ids['#refresh'].disabled, true, 'publication progress does not claim another request ID');
  for (const state of ['already_running', 'retry_queued']) {
    for (const [ack, stamp] of [
      ['2026-01-01T01:00:00Z', '2026-01-01T01:00:00.000000Z'],
      ['2026-01-01T01:00:00.000000Z', '2026-01-01T01:00:00Z'],
    ]) {
      const legacy = refreshStatus({request_id: null, last_attempt_at: stamp, result: 'no_data', request_state: 'pending'});
      const equivalent = await refreshScenario(legacy, [legacy], refreshAck(state, null, ack));
      assert.equal(equivalent.page.ids['#refresh'].disabled, false, 'legacy joins compare exact instants, not timestamp formatting');
      assert.match(equivalent.page.ids['#refresh-status'].textContent, /No newer readings.*A retry is queued/);
      assert.equal(equivalent.page.ids['#refresh-status'].textContent.startsWith('Previous attempt:'), state === 'retry_queued');
    }
  }

  for (const result of ['no_data', 'partial', 'failed', 'success']) {
    for (const mode of ['fast-newer-attempt', 'joined-running', 'observed-running-same-stamp']) {
      const before = mode === 'joined-running' ? running : baseline;
      const attempt = mode === 'observed-running-same-stamp' ? before.last_attempt_at : running.last_attempt_at;
      const terminal = refreshStatus({last_attempt_at: attempt, result, request_state: 'pending',
        last_success_at: result === 'success' ? completed.last_success_at : before.last_success_at,
        error_category: result === 'failed' ? 'sync_failed' : null, request_id: REQUEST_C});
      const polled = mode === 'observed-running-same-stamp' ? [{...running, last_attempt_at: attempt}, terminal] : [terminal];
      const scenario = await refreshScenario(before, polled, {state: mode === 'joined-running' ? 'already_running' : 'pending'});
      const page = scenario.page;
      if (mode === 'observed-running-same-stamp') {
        assert.equal(page.ids['#refresh'].disabled, true);
        assert.match(page.ids['#refresh-status'].textContent, /Fetching from Octopus/);
        await page.tick();
      }
      assert.equal(page.ids['#refresh'].disabled, false, `${result}/${mode} must finish despite a queued successor`);
      assert.equal(page.timers.size, 0);
      assert.equal(page.ids['#refresh-status'].dataset.state, ['partial', 'failed'].includes(result) ? 'error' : 'ready');
      assert.match(page.ids['#refresh-status'].textContent, result === 'success' ? /Updated.*Another update is queued/ :
        result === 'no_data' ? /No newer readings.*A retry is queued/ : /Old data is kept.*A retry is queued/);
      assert.doesNotMatch(page.ids['#refresh-status'].textContent, /Previous attempt|Waiting for the background runner/);
      assert.equal(scenario.loads, result === 'success' ? 2 : 1, 'failed, partial and no-data attempts must retain the snapshot');
      assert.equal(terminal.request_state, 'pending', 'finishing the UI must not consume the queued successor');
      assert.equal(page.requests.filter(([, options]) => options.method === 'POST').length, 1);
    }
    const old = refreshStatus({result, request_state: 'pending', error_category: result === 'failed' ? 'sync_failed' : null});
    const waiting = await refreshScenario(old, [old]);
    for (let count = 0; count < 4; count++) {
      assert.equal(waiting.page.ids['#refresh'].disabled, true, 'pending alone cannot make an unchanged terminal baseline current');
      assert.match(waiting.page.ids['#refresh-status'].textContent, /Requested.*Waiting/);
      assert.equal(waiting.loads, 1);
      await waiting.page.tick();
    }
  }
  for (const result of ['no_data', 'partial', 'failed']) {
    const retained = refreshStatus({result, request_state: 'pending', error_category: result === 'failed' ? 'sync_failed' : null});
    const joined = await refreshScenario(retained, [retained], {state: 'retry_queued', last_attempt_at: retained.last_attempt_at});
    assert.equal(joined.page.ids['#refresh'].disabled, false);
    assert.equal(joined.page.timers.size, 0);
    assert.match(joined.page.ids['#refresh-status'].textContent, /^Previous attempt:.*A retry is queued/);
    assert.equal(joined.loads, 1);
  }
  const staleFailure = refreshStatus({result: 'failed', request_state: 'pending', error_category: 'sync_failed'});
  const mismatchedRetry = await refreshScenario(staleFailure, [staleFailure], {state: 'retry_queued', last_attempt_at: running.last_attempt_at});
  await mismatchedRetry.page.tick();
  assert.equal(mismatchedRetry.page.ids['#refresh'].disabled, true, 'a queued retry acknowledgement cannot complete a different stale attempt');
  assert.match(mismatchedRetry.page.ids['#refresh-status'].textContent, /Waiting/);
  for (const stamp of [undefined, 'synthetic-private-diagnostic', '2026-01-01T00:00:00+00:00']) {
    const invalidRetry = await refreshScenario(staleFailure, [staleFailure], {state: 'retry_queued', last_attempt_at: stamp});
    assert.equal(invalidRetry.page.ids['#refresh-status'].dataset.state, 'error');
    assert.equal(invalidRetry.page.ids['#refresh'].disabled, false);
    assert.doesNotMatch(invalidRetry.page.ids['#refresh-status'].textContent, /synthetic-private-diagnostic/);
  }
  const failedPending = {...running, result: 'failed', request_state: 'pending', error_category: 'sync_failed'};
  const newlyAcknowledged = await refreshScenario(baseline, [failedPending], {
    state: 'retry_queued', last_attempt_at: failedPending.last_attempt_at, request_id: failedPending.request_id});
  assert.equal(newlyAcknowledged.page.ids['#refresh'].disabled, false, 'the retry acknowledgement must match fresh status, not require the old pre-POST baseline');
  assert.match(newlyAcknowledged.page.ids['#refresh-status'].textContent, /^Previous attempt:/);
  assert.match(newlyAcknowledged.page.ids['#refresh-status'].textContent, /Old data is kept.*A retry is queued/);
  assert.equal(newlyAcknowledged.loads, 1);
  const priority = await refreshScenario(baseline, [{...failedPending, request_state: 'running'}, failedPending]);
  assert.equal(priority.page.ids['#refresh'].disabled, true, 'explicit active running state retains precedence');
  assert.match(priority.page.ids['#refresh-status'].textContent, /Fetching/);
  await priority.page.tick();
  assert.match(priority.page.ids['#refresh-status'].textContent, /Old data is kept.*A retry is queued/);
  const resumedRetry = await refreshScenario(staleFailure, [
    {...running, request_id: staleFailure.request_id}, {...failedPending, request_id: staleFailure.request_id},
  ], {state: 'retry_queued', last_attempt_at: staleFailure.last_attempt_at});
  assert.equal(resumedRetry.page.ids['#refresh'].disabled, true, 'a retry that has already started must show running, not the previous outcome');
  assert.match(resumedRetry.page.ids['#refresh-status'].textContent, /Fetching/);
  await resumedRetry.page.tick();
  assert.equal(resumedRetry.page.ids['#refresh'].disabled, false);
  assert.doesNotMatch(resumedRetry.page.ids['#refresh-status'].textContent, /Previous attempt/);
  const successOnly = {...baseline, last_success_at: completed.last_success_at, request_state: 'pending'};
  const noAttempt = await refreshScenario(baseline, [successOnly]);
  await noAttempt.page.tick();
  assert.equal(noAttempt.page.ids['#refresh'].disabled, true, 'a publication change alone cannot complete a newly queued attempt');
  assert.equal(noAttempt.loads, 2, 'a newer published snapshot still reloads only once');
  const olderAttempt = await refreshScenario(completed, [{...failedPending, last_attempt_at: baseline.last_attempt_at}]);
  await olderAttempt.page.tick();
  assert.equal(olderAttempt.page.ids['#refresh'].disabled, true, 'an older terminal attempt is not genuine progress');

  const completedB = {...completed, request_id: REQUEST_B};
  const runningC = {...running, last_attempt_at: '2026-01-01T02:00:00Z', last_success_at: completedB.last_success_at, request_id: REQUEST_C};
  const completedC = {...completed, last_attempt_at: runningC.last_attempt_at, last_success_at: '2026-01-01T02:00:01Z', request_id: REQUEST_C};
  const suspended = await refreshScenario(baseline, [
    completedB, completedB, {...runningC, request_id: null}, runningC, completedC,
  ], refreshAck('pending', REQUEST_C, completedB.last_attempt_at));
  assert.equal(suspended.page.ids['#refresh'].disabled, true, 'completed B cannot satisfy the C flag created after a suspended tab resumes');
  assert.match(suspended.page.ids['#refresh-status'].textContent, /Requested.*Waiting/);
  await suspended.page.tick();
  assert.equal(suspended.page.ids['#refresh'].disabled, true);
  await suspended.page.tick();
  assert.match(suspended.page.ids['#refresh-status'].textContent, /Another update is running.*still queued/);
  await suspended.page.tick();
  assert.match(suspended.page.ids['#refresh-status'].textContent, /Fetching from Octopus/);
  await suspended.page.tick();
  assert.match(suspended.page.ids['#refresh-status'].textContent, /^Updated/);
  assert.equal(suspended.page.ids['#refresh'].disabled, false);
  assert.equal(suspended.loads, 3, 'published B may reload globally, but only matching C completes this request');

  const overtaken = await refreshScenario(baseline, [
    {...running, request_id: null}, {...completedB, request_id: null}, completedC,
  ], refreshAck('pending', REQUEST_C, baseline.last_attempt_at));
  assert.equal(overtaken.page.ids['#refresh'].disabled, true);
  assert.match(overtaken.page.ids['#refresh-status'].textContent, /Another update is running.*still queued/);
  await overtaken.page.tick();
  assert.equal(overtaken.page.ids['#refresh'].disabled, true, 'an attempt that overtook the pre-creation status read and missed C cannot satisfy C');
  assert.doesNotMatch(overtaken.page.ids['#refresh-status'].textContent, /Updated/);
  await overtaken.page.tick();
  assert.match(overtaken.page.ids['#refresh-status'].textContent, /^Updated/);
  for (const result of ['success', 'no_data', 'partial', 'failed']) {
    const fast = {...completedC, result, request_state: 'pending',
      last_success_at: result === 'success' ? completedC.last_success_at : baseline.last_success_at};
    const afterCreation = await refreshScenario(baseline, [fast], refreshAck('pending', REQUEST_C, fast.last_attempt_at));
    assert.equal(afterCreation.page.ids['#refresh'].disabled, false, 'matching C can finish before the post-creation watermark is read');
    assert.match(afterCreation.page.ids['#refresh-status'].textContent, result === 'success' ? /Updated.*Another update is queued/ :
      result === 'no_data' ? /No newer readings.*A retry is queued/ : /Old data is kept.*A retry is queued/);
    assert.equal(afterCreation.page.timers.size, 0);
  }
  const retainedA = {...staleFailure, request_id: REQUEST_A};
  const runningA = {...running, request_id: REQUEST_A};
  const laterRunningA = {...runningC, request_id: REQUEST_A};
  const priorityA = await refreshScenario(baseline, [
    runningA, {...retainedA, last_attempt_at: runningA.last_attempt_at}, laterRunningA,
    {...completedC, request_id: REQUEST_A, request_state: 'pending'},
    {...runningC, request_id: REQUEST_B, last_attempt_at: '2026-01-01T03:00:00Z'},
    {...completedC, request_id: REQUEST_B, last_attempt_at: '2026-01-01T03:00:00Z', last_success_at: '2026-01-01T03:00:01Z'},
  ], refreshAck('pending', REQUEST_B, retainedA.last_attempt_at));
  for (let count = 0; count < 4; count++) {
    assert.equal(priorityA.page.ids['#refresh'].disabled, true, 'B must keep waiting through retained A retries and terminal outcomes');
    assert.doesNotMatch(priorityA.page.ids['#refresh-status'].textContent, /Updated|Previous attempt/);
    await priorityA.page.tick();
  }
  assert.match(priorityA.page.ids['#refresh-status'].textContent, /Fetching from Octopus/);
  await priorityA.page.tick();
  assert.match(priorityA.page.ids['#refresh-status'].textContent, /^Updated/);
  assert.equal(priorityA.page.ids['#refresh'].disabled, false);

  for (const reply of [
    {state: 'pending', request_id: REQUEST_C},
    {state: 'pending', last_attempt_at: null},
    refreshAck('pending', REQUEST_C, 'synthetic-private-diagnostic'),
    refreshAck('pending', REQUEST_C, '2026-01-01T00:00:00+00:00'),
    refreshAck('pending', 'invalid', null),
    refreshAck('pending', 'A'.repeat(32), null),
    refreshAck('pending', REQUEST_C + '\n', null),
    refreshAck('already_running', null, null),
    refreshAck('retry_queued', REQUEST_C, null),
  ]) {
    const invalidAck = await refreshScenario(baseline, [completedC], reply, true);
    assert.equal(invalidAck.page.ids['#refresh-status'].dataset.state, 'error');
    assert.equal(invalidAck.page.ids['#refresh'].disabled, false);
    assert.equal(invalidAck.page.timers.size, 0);
    assert.doesNotMatch(invalidAck.page.ids['#refresh-status'].textContent, /Updated|synthetic-private-diagnostic/);
  }
  const legacyQueue = await refreshScenario(baseline, [completedB], refreshAck('pending', null, completedB.last_attempt_at));
  assert.equal(legacyQueue.page.ids['#refresh'].disabled, false);
  assert.match(legacyQueue.page.ids['#refresh-status'].textContent, /outcome cannot be tracked/);
  assert.equal(legacyQueue.page.timers.size, 0);
  for (const result of ['no_data', 'partial', 'failed']) {
    const legacy = {...staleFailure, result, request_id: null};
    const retainedLegacy = await refreshScenario(legacy, [legacy], refreshAck('retry_queued', null, legacy.last_attempt_at));
    assert.equal(retainedLegacy.page.ids['#refresh'].disabled, false);
    assert.match(retainedLegacy.page.ids['#refresh-status'].textContent, /^Previous attempt:.*A retry is queued/);
    assert.doesNotMatch(retainedLegacy.page.ids['#refresh-status'].textContent, /Updated/);
  }
  const scheduledJoin = await refreshScenario(baseline, [
    {...baseline, request_id: null}, {...completedB, request_id: null},
  ], refreshAck('already_running', null, completedB.last_attempt_at));
  assert.equal(scheduledJoin.page.ids['#refresh'].disabled, true, 'an active scheduled join is bound to its server ACK, not preflight A');
  await scheduledJoin.page.tick();
  assert.equal(scheduledJoin.page.ids['#refresh'].disabled, false);
  assert.match(scheduledJoin.page.ids['#refresh-status'].textContent, /^Updated/);
  const missingStatusId = {...completedC}; delete missingStatusId.request_id;
  const unclaimed = await refreshScenario(baseline, [missingStatusId], refreshAck('pending', REQUEST_C, null));
  await unclaimed.page.tick();
  assert.equal(unclaimed.page.ids['#refresh'].disabled, true, 'legacy/pre-claim status cannot claim the new request ID');
  assert.doesNotMatch(unclaimed.page.ids['#refresh-status'].textContent, /Updated/);

  for (const result of ['no_data', 'success']) {
    let loads = 0;
    const stable = data();
    const end = {...completed, result, last_success_at: result === 'no_data' ? baseline.last_success_at : completed.last_success_at};
    const states = [baseline, end];
    const lag = browserHarness(async url => {
      if (url.pathname.endsWith('data.json')) {
        loads++;
        const snapshot = JSON.parse(JSON.stringify(stable));
        if (loads > 1) snapshot.latest_reading_at.electricity = snapshot.latest_reading_at.electricity.replace(/Z$/, '+00:00');
        return response(snapshot);
      }
      if (url.pathname.endsWith('status.json')) return response(states.shift());
      return {status: 202, ...response(refreshAck())};
    });
    await flush(); await lag.ids['#refresh'].onclick();
    assert.equal(loads, result === 'no_data' ? 1 : 2);
    assert.match(lag.ids['#refresh-status'].textContent, /No newer readings from Octopus yet/);
    assert.match(lag.ids['#refresh-status'].textContent, /1–2 day/);
    assert.equal(lag.ids['#refresh-status'].dataset.state, 'ready');
  }

  for (const [code, state, text] of [[429, 'rate_limited', /Rate limited.*300 seconds/],
    [503, 'auth_unavailable', /Protected refresh authentication is unavailable/], [503, 'unavailable', /could not be requested/]]) {
    const rejected = browserHarness(async url => {
      if (url.pathname.endsWith('data.json')) return response(data());
      if (url.pathname.endsWith('status.json')) return response(baseline);
      return {status: code, ok: false, json: async () => ({state, retry_after: 600, detail: 'synthetic-private-diagnostic'})};
    });
    await flush(); await rejected.ids['#refresh'].onclick();
    assert.match(rejected.ids['#refresh-status'].textContent, text);
    assert.doesNotMatch(rejected.ids['#refresh-status'].textContent, /synthetic-private-diagnostic/);
    assert.equal(rejected.ids['#refresh'].disabled, false);
  }
  const invalidStatus = browserHarness(async url => url.pathname.endsWith('data.json') ? response(data()) : response(refreshStatus({result: 'synthetic-private-diagnostic'})));
  await flush(); await invalidStatus.ids['#refresh'].onclick();
  assert.equal(invalidStatus.requests.filter(([, options]) => options.method === 'POST').length, 0, 'status baseline must validate before POST');
  assert.doesNotMatch(invalidStatus.ids['#refresh-status'].textContent, /synthetic-private-diagnostic/);

  let recoveryLoads = 0;
  const recoveryStates = [baseline, completed];
  const recovery = browserHarness(async url => {
    if (url.pathname.endsWith('data.json')) return ++recoveryLoads === 1 ? {ok: false} : response(data());
    if (url.pathname.endsWith('status.json')) return response(recoveryStates.shift());
    return {status: 202, ...response(refreshAck())};
  });
  await flush();
  assert.equal(recovery.ids['#status'].dataset.state, 'error');
  await recovery.ids['#refresh'].onclick();
  assert.equal(recoveryLoads, 2, 'a manual fetch can recover after an initial snapshot-load failure');
  assert.equal(recovery.ids['#refresh'].disabled, false);
  assert.match(recovery.ids['#refresh-status'].textContent, /Updated/);

  const timeoutPage = browserHarness(async url => {
    if (url.pathname.endsWith('data.json')) return response(data());
    if (url.pathname.endsWith('status.json')) return response(baseline);
    return {status: 202, ...response(refreshAck())};
  });
  await flush(); await timeoutPage.ids['#refresh'].onclick();
  timeoutPage.clock.now += 600000;
  await timeoutPage.tick();
  assert.match(timeoutPage.ids['#refresh-status'].textContent, /Unable to confirm.*queued or delayed/);
  assert.equal(timeoutPage.ids['#refresh'].disabled, false);
  assert.equal(timeoutPage.timers.size, 0);
  const supersededStates = [running, {...running, result: 'failed', request_state: null, error_category: 'superseded_release'}];
  const superseded = browserHarness(async url => {
    if (url.pathname.endsWith('data.json')) return response(data());
    if (url.pathname.endsWith('status.json')) return response(supersededStates.shift());
    return {status: 202, ...response(refreshAck('already_running', REQUEST_C, running.last_attempt_at))};
  });
  await flush(); await superseded.ids['#refresh'].onclick();
  assert.match(superseded.ids['#refresh-status'].textContent, /newer update is publishing/i);
  assert.notEqual(superseded.ids['#refresh-status'].dataset.state, 'error');

  for (const hanging of ['baseline', 'post', 'poll']) {
    let statusRequests = 0;
    const hung = browserHarness((url, options) => {
      if (url.pathname.endsWith('data.json')) return Promise.resolve(response(data()));
      if (url.pathname.endsWith('status.json')) {
        statusRequests++;
        if ((hanging === 'baseline' && statusRequests === 1) || (hanging === 'poll' && statusRequests === 2))
          return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('synthetic-private-diagnostic'))));
        return Promise.resolve(response(baseline));
      }
      if (hanging === 'post') return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('synthetic-private-diagnostic'))));
      return Promise.resolve({status: 202, ...response(refreshAck())});
    });
    await flush();
    const request = hung.ids['#refresh'].onclick();
    await flush(); await hung.tick(10000); await request;
    assert.equal(hung.ids['#refresh'].disabled, false);
    assert.equal(hung.ids['#refresh-status'].dataset.state, 'error');
    assert.doesNotMatch(hung.ids['#refresh-status'].textContent, /synthetic-private-diagnostic/);
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
  console.log('Dashboard JavaScript checks passed: charts, London day/DST, keyboard/tap/hover, null costs, freshness, protected request IDs, both watermark races, retained retries, legacy requests, fail-closed ACKs and bounded polling.');
})().catch(error => { console.error(error); process.exitCode = 1; });
