'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../command_centre/static/app.js'), 'utf8');

// Calendar identity must not depend on a browser's localized date punctuation.
const dates = vm.createContext({Intl: {DateTimeFormat: function(locale, options) {
  const real = new Intl.DateTimeFormat(locale, options);
  return {format: () => '11/24/2026', formatToParts: value => real.formatToParts(value)};
}}});
vm.runInContext(source.slice(source.indexOf('  const igDay ='), source.indexOf('  const igLink =')), dates);
assert.equal(vm.runInContext('igDay("2026-07-05T23:30:00Z")', dates), '2026-07-06');
assert.equal(vm.runInContext('igWeek("2026-07-05T23:30:00Z")', dates), '2026-07-06');
assert.equal(vm.runInContext('igWeek("2026-11-22T23:30:00Z")', dates), '2026-11-16');

// Takeaways cannot promote a sparse group, a Story live capture or missing results to a winner.
const insights = vm.createContext({});
vm.runInContext(source.slice(source.indexOf('  const igView ='), source.indexOf('  const igDate =')), insights);
assert.equal(vm.runInContext('igFormatCount("reel", 1)', insights), '1 reel');
assert.equal(vm.runInContext('igFormatCount("story", 1)', insights), '1 Story');
assert.equal(vm.runInContext('igFormatCount("post", 1)', insights), '1 post');
assert.equal(vm.runInContext('igFormatCount("post", 2)', insights), '2 posts');
insights.items = Array.from({length: 5}, () => ({format: 'reel', views: 100}));
insights.groups = [{format: 'reel', hour: '18', values: [100, 100, 100, 100, 100]}];
insights.learned = {not_working: [{feature: 'caption length', value: 'over 150 words'}]};
let next = vm.runInContext('igNextTest(items, p => p.views, groups, "Views", learned)', insights);
assert.match(next['Format to test'], /No clear format winner/);
assert.match(next['Posting hour'], /18:00 London/);
assert.match(next['Next reel'], /alternative to caption length: over 150 words/);
insights.items.push(...Array.from({length: 5}, () => ({format: 'post', views: 20})));
next = vm.runInContext('igNextTest(items, p => p.views, groups, "Views", learned)', insights);
assert.match(next['Format to test'], /Reel leads.*100, n=5/);
next = vm.runInContext('igNextTest([{format:"story", views:9999}], p => p.views, [], "Views", {})', insights);
assert.match(next['Format to test'], /No clear format winner/);
assert.match(next['Posting hour'], /No dependable hour/);
assert.match(next['Next Story'], /matched capture ages/);
next = vm.runInContext('igNextTest(items, p => p.views, [], "Views", learned)', insights);
assert.match(next['Posting hour'], /single content test below/);
assert.doesNotMatch(next['Posting hour'], /change only the hook/);
assert.match(next['Next reel'], /caption length/);
insights.learned.not_working.unshift({feature: 'format', value: 'story', text: 'Excluded format evidence'});
insights.learned.not_working[1].text = 'Caption length: over 150 words — 0.7x the median (3 posts, small sample)';
assert.equal(vm.runInContext('igContentFindings(learned).change.text', insights), insights.learned.not_working[1].text,
  'the evidence card and recommendation must select the same finding, including sample size');

// Test section routing without a browser. Real DOM interactions are covered by UI testing.
class Element {
  constructor(tag, attrs = {}, children = []) {
    this.tag = tag; this.attrs = attrs; this.children = []; this.events = {};
    this.hidden = Object.hasOwn(attrs, 'hidden');
    this.append(...children.flat(Infinity));
  }
  append(...children) {
    for (const child of children.filter(value => value !== null && value !== undefined && value !== false)) {
      if (child instanceof Element) child.parent = this;
      this.children.push(child);
    }
  }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute(name, value) { this.attrs[name] = value; }
  addEventListener(name, fn) { this.events[name] = fn; }
  find(id) { return this.attrs.id === id ? this : this.children.map(child => child instanceof Element ? child.find(id) : null).find(Boolean); }
}

