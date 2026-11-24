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

// Test section routing without a browser. Real DOM interactions are covered by UI testing.
class Element {
  constructor(tag, attrs = {}, children = []) {
    this.tag = tag; this.attrs = attrs; this.children = []; this.events = {};
    this.hidden = Object.hasOwn(attrs, 'hidden');
    this.append(...children.flat(Infinity));
  }
  append(...children) { this.children.push(...children.filter(value => value !== null && value !== undefined && value !== false)); }
  setAttribute(name, value) { this.attrs[name] = value; }
  addEventListener(name, fn) { this.events[name] = fn; }
  find(id) { return this.attrs.id === id ? this : this.children.map(child => child instanceof Element ? child.find(id) : null).find(Boolean); }
}

async function dashboard(savedHold = null) {
  const renderedDrafts = [];
  const data = {
    state: 'available', auto_publish: true, runs: [], token: {state: 'valid'},
    drafts: [
      {draft_id: 'failed', status: 'failed', publish_error: 'outcome unknown; check Instagram'},
      {draft_id: 'scheduled', status: 'pending', review_score: 90, reel: {hold: false}},
      {draft_id: 'held', status: 'pending', review_score: 90, reel: {hold: true}},
      {draft_id: 'approved', status: 'approved', review_score: 90},
    ],
  };
  const context = vm.createContext({
    session: {permission: 'recommend'}, load: async () => data,
    igView: {section: 'performance'}, igNumber: Number.isFinite,
    el: (tag, attrs, ...children) => new Element(tag, attrs, children),
    card: (title, ...children) => new Element('card', {title}, children), muted: text => text,
    tokenCard: () => new Element('token'), igPerformance: () => new Element('performance'),
    unconfirmedHold: id => savedHold && savedHold.body.draft_id === id ? savedHold : null,
    draftCard: draft => { renderedDrafts.push(draft); return new Element('draft', {id: draft.draft_id}); },
  });
  vm.runInContext(source.slice(source.indexOf('  async function instagram(view)'), source.indexOf('  const OUTCOME_LABELS')), context);
  const root = new Element('root');
  context.root = root;
  await vm.runInContext('instagram(root)', context);
  return {root, context, renderedDrafts};
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

  const pending = await dashboard({token: 'saved-hold', body: {draft_id: 'scheduled', hold: true, version: 0}});
  assert.equal(pending.context.igView.section, 'drafts', 'a reload must instantiate the saved hold retry immediately');
  assert.ok(pending.renderedDrafts.some(draft => draft.draft_id === 'scheduled'));
  assert.equal(pending.root.find('ig-section-performance').hidden, true);
  console.log('Instagram calendar and section routing regressions passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
