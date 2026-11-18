// Fleet > Locations: every column header sorts, announces its state, and never touches the
// Machines table's sort.
//
// Owner/Developer, 2026-11-18: Location / Health / Likely cause / Network / Hub / Members / Models /
// Status were the only headers on this page with no handler — they looked sortable and did nothing,
// while the Machines table's identical-looking headers worked. Each column below is driven through
// the page's own functions over a fleet read.
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
const IDS = ['fleet-filter', 'fleet-sort', 'fleet-location', 'fleet-search', 'fleet-status',
  'fleet-warnings', 'fleet-machines-wrap', 'fleet-active-filters', 'fleet-facet-state',
  'fleet-facet-issues', 'fleet-facet-version', 'fleet-facet-network', 'fleet-filters',
  'fleet-show-wrap', 'fleet-machine-filters', 'fleet-filter-count', 'fleet-threshold-note',
  'fleet-warning-settings', 'fleet-tabs', 'fleet-drops', 'fleet-capacity',
  'fleet-locations-wrap', 'fleet-locations-status', 'fleet-issues'];

function harness(payload) {
  const nodes = new Map(IDS.map(id => [id, element()]));
  const ctx = vm.createContext({
    document: { getElementById: id => nodes.get(id), querySelector: () => null,
      querySelectorAll: () => [], activeElement: null },
    window: {}, esc, ago: () => 'just now',
    FLEET_STATES: { working: ['info', 'Working'], offline: ['bad', 'Offline'],
      ready: ['ok', 'Ready'], needs_attention: ['warn', 'Needs attention'] },
    state: { view: 'fleet' }, nodes,
    api: async () => payload,
    Date, Number, Math, isFinite, setTimeout, clearTimeout, JSON,
  });
  vm.runInContext(`globalThis.uiConfirm = async () => true; globalThis.uiAlert = async () => {};`, ctx);
  vm.runInContext(between("let fleetFilter = 'all';", 'async function loadJobs()'), ctx);
  vm.runInContext('globalThis.__setPayload = value => { lastFleetPayload = value; };', ctx);
  return { ctx, nodes };
}

function location(overrides = {}) {
  return { id: 'L1', label: 'Blue Room', reachable: true, network: 'Tailscale', version: '0.6.245',
    version_status: 'latest', causes: [], node_summary: { total: 3, reachable: 3, unauthorized: 0 },
    models_publishable: 12, models_discovered: 14,
    history: { observed: true, online: true, days: [], drops_day: 0, drops_week: 0,
      problematic: false, offline_since: null }, ...overrides };
}

const LOCATIONS = [
  location({ id: 'a', label: 'Alpha', node_summary: { total: 5, reachable: 5, unauthorized: 0 },
    models_publishable: 9, history: { observed: true, drops_day: 0, problematic: false } }),
  location({ id: 'b', label: 'Bravo', node_summary: { total: 12, reachable: 11, unauthorized: 1 },
    models_publishable: 20, history: { observed: false } }),
  location({ id: 'c', label: 'Charlie', reachable: false, node_summary: { total: null, reachable: null },
    models_publishable: null, version_status: 'behind',
    history: { observed: true, drops_day: 2, problematic: true } }),
];
const PAYLOAD = { locations: LOCATIONS, machines: [], summary: { locations: 3, locations_up: 2, pulse: {} } };

function ids(ctx) {
  return [...vm.runInContext('fleetLocationRowsFor(lastFleetPayload).map(L => L.id)', ctx)];
}

test('every Locations header is a sort button that announces aria-sort, like the Machines table', () => {
  const { ctx } = harness(PAYLOAD);
  ctx.__setPayload(PAYLOAD);
  const columns = vm.runInContext('FLEET_LOCATION_COLUMNS.map(c => c[0])', ctx);
  // The prose column became one state word (owner, 2026-11-18); the sentence lives in the tooltip.
  assert.deepEqual([...columns],
    ['location', 'health', 'state', 'network', 'hub', 'members', 'models', 'status']);
  for (const key of columns) {
    const header = vm.runInContext(`fleetLocationSortHeader('${key}', '${key}')`, ctx);
    assert.match(header, /<button type="button" class="sort"/);
    assert.match(header, new RegExp(`fleetSortLocationsBy\\('${key}'\\)`));
    assert.match(header, /aria-sort="none"/);
    vm.runInContext(`fleetSortLocationsBy('${key}')`, ctx);
    assert.match(vm.runInContext(`fleetLocationSortHeader('${key}', '${key}')`, ctx),
      /aria-sort="ascending"/, `${key} announces ascending`);
    vm.runInContext(`fleetSortLocationsBy('${key}')`, ctx);
    assert.match(vm.runInContext(`fleetLocationSortHeader('${key}', '${key}')`, ctx),
      /aria-sort="descending"/, `${key} announces descending`);
  }
});

test('Location sorts by the name the row reads, either way', () => {
  const { ctx } = harness(PAYLOAD);
  ctx.__setPayload(PAYLOAD);
  vm.runInContext("fleetSortLocationsBy('location')", ctx);
  assert.deepEqual(ids(ctx), ['a', 'b', 'c']);
  vm.runInContext("fleetSortLocationsBy('location')", ctx);
  assert.deepEqual(ids(ctx), ['c', 'b', 'a']);
});

test('Members and Models sort by their numbers, and a value nobody reported sorts last both ways', () => {
  const { ctx } = harness(PAYLOAD);
  ctx.__setPayload(PAYLOAD);
  vm.runInContext("fleetSortLocationsBy('members')", ctx);
  assert.deepEqual(ids(ctx), ['a', 'b', 'c'], 'members ascending, unknown last');
  vm.runInContext("fleetSortLocationsBy('members')", ctx);
  assert.deepEqual(ids(ctx), ['b', 'a', 'c'], 'members descending, unknown still last');
  vm.runInContext("fleetSortLocationsBy('models')", ctx);
  assert.deepEqual(ids(ctx), ['a', 'b', 'c'], 'models ascending, 9 before 20, unknown last');
});

test('Health puts the worst first and an unmeasured site last, and State sorts by the word it shows', () => {
  const { ctx } = harness(PAYLOAD);
  ctx.__setPayload(PAYLOAD);
  vm.runInContext("fleetSortLocationsBy('health')", ctx);
  assert.equal(ids(ctx)[0], 'c', 'a problematic site comes before healthy ones');
  assert.equal(ids(ctx)[2], 'b', 'a site with no history is last');
  vm.runInContext("fleetSortLocationsBy('state')", ctx);
  assert.equal(ids(ctx)[0], 'c', 'Offline sorts before Working');
});

test('Sorting Locations never changes the Machines table’s sort', () => {
  const { ctx } = harness(PAYLOAD);
  ctx.__setPayload(PAYLOAD);
  assert.equal(vm.runInContext('machineSort.key', ctx), null);
  vm.runInContext("fleetSortLocationsBy('members')", ctx);
  assert.equal(vm.runInContext('locationSort.key', ctx), 'members');
  assert.equal(vm.runInContext('machineSort.key', ctx), null,
    'the Machines table keeps its own state');
  vm.runInContext("fleetSortBy('machine')", ctx);
  assert.equal(vm.runInContext('locationSort.key', ctx), 'members',
    'sorting Machines leaves the Locations sort alone');
});