async function dashboard(savedHolds = {}, permission = 'recommend', holdFailures = new Map()) {
  const renderedDrafts = [];
  const sentHolds = [];
  const renderCalls = [];
  const calendarCalls = [];
  const root = new Element('root');
  const data = {
    state: 'available', auto_publish: true, runs: [], token: {state: 'valid'},
    calendar: [{id: 1, pillar_name: '£3 soup test', slot: '2026-11-20T16:00:00+00:00', status: 'Idea', next_status: 'Scripted'}],
    drafts: [
      {draft_id: 'failed', status: 'failed', publish_error: 'outcome unknown; check Instagram'},
      {draft_id: 'scheduled', kind: 'reel', status: 'pending', review_score: 90, reel: {hold: false}},
      {draft_id: 'held', kind: 'reel', status: 'pending', review_score: 90, reel: {hold: true}},
      {draft_id: 'approved', status: 'approved', review_score: 90},
    ],
  };
  const context = vm.createContext({
    session: {permission}, load: async () => data, holdFailures, render: () => renderCalls.push(true), $: id => root.find(id),
    igView: {section: 'performance'}, igNumber: Number.isFinite,
    when: date => date, clearCache: () => {}, signInCount: 0,
    api: async (path, options) => { calendarCalls.push({path, ...options}); return {ok: true, status: 200}; },
    el: (tag, attrs, ...children) => new Element(tag, attrs, children),
    card: (title, ...children) => new Element('card', {title}, children), muted: text => text,
    tokenCard: () => new Element('token'), igPerformance: () => new Element('performance'),
    unconfirmedHold: id => savedHolds[id] || null,
    sendHold: (draft, body, token) => {
      sentHolds.push({id: draft.draft_id, body, token});
      return new Promise(() => {}); // one unanswered hold must never block another
    },
    draftCard: draft => { renderedDrafts.push(draft); return new Element('draft', {id: draft.draft_id}); },
  });
  vm.runInContext(source.slice(source.indexOf('  function holdFailureCards()'), source.indexOf('  const pendingHold =')), context);
  vm.runInContext(source.slice(source.indexOf('  const pendingHold ='), source.indexOf('  // Shared by dashboard recovery')), context);
  vm.runInContext(source.slice(source.indexOf('  function igCalendar('), source.indexOf('  const OUTCOME_LABELS')), context);
  context.root = root;
  await vm.runInContext('instagram(root)', context);
  return {root, context, renderedDrafts, sentHolds, renderCalls, calendarCalls};
}

// Exercise the real sender: retries share the same version and token, with no duplicate
// request when dashboard recovery and a lazy checkbox both observe the same saved hold.
async function sender() {
  const storage = new Map();
  const calls = [];
  let resolveFirst;
  const first = new Promise(resolve => { resolveFirst = resolve; });
  const context = vm.createContext({
    sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
    signInCount: 0, setTimeout: fn => fn(), DECISION_ERRORS: {},
    clearCache: () => {}, render: () => {}, showLogin: () => {}, $: () => null,
    api: async (path, options) => { calls.push({path, ...options}); return calls.length === 1 ? first : {ok: true, status: 200}; },
  });
  vm.runInContext(source.slice(source.indexOf('  const unconfirmedHold ='), source.indexOf('  function holdToggle(')), context);
  const expr = 'sendHold({draft_id:"a"}, {draft_id:"a", hold:true, version:7}, "same-token")';
  const a = vm.runInContext(expr, context);
  const b = vm.runInContext(expr, context);
  assert.equal(a, b, 'recovery and checkbox must share the in-flight request');
  assert.equal(calls.length, 1);
  assert.equal(JSON.parse(storage.get('ig-hold:a')).token, 'same-token');
  resolveFirst({ok: false, status: 0});
  await a;
  assert.equal(calls.length, 2);
  assert.equal(calls[1].body.version, 7);
  assert.equal(calls[1].body.hold, true);
  assert.equal(storage.size, 0, 'confirmed version is forgotten');

  // An old sign-in must not clear the next sign-in's saved operation.
  context.api = async () => { context.signInCount++; return {ok: false, status: 409}; };
  await vm.runInContext(expr, context);
  assert.equal(JSON.parse(storage.get('ig-hold:a')).token, 'same-token');

  context.api = async () => ({ok: false, status: 409, data: {error: 'hold_changed'}});
  await vm.runInContext(expr, context);
  assert.match(vm.runInContext('holdFailures.get("a").message', context), /Could not change the hold/);
  context.api = async () => ({ok: true, status: 200});
  await vm.runInContext('sendHold({draft_id:"b"}, {draft_id:"b", hold:true, version:0}, "b-token")', context);
  assert.equal(vm.runInContext('holdFailures.has("a")', context), true, 'another replay success cannot erase an unacknowledged failure');
}

