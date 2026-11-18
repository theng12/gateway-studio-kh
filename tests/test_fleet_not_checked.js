// Fleet 0.2.280: a site that does not answer is counted as **not checked**, never as a smaller
// fleet and never as broken.
//
// Owner's fleet, 2026-11-18 21:45: 1100, 1200 and Shops timed out in one fan-out and the headline
// fell from 29 online to 8 with nothing saying why. The lead must say the Macs it did not check,
// and the location must say not-answering plus its cause and when it was last heard from.
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
    Date, Number, Math, isFinite, setTimeout, clearTimeout, JSON, localStorage: null,
  });
  vm.runInContext(`globalThis.uiConfirm = async () => true; globalThis.uiAlert = async () => {};
    globalThis.closeMenus = () => {};`, ctx);
  vm.runInContext(between('function toggleCardMenuFor(', "document.addEventListener?.('click'"), ctx);
  vm.runInContext(between("let fleetFilter = 'all';", 'async function loadJobs()'), ctx);
  return { ctx, nodes };
}

const HEARD_AT = Math.floor(Date.now() / 1000) - 600;
const machine = (overrides = {}) => ({
  machine: 'm1', label: 'm1', location: '1400', location_id: 'l1400', state: 'working',
  reachable: true, hub_version: '0.6.245', version_status: 'latest',
  history: { observed: true, drops_day: 0, problematic: false }, causes: [], ...overrides,
});
const PAYLOAD = {
  locations: [
    { id: 'l1400', label: '1400', reachable: true },
    { id: 'l1700', label: '1700', reachable: true },
    { id: 'l1100', label: '1100', reachable: false, error_kind: 'timeout', slow: true,
      members_from: HEARD_AT, node_summary: { total: 11, reachable: 10, unauthorized: 0 },
      error: 'No answer within 10s.' },
    { id: 'lshops', label: 'Shops', reachable: false, error_kind: 'timeout', slow: true,
      members_from: HEARD_AT, node_summary: { total: 9, reachable: 9, unauthorized: 0 } },
    { id: 'l1300', label: '1300', reachable: false, error_kind: 'refused' },
  ],
  machines: [machine(), machine({ machine: 'm2' }), machine({ machine: 'm3' })],
  summary: { locations: 6, locations_up: 2, machines_not_checked: 42, locations_not_answering: 4,
    pulse: { working: 8, ready: 0, long_idle: 0, needs_attention: 0, just_finished: 0, offline: 0 } },
  generated_at: Date.now() / 1000,
};

