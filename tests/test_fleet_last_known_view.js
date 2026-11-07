/**
 * A slow site's Macs on the Fleet page: drawn, marked as old, sorted with their site, and never
 * counted as down or online.
 *
 * The server now carries those rows separately (`last_known_machines`) and `admin.fleet_snapshot`
 * adds them to the machine list with `last_known: true`, `as_of`, `state_text` and
 * `last_known_state`. This file pins the page's half, in the page's own code (a VM over
 * `admin_ui.html`; never a live gateway):
 *
 * 1. **The row says "Last report HH:MM"**, never a live "Working"/"Ready" and never "Offline".
 * 2. **Nothing that reads as a live measurement is drawn**: Disk, Memory, Network, actions and the
 *    history cells say "—", and no action button is offered.
 * 3. **The "Offline now" filter does not count it as down**, in the table or the Groups list.
 * 4. **It keeps its place with its site's rows**, in the page's shared order (measured rows
 *    first, the carried pair last and together).
 * 5. **The summary says how many were carried**, separately from the measured online count.
 */
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
  return { innerHTML: '', textContent: '', value: '', options: [], dataset: {},
    querySelector: () => null, ...extra };
}

function context(payload = null) {
  const nodes = new Map();
  for (const id of ['fleet-body', 'fleet-summary', 'fleet-issues', 'fleet-filter', 'fleet-threshold',
    'fleet-machines-wrap', 'fleet-status', 'fleet-warnings', 'fleet-locations-wrap',
    'fleet-locations-status', 'fleet-models-wrap', 'fleet-models-status', 'fleet-jobs-wrap',
    'fleet-jobs-status', 'fleet-jobs-problems', 'fleet-tabs', 'fleet-search', 'fleet-location',
    'fleet-drops', 'fleet-threshold-note']) nodes.set(id, element());
  const ctx = vm.createContext({
    document: {
      getElementById: id => nodes.get(id), querySelector: () => null, activeElement: null,
      querySelectorAll: () => [],
    },
    window: {},
    esc,
    ago: () => 'just now',
    FLEET_STATES: {
      working: ['info', 'Working'], just_finished: ['ok', 'Just finished'],
      needs_attention: ['warn', 'Needs attention'], offline: ['bad', 'Offline'], ready: ['ok', 'Ready'],
    },
    state: { view: 'fleet' },
    nodes,
    api: async () => payload,
    Date,
    Number,
    Math,
    isFinite,
    setTimeout,
    clearTimeout,
  });
  vm.runInContext(between("let fleetFilter = 'all';", 'async function loadJobs()'), ctx);
  return ctx;
}

function history(overrides = {}) {
  return {
    observed: true, observed_at: 1000, online: true, since: 1000, online_since: 1000,
    offline_since: null, drops_day: 0, drops_week: 0, flips_day: 0, flips_week: 0,
    offline_seconds_day: 0, offline_seconds_week: 0, days: [], problematic: false, last_drop: null,
    ...overrides,
  };
}

function machine(overrides = {}) {
  return {
    machine: 'g1', label: 'g1', location: 'Garage', location_id: 'd1',
    state: 'ready', reachable: true, chip: 'Apple M4', memory_gb: 32, available_gb: 18.4,
    disk_free_gb: 61, disk_total_gb: 460, hub_version: '0.6.213', version_status: 'latest',
    history: history(), causes: [{ code: 'fine', text: 'Nothing wrong reported', evidence: null }],
    ...overrides,
  };
}

// A row the server carried from its site's last answer: old live fields are still on it, and the
// page must not draw them as now.
function carried(overrides = {}) {
  return {
    machine: 'm1', label: 'm1', location: 'Terranash', location_id: 'c1',
    last_known: true, as_of: Date.now() / 1000 - 600, state: 'last_known',
    state_text: 'Last report 12:31', last_known_state: 'working', reachable: null,
    location_reachable: false, chip: 'Apple M4', memory_gb: 32, available_gb: 18.4,
    disk_free_gb: 61, disk_total_gb: 460, hub_version: '0.6.213',
    ...overrides,
  };
}

function location(overrides = {}) {
  return {
    id: 'd1', label: 'Garage', url: 'http://100.64.0.11:47873', reachable: true,
    version: '0.6.213', version_status: 'latest', machines: [{}],
    history: history(), causes: [{ code: 'fine', text: 'Nothing wrong reported' }], ...overrides,
  };
}