(async () => {
  const normal = await dashboard();
  assert.equal(normal.renderedDrafts.length, 0, 'drafts should remain lazy on Performance');
  const panel = normal.root.find('ig-section-drafts');
  const nav = normal.root.children[0].children.find(node => node instanceof Element && node.attrs.class === 'ig-sections');
  nav.children.find(button => button.attrs['aria-controls'] === 'ig-section-drafts').events.click();
  assert.equal(panel.hidden, false);
  assert.ok(normal.renderedDrafts.some(draft => draft.draft_id === 'failed'), 'failed publishing drafts must not disappear');
  assert.ok(!normal.renderedDrafts.some(draft => draft.draft_id === 'held'), 'held drafts belong in review only');
  nav.children.find(button => button.attrs['aria-controls'] === 'ig-section-calendar').events.click();
  const calendar = normal.root.find('ig-section-calendar');
  assert.equal(calendar.hidden, false);
  const entry = calendar.children[0].children.find(child => child.attrs.title === '£3 soup test');
  const advance = entry.children.find(child => child instanceof Element && child.tag === 'button');
  assert.equal(advance.children[0], 'Mark Scripted');
  await advance.events.click();
  assert.equal(normal.calendarCalls[0].path, 'api/v1/instagram/calendar/advance');
  assert.equal(normal.calendarCalls[0].body.status, 'Idea');
  assert.equal(normal.renderCalls.length, 1);
  let loginShown = false;
  normal.context.showLogin = () => { loginShown = true; };
  normal.context.api = async () => {
    normal.context.signInCount++;
    return {ok: false, status: 401};
  };
  await advance.events.click();
  assert.equal(loginShown, false, 'an earlier calendar request cannot sign out a later session');
  assert.equal(normal.renderCalls.length, 1, 'a failed stale request must not re-render');
  let finishAdvance;
  normal.context.api = () => new Promise(resolve => { finishAdvance = resolve; });
  const refresh = [];
  normal.context.clearCache = () => refresh.push('clear cache');
  normal.context.render = () => refresh.push('render');
  const staleAdvance = advance.events.click();
  normal.context.signInCount++;
  finishAdvance({ok: true, status: 200});
  await staleAdvance;
  assert.deepEqual(refresh, ['clear cache', 'render'], 'refresh the new sign-in after a stale successful advance');
  normal.context.api = async () => ({ok: false, status: 401});
  await advance.events.click();
  assert.equal(loginShown, true, 'a 401 in the current session still goes to login');

  const saved = {
    held: {token: 'saved-release', body: {draft_id: 'held', hold: false, version: 2}},
    scheduled: {token: 'saved-hold', body: {draft_id: 'scheduled', hold: true, version: 0}},
  };
  const pending = await dashboard(saved);
  assert.equal(pending.context.igView.section, 'drafts', 'a reload must instantiate the saved hold retry immediately');
  assert.ok(pending.renderedDrafts.some(draft => draft.draft_id === 'scheduled'));
  assert.equal(pending.root.find('ig-section-performance').hidden, true);
  assert.deepEqual(pending.sentHolds.map(call => call.id).sort(), ['held', 'scheduled'], 'both cross-section holds must be sent, even if the first never answers');
  assert.equal(pending.sentHolds.find(call => call.id === 'held').body.version, 2);
  const readonly = await dashboard(saved, 'read');
  assert.equal(readonly.sentHolds.length, 0, 'read-only sessions cannot replay mutations');
  const invalid = await dashboard({scheduled: {token: 123, body: {draft_id: 'wrong', hold: 'yes'}}});
  assert.equal(invalid.sentHolds.length, 0, 'malformed saved bodies cannot be replayed');
  const failures = new Map([['scheduled', {topic:'Earlier reel', message:'The hold was changed elsewhere. Refresh to see it.'}]]);
  const recovered = await dashboard({}, 'recommend', failures);
  let notice = recovered.root.find('ig-hold-notices').children[0];
  assert.ok(notice, 'completed hold failures remain visible with no saved request');
  failures.set('scheduled', {topic:'Earlier reel', message:'A second failure'});
  vm.runInContext('refreshHoldNotices()', recovered.context);
  notice = recovered.root.find('ig-hold-notices').children[0];
  assert.match(notice.children[0].children[0].children[0], /A second failure/, 'new failures update the live region immediately');
  notice.children[0].children.find(node => node instanceof Element && node.tag === 'button').events.click();
  assert.equal(failures.size, 0, 'only explicit acknowledgement clears the notice');
  assert.equal(recovered.renderCalls.length, 0, 'notice dismissal must not re-render unsent upload forms');
  assert.equal(recovered.root.find('ig-hold-notices').children.length, 0);
  failures.set('scheduled', {topic:'Earlier reel', message:'First failure after loading'});
  vm.runInContext('refreshHoldNotices()', recovered.context);
  assert.equal(recovered.root.find('ig-hold-notices').children.length, 1, 'an initially empty notice region must handle new failures');
  await sender();
  console.log('Instagram calendar and section routing regressions passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