test('the lead says what it did not check, beside the other counts', () => {
  const { ctx, nodes } = harness(PAYLOAD);
  vm.runInContext('renderFleetCapacity(__p);', Object.assign(ctx, { __p: PAYLOAD }));
  const lead = nodes.get('fleet-capacity').innerHTML;
  assert.match(lead, /fleet-lead-number">8</);
  assert.match(lead, /42 Macs not checked/);
  assert.match(lead, /4 sites not answering/);
  assert.doesNotMatch(lead, /not reporting/);
  // The tooltip names every silent site with its own cause and when it was last heard from.
  assert.match(lead, /1100: no answer in time, last heard \d\d:\d\d/);
  assert.match(lead, /1300: refused the connection, never heard from/);
});

test('a silent site is Not answering — never Offline — with its cause and last heard', () => {
  const { ctx } = harness(PAYLOAD);
  vm.runInContext('globalThis.__l = __p.locations;', Object.assign(ctx, { __p: PAYLOAD }));
  assert.equal(vm.runInContext("fleetLocationStateWord(__l[2])", ctx), 'Not answering');
  assert.equal(vm.runInContext("fleetLocationStateWord(__l[4])", ctx), 'Not answering');
  const status = vm.runInContext("fleetLocationStatus(__l[2])", ctx);
  assert.equal(status.main, 'Not answering');
  assert.match(status.sub, /^no answer in time · last heard \d\d:\d\d$/);
  assert.equal(status.bad, false, 'silence is not drawn as broken');
  const never = vm.runInContext("fleetLocationStatus(__l[4])", ctx);
  assert.match(never.sub, /refused the connection · never heard from/);
});

test('the Members cell of a silent site says it is an old roster, not a live read', () => {
  const { ctx } = harness(PAYLOAD);
  vm.runInContext('globalThis.__l = __p.locations;', Object.assign(ctx, { __p: PAYLOAD }));
  const tip = vm.runInContext('fleetMembersTooltip(__l[2])', ctx);
  assert.match(tip, /last report named 11 Macs/);
  assert.match(tip, /none of them was checked now/);
  assert.doesNotMatch(tip, /reachable now/);
});

test('a site that answers slowly is measured, not "not checked"', () => {
  const payload = { ...PAYLOAD, locations: [
    { id: 'l1400', label: '1400', reachable: true },
    { id: 'l1700', label: '1700', reachable: true, slow: true },
  ], summary: { ...PAYLOAD.summary, machines_not_checked: 0, locations_not_answering: 0 } };
  const { ctx, nodes } = harness(payload);
  vm.runInContext('renderFleetCapacity(__p);', Object.assign(ctx, { __p: payload }));
  assert.match(nodes.get('fleet-capacity').innerHTML, /0|^$/);   // no not-checked item
  assert.doesNotMatch(nodes.get('fleet-capacity').innerHTML, /Macs? not checked/);
  assert.equal(vm.runInContext('fleetLocationStateWord(__p.locations[1])',
    Object.assign(ctx, { __p: payload })), 'Slow');
});

test('the lead keeps the SSH-hidden line working beside the new count', () => {
  const { ctx, nodes } = harness(PAYLOAD);
  vm.runInContext('renderFleetCapacity(__p);', Object.assign(ctx, { __p: PAYLOAD }));
  const lead = nodes.get('fleet-capacity').innerHTML;
  assert.match(lead, /sites not answering/);
  assert.match(lead, /Macs not checked/);
});

// ---- 0.2.281: the roster survives a restart ---------------------------------------------------
//
// The page cannot know a site's Macs it has never been told about. When a site answers, its roster
// is persisted; after a restart and that site going unreachable, the count is known again. A site
// genuinely never reached reports its Mac count as unknown — never as zero, never as a smaller
// total.

test('a restart keeps the count: 4 not checked is exact, not "at least"', () => {
  const payload = { ...PAYLOAD,
    summary: { ...PAYLOAD.summary, machines_not_checked: 4, locations_not_answering: 1,
      sites_roster_unknown: 0 } };
  const { ctx, nodes } = harness(payload);
  vm.runInContext('renderFleetCapacity(__p);', Object.assign(ctx, { __p: payload }));
  const lead = nodes.get('fleet-capacity').innerHTML;
  assert.match(lead, />4 Macs not checked</);
  assert.doesNotMatch(lead, /at least/);
  assert.doesNotMatch(lead, /no known Macs/);
});

test('a site with no known roster makes the count "at least" and names the unknown', () => {
  const payload = { ...PAYLOAD,
    locations: [...PAYLOAD.locations,
      { id: 'l9999', label: '9999', reachable: false, error_kind: 'timeout' }],
    summary: { ...PAYLOAD.summary, machines_not_checked: 4, locations_not_answering: 5,
      sites_roster_unknown: 1 } };
  const { ctx, nodes } = harness(payload);
  vm.runInContext('renderFleetCapacity(__p);', Object.assign(ctx, { __p: payload }));
  const lead = nodes.get('fleet-capacity').innerHTML;
  assert.match(lead, /at least 4 Macs not checked/);
  assert.match(lead, /1 site with no known Macs/);
  assert.match(lead, /9999: no answer in time, never heard from — its Mac count is unknown/);
});

test('when nothing is known at all the lead says unknown, not zero', () => {
  const payload = { ...PAYLOAD,
    locations: [{ id: 'l9999', label: '9999', reachable: false, error_kind: 'refused' }],
    summary: { ...PAYLOAD.summary, machines_not_checked: 0, locations_not_answering: 1,
      sites_roster_unknown: 1, pulse: { working: 0, ready: 0, long_idle: 0, needs_attention: 0,
        just_finished: 0, offline: 0 } } };
  const { ctx, nodes } = harness(payload);
  vm.runInContext('renderFleetCapacity(__p);', Object.assign(ctx, { __p: payload }));
  const lead = nodes.get('fleet-capacity').innerHTML;
  assert.match(lead, /Macs not checked: unknown/);
  assert.match(lead, /1 site with no known Macs/);
  assert.doesNotMatch(lead, />0 Macs not checked</);
});

test('the table status line says a site with no known Macs leaves the total incomplete', () => {
  const payload = { ...PAYLOAD,
    summary: { ...PAYLOAD.summary, sites_roster_unknown: 1 } };
  const { ctx } = harness(payload);
  vm.runInContext('lastFleetPayload = __p;', Object.assign(ctx, { __p: payload }));
  const line = vm.runInContext('fleetStatusLine(__p.machines, __p.machines)',
    Object.assign(ctx, { __p: payload }));
  assert.match(line, /1 site with no known Macs/);
});
