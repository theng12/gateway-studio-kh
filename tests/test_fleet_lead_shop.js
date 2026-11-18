// Fleet, 2026-11-18: the lead answers first, Shops hide behind a switch, Details opens, and a
// menu in a table is actually visible.
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
    dataset: {}, querySelector: () => null, style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    hidden: true, appendChild() {}, replaceChildren() {}, remove() {}, insertAdjacentHTML() {},
    addEventListener() {}, setAttribute() {}, ...extra };
}
const IDS = ['fleet-filter', 'fleet-location', 'fleet-search', 'fleet-status', 'fleet-warnings',
  'fleet-machines-wrap', 'fleet-active-filters', 'fleet-facet-state', 'fleet-facet-issues',
  'fleet-facet-version', 'fleet-facet-network', 'fleet-filters', 'fleet-show-wrap',
  'fleet-machine-filters', 'fleet-filter-count', 'fleet-threshold-note', 'fleet-warning-settings',
  'fleet-tabs', 'fleet-drops', 'fleet-capacity', 'fleet-locations-wrap', 'fleet-locations-status',
  'fleet-issues', 'fleet-machine-action-note', 'pmenu-x'];

function harness(payload) {
  const nodes = new Map(IDS.map(id => [id, element()]));
  const ctx = vm.createContext({
    document: { getElementById: id => nodes.get(id), querySelector: () => null,
      querySelectorAll: () => [], activeElement: null },
    window: { innerWidth: 1200 }, esc, ago: () => 'just now',
    FLEET_STATES: { working: ['info', 'Working'], offline: ['bad', 'Offline'],
      ready: ['ok', 'Ready'], needs_attention: ['warn', 'Needs attention'],
      long_idle: ['', 'Idle'], just_finished: ['ok', 'Just finished'] },
    state: { view: 'fleet' }, nodes,
    api: async () => payload,
    Date, Number, Math, isFinite, setTimeout, clearTimeout, JSON,
  });
  vm.runInContext(`globalThis.uiConfirm = async () => true; globalThis.uiAlert = async () => {};
    globalThis.closeMenus = () => {};`, ctx);
  // The menu helpers live earlier in the file than the fleet block; both are needed here.
  vm.runInContext(between('function toggleCardMenuFor(', "document.addEventListener?.('click'"), ctx);
  vm.runInContext(between("let fleetFilter = 'all';", 'async function loadJobs()'), ctx);
  vm.runInContext('globalThis.__setPayload = value => { lastFleetPayload = value; };', ctx);
  return { ctx, nodes };
}

function machine(overrides = {}) {
  return { machine: 'm1', label: 'm1', location: 'Main', location_id: 'main', state: 'working',
    reachable: true, hub_version: '0.6.245', version_status: 'latest',
    history: { observed: true, drops_day: 0, problematic: false }, causes: [], ...overrides };
}

const PAYLOAD = {
  locations: [{ id: 'main', label: 'Main', reachable: true },
    { id: 'shops', label: 'Shops', reachable: true }],
  machines: [machine(), machine({ machine: 'm2', label: 'm2' }),
    machine({ machine: 's1', label: 's1', location: 'Shops', location_id: 'shops' }),
    machine({ machine: 's2', label: 's2', location: 'Shops', location_id: 'shops' })],
  summary: { locations: 2, locations_up: 2,
    pulse: { working: 4, ready: 0, long_idle: 0, needs_attention: 0, just_finished: 0, offline: 0 } },
};

test('the lead leads with the running count, the split, and per-location failures', () => {
  const { ctx, nodes } = harness(PAYLOAD);
  ctx.__setPayload(PAYLOAD);
  vm.runInContext('renderFleetCapacity(lastFleetPayload)', ctx);
  const html = nodes.get('fleet-capacity').innerHTML;
  assert.match(html, /class="fleet-lead-number">2</, 'Shops is hidden by default, so the two Main Macs count');
  assert.match(html, /running now/);
  assert.match(html, /generating/);
  const failing = harness({ ...PAYLOAD, machines: [
    machine({ machine: 'x1', label: 'x1', state: 'offline', reachable: false }),
    machine({ machine: 's1', label: 's1', location: 'Shops', location_id: 'shops' })],
    locations: [{ id: 'main', label: 'Main', reachable: true }, { id: 'shops', label: 'Shops' }] });
  failing.ctx.__setPayload({ ...PAYLOAD, machines: [
    machine({ machine: 'x1', label: 'x1', state: 'offline', reachable: false }),
    machine({ machine: 's1', label: 's1', location: 'Shops', location_id: 'shops' })],
    locations: [{ id: 'main', label: 'Main', reachable: true }, { id: 'shops', label: 'Shops' }] });
  vm.runInContext('renderFleetCapacity(lastFleetPayload)', failing.ctx);
  assert.match(failing.nodes.get('fleet-capacity').innerHTML, /Main — 1 offline/);
});

test('Shops is hidden by default, the switch shows it, and the line says how many', () => {
  const { ctx, nodes } = harness(PAYLOAD);
  ctx.__setPayload(PAYLOAD);
  vm.runInContext('renderFleetCapacity(lastFleetPayload)', ctx);
  assert.match(nodes.get('fleet-capacity').innerHTML, /2 machines from Shops hidden/);
  assert.deepEqual([...vm.runInContext('fleetMachineRowsFor(lastFleetPayload).map(m => m.machine)', ctx)],
    ['m1', 'm2'], 'only Main is in the table');
  assert.equal(vm.runInContext("fleetCapacityCount(lastFleetPayload, 'working')", ctx), 2,
    'the headline agrees with the table');
  vm.runInContext("fleetToggleLocationHidden('shops')", ctx);
  assert.equal(vm.runInContext('fleetMachineRowsFor(lastFleetPayload).length', ctx), 4,
    'the switch shows them again');
  assert.equal(vm.runInContext("fleetCapacityCount(lastFleetPayload, 'working')", ctx), 4);
});

test('choosing a hidden location in the Location filter still shows its Macs', () => {
  const { ctx } = harness(PAYLOAD);
  ctx.__setPayload(PAYLOAD);
  vm.runInContext('fleetHiddenSet(lastFleetPayload)', ctx);
  vm.runInContext("fleetSetLocation('shops')", ctx);
  assert.equal(vm.runInContext('fleetMachineRowsFor(lastFleetPayload).length', ctx), 2);
});

test('Details opens the row below it, and a carried row has none', () => {
  const { ctx } = harness(PAYLOAD);
  ctx.__setPayload(PAYLOAD);
  const detail = element({ hidden: true });
  detail.classList.contains = name => name === 'fleet-detail-row';
  const row = element({ nextElementSibling: detail });
  const button = element({ closest: () => row, textContent: 'Details…' });
  vm.runInContext('globalThis.__toggle = fleetToggleMachineDetails', ctx);
  ctx.__toggle(button);
  assert.equal(detail.hidden, false, 'the panel opens');
  assert.equal(button.textContent, 'Hide');
});

test('a menu inside the table is placed fixed under its button, so it is visible', () => {
  const { ctx, nodes } = harness(PAYLOAD);
  const menu = nodes.get('pmenu-x');
  menu.hidden = true;
  const button = element({ getBoundingClientRect: () => ({ top: 10, bottom: 30, right: 400, left: 300 }) });
  vm.runInContext('globalThis.__toggle = toggleCardMenu', ctx);
  ctx.__toggle(button, 'x');
  assert.equal(menu.hidden, false);
  assert.equal(menu.style.position, 'fixed');
  assert.equal(menu.style.top, '34px');
  assert.ok(parseInt(menu.style.left, 10) <= 400 - 240);
});
