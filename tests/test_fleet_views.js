// One view per Fleet tab, remembered per tab.
//
// Owner, 2026-11-13: stop mixing a metric strip, a card wall and a table on one tab. Each tab
// draws exactly one view; the choice is remembered per tab across reloads, with the owner's
// defaults (table for Machines and Models, cards for Groups and Locations).
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');

function between(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + 1);
  if (first < 0 || last < 0) throw new Error(`admin_ui.html moved: ${start}`);
  return source.slice(first, last);
}

function harness() {
  const store = new Map();
  const box = { hidden: false, innerHTML: '' };
  const context = vm.createContext({
    localStorage: {
      getItem: key => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => store.set(key, String(value)),
    },
    document: { getElementById: id => (id === 'fleet-view' ? box : null) },
  });
  vm.runInContext('let fleetTab = "groups"; let lastFleetPayload = null;'
    + between('const FLEET_VIEW_DEFAULT', 'function renderFleetViewSwitch')
    + between('function renderFleetViewSwitch()', 'async function saveFleetThreshold')
    + ';globalThis.__v = { fleetViewFor, fleetSetView, renderFleetViewSwitch,'
    + ' tab: value => { fleetTab = value; }, setPayload: value => { lastFleetPayload = value; } };',
    context);
  return { v: context.__v, box, store };
}

test('the owner defaults hold before anything is chosen', () => {
  const { v } = harness();
  assert.equal(v.fleetViewFor(), 'cards', 'Groups defaults to cards');
  v.tab('locations'); assert.equal(v.fleetViewFor(), 'cards');
  v.tab('machines'); assert.equal(v.fleetViewFor(), 'table');
  v.tab('models'); assert.equal(v.fleetViewFor(), 'table');
  v.tab('jobs'); assert.equal(v.fleetViewFor(), 'list');
  v.tab('story'); assert.equal(v.fleetViewFor(), 'list');
});

test('a changed view is remembered per tab and does not leak to another tab', () => {
  const { v, store } = harness();
  v.tab('groups');
  v.fleetSetView('table');
  assert.equal(store.get('gatewayFleetView.groups'), 'table');
  assert.equal(v.fleetViewFor('groups'), 'table');

  v.tab('machines');
  assert.equal(v.fleetViewFor(), 'table', 'the Machines default is untouched');
  v.fleetSetView('cards');
  v.tab('groups');
  assert.equal(v.fleetViewFor(), 'table', 'Groups still remembers its own choice');
});

test('the switch exists only where two views do, and marks the current one', () => {
  const { v, box } = harness();
  v.tab('machines'); v.renderFleetViewSwitch();
  assert.equal(box.hidden, false);
  assert.match(box.innerHTML, /aria-pressed="true"[^>]*>Table/);
  v.tab('jobs'); v.renderFleetViewSwitch();
  assert.equal(box.hidden, true, 'Jobs has one view; a switch with one option is noise');
  assert.equal(box.innerHTML, '');
});
