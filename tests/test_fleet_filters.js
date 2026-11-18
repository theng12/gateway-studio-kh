// Fleet > Machines: grouped live filters, counts, chips, reset and column sorting.
//
// Owner, 2026-11-15: the Filters control had no useful options and there was no way to sort by
// issues. These drive the page's own functions over a fleet read; every option and count below
// comes from that read, never from a list in the page.
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
  'fleet-warnings', 'fleet-machines-wrap', 'fleet-machines-wrap', 'fleet-active-filters',
  'fleet-facet-state', 'fleet-facet-issues', 'fleet-facet-version', 'fleet-facet-network',
  'fleet-filters', 'fleet-show-wrap', 'fleet-machine-filters', 'fleet-filter-count',
  'fleet-threshold-note', 'fleet-warning-settings', 'fleet-tabs', 'fleet-drops'];

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

function history(overrides = {}) {
  return { observed: true, online: true, days: [], drops_day: 0, drops_week: 0,
    problematic: false, offline_since: null, ...overrides };
}
function machine(overrides = {}) {
  return { machine: '1301', label: '1301', location: 'Blue Room', location_id: 'L1',
    state: 'ready', reachable: true, hub_version: '0.6.126', version_status: 'latest',
    history: history(), causes: [], ...overrides };
}
function payload(machines) {
  return { machines, locations: [{ id: 'L1', label: 'Blue Room' }], summary: {} };
}

const MACHINES = [
  machine({ machine: 'slow', label: 'slow', connection: { status: 'measured', kind: 'ethernet', slow: true } }),
  machine({ machine: 'disk', label: 'disk', disk: { state: 'red', free_gb: 4 } }),
  machine({ machine: 'off', label: 'off', reachable: false, state: 'offline' }),
  machine({ machine: 'fine', label: 'fine' }),
];

test('the filter menu is built from the read: states, issues and live counts', () => {
  const { ctx, nodes } = harness(payload(MACHINES));
  ctx.__setPayload(payload(MACHINES));
  vm.runInContext('fleetBuildFacets(lastFleetPayload)', ctx);
  const states = nodes.get('fleet-facet-state').innerHTML;
  assert.match(states, /Ready/);
  assert.match(states, /Offline/);
  assert.match(states, /<span class="n">3<\/span>/, 'three working Macs');
  const issues = nodes.get('fleet-facet-issues').innerHTML;
  assert.match(issues, /Slow Ethernet/);
  assert.match(issues, /Disk red/);
  assert.match(issues, /Any issue/);
  assert.match(issues, /<span class="n">1<\/span>/, 'one Mac per issue here');
});

test('a selected issue narrows the rows, the chip says so, and Reset restores every row', () => {
  const { ctx, nodes } = harness(payload(MACHINES));
  ctx.__setPayload(payload(MACHINES));
  vm.runInContext("fleetToggleFacet('issue','slow_link',true)", ctx);
  const rows = vm.runInContext('fleetMachineRowsFor(lastFleetPayload).map(m => m.label)', ctx);
  assert.deepEqual([...rows], ['slow']);
  assert.match(nodes.get('fleet-active-filters').innerHTML, /Issue: Slow Ethernet/);
  assert.match(nodes.get('fleet-active-filters').innerHTML, /Reset filters/);
  vm.runInContext('fleetClearFacets()', ctx);
  const all = vm.runInContext('fleetMachineRowsFor(lastFleetPayload).map(m => m.label)', ctx);
  assert.equal(all.length, 4, 'clearing filters brings every row back');
  assert.equal(nodes.get('fleet-active-filters').innerHTML, '');
});

test('two conditions combine, and a version filter reads the live versions', () => {
  const { ctx } = harness(payload(MACHINES));
  ctx.__setPayload(payload(MACHINES));
  vm.runInContext("fleetToggleFacet('state','Ready',true); fleetToggleFacet('issue','disk_red',true)", ctx);
  const rows = vm.runInContext('fleetMachineRowsFor(lastFleetPayload).map(m => m.label)', ctx);
  assert.deepEqual([...rows], ['disk']);
  vm.runInContext("fleetClearFacets(); fleetSetFacetVersion('0.6.126')", ctx);
  assert.equal(vm.runInContext('fleetMachineRowsFor(lastFleetPayload).length', ctx), 4);
});

test('the network filter matches the measured link kind, and Reset clears it', () => {
  const machines = [
    machine({ machine: 'eth', connection: { status: 'measured', kind: 'ethernet', slow: false } }),
    machine({ machine: 'wifi', connection: { status: 'measured', kind: 'wifi', slow: false } }),
    machine({ machine: 'none' }),
  ];
  const { ctx } = harness(payload(machines));
  ctx.__setPayload(payload(machines));
  const labels = () => [...vm.runInContext('fleetMachineRowsFor(lastFleetPayload).map(m => m.machine)', ctx)];
  vm.runInContext("fleetSetFacetNetwork('ethernet')", ctx);
  assert.deepEqual(labels(), ['eth']);
  vm.runInContext("fleetSetFacetNetwork('wifi')", ctx);
  assert.deepEqual(labels(), ['wifi']);
  vm.runInContext("fleetSetFacetNetwork('unknown')", ctx);
  assert.deepEqual(labels(), ['none'], 'a Mac that reported no link is "not reported", never a guess');
  vm.runInContext("fleetSetFacetNetwork('other')", ctx);
  assert.deepEqual(labels(), []);
  vm.runInContext('fleetClearFacets()', ctx);
  assert.equal(labels().length, 3, 'Reset restores every row');
});

test('the Machines table is the six columns the owner scans, and announces its sort', () => {
  const { ctx } = harness(payload(MACHINES));
  ctx.__setPayload(payload(MACHINES));
  const columns = vm.runInContext('FLEET_MACHINE_COLUMNS.map(c => c[0])', ctx);
  // Owner, 2026-11-18: Machine · State · Version · Disk free · RAM free · Network. Location and
  // the cause sentence moved into the row's Details panel.
  assert.deepEqual([...columns], ['machine', 'state', 'version', 'disk', 'memory', 'network']);
  const row = vm.runInContext('fleetMachineRow(lastFleetPayload.machines[0])', ctx);
  assert.match(row, /data-label="Machine"/);
  assert.match(row, /Blue Room/);
  assert.match(row, /fleet-detail-row/);
  assert.doesNotMatch(row.split('fleet-detail-row')[0], /data-label="Location"/);
  const header = vm.runInContext("fleetSortHeader('machine','Machine')", ctx);
  assert.match(header, /aria-sort="none"/);
  vm.runInContext("fleetSortBy('machine')", ctx);
  const sorted = vm.runInContext("fleetSortHeader('machine','Machine')", ctx);
  assert.match(sorted, /aria-sort="ascending"/);
});
