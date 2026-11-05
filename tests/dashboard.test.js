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

function element(dataset = {}) {
  return {dataset, textContent: '', innerHTML: '', disabled: false, attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    classList: {toggle() {}}};
}

function browserHarness(fetchImpl, protocol = 'https:') {
  const ids = Object.fromEntries(['status', 'refresh', 'updated', 'cards', 'chart', 'title', 'range', 'action', 'notice'].map(id => ['#' + id, element()]));
  const fuels = ['electricity', 'gas'].map(fuel => element({fuel}));
  const periods = ['day', 'week', 'month', 'year'].map(period => element({period}));
  const listeners = {}, timers = new Map(), intervals = [];
  const requests = [];
  const document = {
    baseURI: 'https://private.test/energy-usage/', visibilityState: 'visible',
    querySelector: selector => ids[selector],
    querySelectorAll: selector => selector === '[data-fuel]' ? fuels : periods,
    addEventListener: (event, callback) => { listeners[event] = callback; },
  };
  let timerId = 0;
  const context = vm.createContext({
    document, location: {protocol}, URL, AbortController, Intl,
    fetch: (...args) => { requests.push(args); return fetchImpl(...args); },
    setTimeout: callback => { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
    setInterval: (callback, duration) => { intervals.push({callback, duration}); },
  });
  vm.runInContext(source, context);
  return {ids, fuels, periods, listeners, timers, intervals, requests, document};
}

const response = fixture => ({ok: true, json: async () => fixture});
const flush = () => new Promise(resolve => setImmediate(resolve));

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
  const unverifiedGas = {...data(), daily: [unknown], hourly: []};
  unverifiedGas.latest_reading_at.gas = new Date().toISOString();
  next = async () => response(unverifiedGas);
  await page.ids['#refresh'].onclick();
  page.fuels[1].onclick();
  assert.match(page.ids['#status'].textContent, /source units are unverified/);
  assert.match(page.ids['#cards'].innerHTML, /Usage unavailable/);
  assert.match(page.ids['#cards'].innerHTML, /unverified gas units/);
  assert.doesNotMatch(page.ids['#cards'].innerHTML, /0\.0 kWh|£0\.00/);
  assert.equal(page.ids['#chart'].innerHTML, '');
  page.fuels[0].onclick();
  const empty = {...data(), daily: [], hourly: [], latest_reading_at: {}};
  next = async () => response(empty);
  await page.ids['#refresh'].onclick();
  assert.match(page.ids['#status'].textContent, /No recorded electricity data/);
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
  console.log('Dashboard JavaScript checks passed: grouping, freshness, validation, filters, reloads, errors, polling and timeout.');
})().catch(error => { console.error(error); process.exitCode = 1; });
