// Fleet: one capacity line above the panes, in the owner's words and order, each number a filter.
//
// Owner, 2026-11-18: *"how many machines are generating? which ones are they and which ones
// aren't … i need a simple summary of it in the Gateway UI on the web."* The numbers below are the
// live fleet read from that day; every count comes from the same payload the tables draw, and a
// click must filter the table to those rows and say how many it is showing.
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
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function element(extra = {}) {
  return { innerHTML: '', textContent: '', value: '', options: [], selectedOptions: [],
    dataset: {}, querySelector: () => null,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    hidden: true, appendChild() {}, replaceChildren() {}, remove() {}, insertAdjacentHTML() {},
    addEventListener() {}, ...extra };
}
const IDS = ['fleet-filter', 'fleet-location', 'fleet-search', 'fleet-status', 'fleet-warnings',
  'fleet-machines-wrap', 'fleet-active-filters', 'fleet-facet-state', 'fleet-facet-issues',
  'fleet-facet-version', 'fleet-facet-network', 'fleet-filters', 'fleet-show-wrap',
  'fleet-machine-filters', 'fleet-filter-count', 'fleet-threshold-note', 'fleet-warning-settings',
  'fleet-tabs', 'fleet-drops', 'fleet-capacity', 'fleet-locations-wrap', 'fleet-locations-status',
  'fleet-issues'];

function harness(payload) {
  const nodes = new Map(IDS.map(id => [id, element()]));
  const ctx = vm.createContext({
    document: { getElementById: id => nodes.get(id), querySelector: () => null,
      querySelectorAll: () => [], activeElement: null },
    window: {}, esc, ago: () => 'just now',
    FLEET_STATES: { working: ['info', 'Working'], offline: ['bad', 'Offline'],
      ready: ['ok', 'Ready'], needs_attention: ['warn', 'Needs attention'],
      long_idle: ['', 'Idle'], just_finished: ['ok', 'Just finished'] },
    state: { view: 'fleet' }, nodes,
    api: async () => payload,
    Date, Number, Math, isFinite, setTimeout, clearTimeout, JSON,
  });
  vm.runInContext(`globalThis.uiConfirm = async () => true; globalThis.uiAlert = async () => {};`, ctx);
  vm.runInContext(between("let fleetFilter = 'all';", 'async function loadJobs()'), ctx);
  vm.runInContext('globalThis.__setPayload = value => { lastFleetPayload = value; };', ctx);
  return { ctx, nodes };
}

function machine(state, overrides = {}) {
  return { machine: state, label: state, location: 'Blue Room', location_id: 'L1',
    state, reachable: true, hub_version: '0.6.245', version_status: 'latest',
    history: { observed: true, drops_day: 0, problematic: false }, causes: [], ...overrides };
}

const SUMMARY = { locations: 20, locations_up: 11, pulse: {
  working: 20, ready: 2, long_idle: 2, needs_attention: 4, just_finished: 1, offline: 13 } };
const PAYLOAD = {
  machines: [machine('working'), machine('working'), machine('ready'), machine('offline')],
  locations: [
    { id: 'up', label: 'Up', reachable: true },
    { id: 'down1', label: 'Down one', reachable: false },
    { id: 'down2', label: 'Down two', reachable: false },
  ],
  summary: SUMMARY,
};

test('the line reads in the owner’s order and words, and counts what the read says', () => {
  const { ctx, nodes } = harness(PAYLOAD);
  ctx.__setPayload(PAYLOAD);
  vm.runInContext('renderFleetCapacity(lastFleetPayload)', ctx);
  const text = nodes.get('fleet-capacity').innerHTML;
  const order = ['20 generating', '2 ready', '2 idle', '4 need attention', '1 just finished', '13 offline'];
  const at = order.map(fragment => text.indexOf(fragment));
  assert.ok(at.every(index => index >= 0), 'every count is drawn');
  assert.deepEqual([...at].sort((a, b) => a - b), at, 'the counts keep the owner’s order');
  assert.match(text, /9 sites not reporting/, 'sites = locations minus those that answered');
});

test('an unknown state is shown when there is one, and never invented when there is not', () => {
  const { ctx, nodes } = harness(PAYLOAD);
  ctx.__setPayload(PAYLOAD);
  vm.runInContext('renderFleetCapacity(lastFleetPayload)', ctx);
  assert.doesNotMatch(nodes.get('fleet-capacity').innerHTML, /unknown/);
  const withUnknown = { ...PAYLOAD, summary: { ...SUMMARY, pulse: { ...SUMMARY.pulse, unknown: 3 } } };
  ctx.__setPayload(withUnknown);
  vm.runInContext('renderFleetCapacity(lastFleetPayload)', ctx);
  assert.match(nodes.get('fleet-capacity').innerHTML, /3 unknown/);
});

test('clicking a number filters the Machines table and says how many it shows', () => {
  const { ctx } = harness(PAYLOAD);
  ctx.__setPayload(PAYLOAD);
  vm.runInContext("fleetCapacityFilter('working')", ctx);
  assert.equal(vm.runInContext('fleetTab', ctx), 'machines', 'it opens the table the rows live in');
  assert.deepEqual([...vm.runInContext('[...fleetFacetState]', ctx)], ['Working'],
    'the state facet is the filter, so the existing chips and Reset work on it');
  assert.equal(vm.runInContext('fleetMachineRowsFor(lastFleetPayload).length', ctx), 2,
    'the table shows only the generating Macs');
  vm.runInContext('renderFleetCapacity(lastFleetPayload)', ctx);
  assert.match(vm.runInContext("document.getElementById('fleet-capacity').innerHTML", ctx),
    /Showing 20 generating/);
});

test('clicking the site number opens Locations filtered to the sites that did not answer', () => {
  const { ctx } = harness(PAYLOAD);
  ctx.__setPayload(PAYLOAD);
  vm.runInContext("fleetCapacityFilter('sites')", ctx);
  assert.equal(vm.runInContext('fleetTab', ctx), 'locations');
  assert.equal(vm.runInContext('fleetSitesDownOnly', ctx), true);
  assert.deepEqual([...vm.runInContext('fleetLocationRowsFor(lastFleetPayload).map(L => L.id)', ctx)],
    ['down1', 'down2']);
});

test('Show all clears the capacity filter and its facet, and Reset filters clears it too', () => {
  const { ctx } = harness(PAYLOAD);
  ctx.__setPayload(PAYLOAD);
  vm.runInContext("fleetCapacityFilter('working')", ctx);
  vm.runInContext('fleetCapacityClear()', ctx);
  assert.equal(vm.runInContext('fleetCapacityPick', ctx), null);
  assert.equal(vm.runInContext('fleetFacetState.size', ctx), 0);
  vm.runInContext("fleetCapacityFilter('sites')", ctx);
  vm.runInContext('fleetClearFacets()', ctx);
  assert.equal(vm.runInContext('fleetSitesDownOnly', ctx), false);
  assert.equal(vm.runInContext('fleetCapacityPick', ctx), null);
});