function payload(machines, locations = []) {
  return {
    summary: {
      machines: machines.filter(m => m.last_known !== true).length,
      machines_online: machines.filter(m => m.last_known !== true && m.reachable !== false).length,
      machines_last_known: machines.filter(m => m.last_known === true).length,
      locations: locations.length, locations_up: locations.filter(l => l.reachable !== false).length,
      pulse: { working: 0 },
    },
    gateway_queue: { waiting: 0, running: 0 },
    locations, machines, running: [], issues: [],
    generated_at: Math.floor(Date.now() / 1000),
    fleet_health: { flapping_drops_day: 4, default: 4, min: 1, max: 100, window_days: 7 },
  };
}

function sample() {
  return payload([
    machine(),
    carried({ machine: 'm1', label: 'm1' }),
    carried({ machine: 'm2', label: 'm2' }),
    machine({ machine: 'b1', label: 'b1', location: 'Broken', location_id: 'd2', reachable: false,
      state: 'offline', history: history({ online: false, offline_since: 1 }) }),
  ], [
    location(),
    location({ id: 'c1', label: 'Terranash', reachable: false, machines: [],
      error: 'No answer within 6s. last report 12:31.' }),
    location({ id: 'd2', label: 'Broken' }),
  ]);
}

test('a carried row says when it last reported, never a live state', async () => {
  const ctx = context(sample());
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-machines-wrap').innerHTML;

  assert.match(html, /Last report 12:31/);
  assert.match(html, /shown from its last answer/);
  assert.match(html, /not counted as online/);
  assert.match(html, /class="fleet-last-known"/);
  // The old state is on the row, and is not drawn as now.
  assert.doesNotMatch(html, />Working</);
  assert.equal(ctx.fleetStateLabel(carried()), 'Last report 12:31');
  assert.equal(ctx.fleetOfflineMachine(carried()), false);
});

test('no live measurement or action is drawn on a carried row', () => {
  const ctx = context(sample());
  const row = ctx.fleetMachineRow(carried());
  assert.doesNotMatch(row, /onclick=/, 'no action is offered on a Mac the Gateway cannot reach');
  assert.match(row, /data-label="Actions">—<\/td>/);
  assert.doesNotMatch(row, /GB free/, 'Disk and Memory are not drawn from a stale reading');
  assert.match(row, /Not measured this read/);
});

test('the Offline now filter does not count a carried row as down', async () => {
  const ctx = context(sample());
  await ctx.loadFleet();
  ctx.fleetSetFilter('offline');

  const table = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.match(table, />b1</, 'the Mac that really is down is listed');
  assert.doesNotMatch(table, />m1</);
  assert.doesNotMatch(table, />m2</);

  // The Groups list uses its own predicate; it must agree.
  const groups = ctx.nodes.get('fleet-body').innerHTML;
  assert.match(groups, />b1</);
  assert.doesNotMatch(groups, />m1</);
  assert.doesNotMatch(groups, />m2</);
});

test('a carried row keeps its site\'s place in the list', async () => {
  const ctx = context(sample());
  await ctx.loadFleet();
  const table = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  const at = name => table.indexOf(`>${name}<`);
  // The Machines table now draws the page's shared order (the same one the cards use): the Mac
  // that is down first, measured rows next, and the carried pair last — still together, still in
  // the order the Gateway sent them, never scattered between measured rows of other sites.
  assert.ok(at('b1') < at('g1'), 'the down Mac comes first, as the cards draw it');
  assert.ok(at('g1') < at('m1'), 'a measured row is never drawn after a carried one');
  assert.ok(at('m1') < at('m2'), 'the slow site\'s rows stay together');
});

test('the summary says how many rows were carried, apart from the measured online count', async () => {
  const ctx = context(sample());
  await ctx.loadFleet();
  const summary = ctx.nodes.get('fleet-summary').innerHTML;
  // Measured: two Macs in total, both online (g1 ready, b1 offline is not online — so 1).
  assert.match(summary, /<b>1<\/b> of 2 Macs online/);
  // **Not counted either way** (owner's wording, 2026-10-30): the header's issue count says how
  // many rows were left out, so "2 issues" can never be read as including them.
  assert.match(summary, /2 not counted, shown from their last report/);
  assert.match(summary, /<b>2<\/b> issues: 2 down · 0 degraded · 0 info/);
  assert.match(ctx.nodes.get('fleet-status').innerHTML, /2 shown from their last report/);
});

test('a site card names its carried members without calling them offline', async () => {
  const ctx = context(sample());
  await ctx.loadFleet();
  const body = ctx.nodes.get('fleet-body').innerHTML;
  assert.match(body, /2 Macs \(2 from their last report\)/);
  assert.doesNotMatch(body, /2 Macs \(2 from their last report\) · 2 offline/);
});
