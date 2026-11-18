/**
 * The Fleet page's Machines and Locations tabs: the native tables, in the native words.
 *
 * The owner, 2026-10-30: *"can i view the fleet on the web and have it look like this? …
 * story studio's control look like it's own app. why can't gateway?"* — and the plan that
 * followed: the web page is the ONE Gateway UI, so its Fleet page is the native page.
 *
 * What this pins, in the page's own code (a VM over `admin_ui.html`; never a live gateway):
 *
 * 1. **The native columns and the native words**, in this order: Machine, State, Health,
 *    Seen, Drops · 7 days, Likely cause, Version, Disk, Memory, Network.
 * 2. **Health is the history's verdict, never a restatement of State**: a Mac that has been
 *    offline for two hours says so, and one nobody measured says "—" rather than "Stable".
 * 3. **A refused credential is not an offline Mac.**
 * 4. **The filters are the Gateway's own filter words** — All machines / Offline now /
 *    Problematic — plus a location and a search over the same rows.
 * 5. **Sorting is by the header**, numbers numerically, unknown last.
 * 6. **The warnings box names the Mac and its free space**, and does not report the disk of a
 *    Mac that is not answering.
 * 7. **The Locations table reads the roster**, including "1 refused" and "models offered of
 *    discovered", from the fields the Gateway sends.
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
    querySelector: () => null, classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } }, hidden: false, appendChild() {}, replaceChildren() {}, remove() {}, insertAdjacentHTML() {}, addEventListener() {},  ...extra };
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
// Every question in the page is now an in-app dialog; the tests' own confirm/alert/prompt
// stubs are where each answer comes from.
vm.runInContext(`globalThis.uiConfirm = async (t) => (typeof confirm === 'function' ? confirm(t) : true);
globalThis.uiAlert = async (t) => { if (typeof alert === 'function') alert(t); };
globalThis.uiPrompt = async (t, v) => (typeof prompt === 'function' ? prompt(t, v) : v);`, ctx);

  vm.runInContext(between("let fleetFilter = 'all';", 'async function loadJobs()'), ctx);
  return ctx;
}

function history(overrides = {}) {
  return {
    observed: true, online: true, observed_since: 1000, observed_at: 1000,
    since: 1000, online_since: 1000, offline_since: null,
    drops_day: 0, drops_week: 0, flips_day: 0, flips_week: 0,
    offline_seconds_day: 0, offline_seconds_week: 0,
    days: [
      { date: '2026-10-24', offline_fraction: 0 },
      { date: '2026-10-25', offline_fraction: 0.25 },
      { date: '2026-10-26', offline_fraction: null },
    ],
    pattern: null, problematic: false, last_drop: null, ...overrides,
  };
}

function machine(overrides = {}) {
  return {
    machine: '1301', label: '1301', location: 'Blue Room', location_id: 'L1', network: 'A',
    state: 'ready', reachable: true, chip: 'Apple M4', memory_gb: 32, available_gb: 18.4,
    disk_free_gb: 61, disk_total_gb: 460, hub_version: '0.6.126', version_status: 'latest',
    history: history(), causes: [{ code: 'fine', text: 'Nothing wrong reported', evidence: null }],
    ...overrides,
  };
}

function location(overrides = {}) {
  return {
    id: 'L1', label: 'Blue Room', url: 'http://100.64.0.11:47873', reachable: true,
    role: 'member', version: '0.6.126', version_status: 'latest',
    node_summary: { total: 3, reachable: 2, unauthorized: 1 },
    models_publishable: 3, models_discovered: 4,
    machine_summary: { total: 2 }, machines: [{}, {}],
    history: history(), causes: [{ code: 'fine', text: 'Nothing wrong reported' }], ...overrides,
  };
}

function payload(machines, locations = []) {
  return {
    summary: { machines: machines.length, machines_online: machines.filter(m => m.reachable !== false).length,
      locations: locations.length, locations_up: locations.filter(l => l.reachable !== false).length,
      pulse: { working: 0 } },
    gateway_queue: { waiting: 0, running: 0 },
    locations, machines, running: [], issues: [],
    generated_at: Math.floor(Date.now() / 1000),
    fleet_health: { flapping_drops_day: 4, default: 4, min: 1, max: 100, window_days: 7 },
  };
}

test('every number in the drops cell names its own window', async () => {
  // Owner, 2026-10-30: a 24 h count beside a 7-day duration must say which is which —
  // "5 drops in 24 h · 42 h offline in 7 days", never "5 in 24 h · 42 h off".
  const ctx = context(payload([machine({
    history: history({ drops_day: 5, drops_week: 41, offline_seconds_week: 151200 }),
  })]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.match(html, /5 drops in 24 h · 42 h offline in 7 days/);
  assert.doesNotMatch(html, /5 in 24 h/);
  assert.equal(ctx.fleetShortSummary(history({ drops_day: 5, offline_seconds_week: 151200 })),
    '5 drops in 24 h · 42 h offline in 7 days');
  // The Groups card's line says the same thing, window by window.
  assert.equal(ctx.fleetHistoryLine({ row: { history: history({ drops_day: 5, drops_week: 41,
    offline_seconds_week: 151200 }) } }),
    '5 drops in 24 h · 41 drops in 7 days · 42 h offline in 7 days');
});

test('the Machines tab draws the native columns in the native order', async () => {
  const ctx = context(payload([machine()]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  const order = ['Machine', 'State', 'Health', 'Seen', 'Drops · 7 days', 'Likely cause',
    'Version', 'Disk', 'Memory', 'Network'].map(word => html.indexOf(word));
  assert.ok(order.every(at => at >= 0), 'every native column is drawn');
  assert.deepEqual([...order].sort((a, b) => a - b), order, 'in the native order');
  assert.match(html, />Ready</);
  assert.match(html, />Stable</);
  assert.match(html, />Latest</);
  assert.match(html, /61\.0 GB free/);
  assert.match(html, /18\.4 GB free/);
});

test('a Mac nobody measured says "—", not "Stable" and not "0 drops"', async () => {
  const ctx = context(payload([machine({
    history: { observed: false, drops_day: null, drops_week: null, offline_seconds_week: null,
      days: [{ date: '2026-10-26', offline_fraction: null }], problematic: null },
  })]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.match(html, /Not measured yet/);
  assert.match(html, /not recorded/);
  assert.doesNotMatch(html, />Stable</);
  assert.doesNotMatch(html, /0 in 24 h/);
});

test('Health is the history\'s verdict, and a Mac down now says how long for', async () => {
  const now = Date.now() / 1000;
  const ctx = context(payload([
    machine({ machine: 'a', label: 'a', reachable: false, state: 'offline',
      history: history({ online: false, offline_since: now - 2 * 3600, drops_day: 1,
        last_drop: { at: now - 2 * 3600, reason: 'unreachable', text: 'It stopped answering.' } }) }),
  ]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  // Two hours offline: the badge says so, rather than drawing the healthy green of State.
  assert.match(html, /Offline 2h/);
  assert.match(html, /title="[^"]*Offline for 2 h/);
});

test('a refused credential is not an offline Mac', async () => {
  const ctx = context(payload([machine({
    machine: 's2-2', label: 's2-2', reachable: true, state: 'working',
    node_state: 'unauthorized', node_role: 'member',
    causes: [{ code: 'unauthorized', text: 'Its controller refused that Mac\'s token — a credential problem, not the network.',
      evidence: 'The node is listed as unauthorized by its controller.' }],
  })]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.match(html, /Credential refused/);
  assert.doesNotMatch(html, />Offline</);
});

test('the filters are the Gateway\'s words, and they filter the table', async () => {
  const ctx = context(payload([
    machine({ machine: 'a', label: 'a' }),
    machine({ machine: 'b', label: 'b', reachable: false, state: 'offline', location_id: 'L2', location: 'Studio 2',
      history: history({ online: false, offline_since: 1 }) }),
    machine({ machine: 'c', label: 'c', history: history({ drops_day: 6, problematic: true }) }),
  ]));
  await ctx.loadFleet();
  assert.match(ctx.nodes.get('fleet-filter').innerHTML, /All machines/);
  ctx.fleetSetFilter('offline');
  let html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.match(html, />b</);
  assert.doesNotMatch(html, />a</);
  ctx.fleetSetFilter('problematic');
  html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.match(html, />c</);
  assert.doesNotMatch(html, />a</);
  ctx.fleetSetFilter('all');
  ctx.fleetSetLocation('L2');
  html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.match(html, />b</);
  assert.doesNotMatch(html, />a</);
});

test('a header sorts numerically, and an unknown value sorts last', async () => {
  const ctx = context(payload([
    machine({ machine: 'small', label: 'small', disk_free_gb: 9 }),
    machine({ machine: 'large', label: 'large', disk_free_gb: 302 }),
    machine({ machine: 'unknown', label: 'unknown', disk_free_gb: null, disk_total_gb: null }),
  ]));
  await ctx.loadFleet();
  ctx.fleetSortBy('disk');
  const html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.ok(html.indexOf('>small<') < html.indexOf('>large<'), '9 before 302, not "9" after "302"');
  assert.ok(html.indexOf('>large<') < html.indexOf('>unknown<'), 'a Mac nobody measured sorts last');
});

// The defect this pins: descending negated the whole comparison, so every unknown moved to the
// front — disk descending drew [unknown, large, small] where the measured values belong first.
test('a descending header still leaves an unknown value last', async () => {
  const ctx = context(payload([
    machine({ machine: 'small', label: 'small', disk_free_gb: 9 }),
    machine({ machine: 'large', label: 'large', disk_free_gb: 302 }),
    machine({ machine: 'unknown', label: 'unknown', disk_free_gb: null, disk_total_gb: null }),
  ]));
  await ctx.loadFleet();
  ctx.fleetSortBy('disk');   // ascending
  ctx.fleetSortBy('disk');   // descending
  const names = Array.from(
    ctx.nodes.get('fleet-machines-wrap').innerHTML.matchAll(/class="ft-name">([^<]+)</g),
    m => m[1]);
  assert.deepEqual(names, ['large', 'small', 'unknown'],
    'measured values descending, then the Mac nobody measured');
});

// Until a column header is chosen the Machines table follows the page's Sort, so the visible
// order and the table's order cannot disagree; an explicit header choice then wins for that table.
test('the default Machines order is the page Sort, and a header choice keeps it', async () => {
  const ctx = context(payload([
    machine({ machine: 'alpha', label: 'alpha', disk_free_gb: 300 }),
    machine({ machine: 'zulu', label: 'zulu', disk_free_gb: 50, reachable: false, state: 'offline',
      history: history({ online: false, offline_since: 1 }) }),
  ]));
  await ctx.loadFleet();
  const table = () => ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.ok(table().indexOf('>zulu<') < table().indexOf('>alpha<'),
    'the default Issues order puts the down Mac first, as the cards do');
  ctx.fleetSortBy('disk');            // ascending: 50 before 300
  ctx.fleetSetSort('name');           // name would say alpha first
  assert.ok(table().indexOf('>zulu<') < table().indexOf('>alpha<'),
    'the header sort survives a new page Sort choice');
});

// Locations filtered but never ordered: the site rows kept the Gateway's payload order whatever
// the Sort control said. They now share the one ordering the cards use. The Show filters are the
// same predicates the cards and the Machines table already use, applied to a site row.
test('the Locations table follows the page Sort, worst first by default', async () => {
  const ctx = context(payload([machine()], [
    location({ id: 'L1', label: 'Alpha room', reachable: true }),
    location({ id: 'L2', label: 'Zulu room', reachable: false, error: 'No answer within 5s.',
      history: history({ problematic: true }),
      causes: [{ code: 'controller_silent', text: 'It answered nothing in time' }] }),
  ]));
  await ctx.loadFleet();
  const table = () => ctx.nodes.get('fleet-locations-wrap').innerHTML;
  assert.ok(table().indexOf('Zulu room') < table().indexOf('Alpha room'),
    'the down site is first even though the payload listed it last');
  ctx.fleetSetSort('name');
  assert.ok(table().indexOf('Alpha room') < table().indexOf('Zulu room'),
    'the visible Sort control is what chooses the order');
  ctx.fleetSetFilter('offline');
  assert.match(table(), /Zulu room/);
  assert.doesNotMatch(table(), /Alpha room/);
  ctx.fleetSetFilter('problematic');
  assert.match(table(), /Zulu room/);
  assert.doesNotMatch(table(), /Alpha room/);
});

test('the warnings box names the Mac and its free space, and skips a Mac that is not answering', async () => {
  const ctx = context(payload([
    machine({ machine: 's2-2', label: 's2-2', disk_free_gb: 14, disk_total_gb: 460,
      disk: { state: 'red', free_gb: 14, total_gb: 460, warning_items: [] } }),
    machine({ machine: 'blue-2', label: 'blue-2', disk_free_gb: 21, disk_total_gb: 460,
      disk: { state: 'amber', free_gb: 21, total_gb: 460, warning_items: [] } }),
    machine({ machine: 'blue-3', label: 'blue-3', reachable: false, state: 'offline',
      disk: { state: 'stop', free_gb: 0, total_gb: 460, warning_items: [] } }),
  ]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-warnings').innerHTML;
  assert.match(html, /s2-2: disk red, 14\.0 GB free/);
  assert.match(html, /blue-2: disk amber, 21\.0 GB free/);
  assert.doesNotMatch(html, /blue-3/);
});

test('the Locations table reads the roster: refused members, models offered, hub and status', async () => {
  const ctx = context(payload([machine()], [
    location(),
    location({ id: 'L3', label: 'Garage', url: 'http://100.64.0.31:47873', reachable: false,
      error: 'No answer within 5s.', version: '0.6.120', version_status: 'behind',
      node_summary: { total: 2, reachable: 0, unauthorized: 0 }, machines: [] }),
  ]));
  // The 0.2.256 Fleet pass made cards the default for Locations; this test reads the
  // Locations table while the other tabs keep their own default.
  ctx.fleetViewFor = tab => (tab === 'locations' ? 'table' : 'cards');
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-locations-wrap').innerHTML;
  assert.match(html, /2 \/ 3 · 1 refused/);
  assert.match(html, /3 \/ 4/);
  assert.match(html, /0\.6\.126 · Latest/);
  assert.match(html, /http:\/\/100\.64\.0\.31:47873/);
  assert.match(html, /No answer within 5s\./);
  assert.match(html, /0\.6\.120 · Behind/);
  assert.match(html, /fleetLocationAction\(this,'test'\)/);
  assert.match(html, /fleetLocationAction\(this,'rename'\)/);
  assert.match(html, /fleetLocationAction\(this,'pause'\)/);
  assert.match(html, /fleetLocationAction\(this,'remove'\)/);
});

test('the location filter offers every location and keeps the owner\'s choice', async () => {
  const ctx = context(payload([machine()], [location(), location({ id: 'L2', label: 'Studio 2' })]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-location').innerHTML;
  assert.match(html, /All locations/);
  assert.match(html, /Blue Room/);
  assert.match(html, /Studio 2/);
});

test('a current attention row shows the cause and measured time', async () => {
  const measuredAt = Math.floor(Date.now() / 1000) - 120;
  const ctx = context(payload([machine({
    state: 'needs_attention',
    attention: 'Last job failed: disk_full',
    attention_source: 'current',
    attention_measured_at: measuredAt,
  })]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.match(html, /Last job failed: disk_full/);
  assert.match(html, /measured \d{1,2}:\d{2}/);
  assert.doesNotMatch(html, /Not checked in since/);
});

test('a last_known attention row shows not-checked-in instead of the fault', async () => {
  const asOf = Math.floor(Date.now() / 1000) - 300;
  const ctx = context(payload([machine({
    state: 'last_known',
    last_known: true,
    attention: 'Flagged by its Hub',
    attention_source: 'last_known',
    attention_measured_at: asOf,
  })]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.match(html, /Not checked in since \d{1,2}:\d{2}/);
  assert.doesNotMatch(html, /Flagged by its Hub/);
});

test('a cleared attention row draws no marker at all', async () => {
  const ctx = context(payload([machine({
    state: 'ready',
    attention: null,
    attention_source: 'current',
    attention_measured_at: Math.floor(Date.now() / 1000),
  })]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.doesNotMatch(html, /msg warn/);
  assert.doesNotMatch(html, /Not checked in since/);
  assert.doesNotMatch(html, /measured \d{1,2}:\d{2}/);
});

test('a silent node under a reachable controller shows not-checked-in and a live disk cell shows its as-of time', async () => {
  const now = Math.floor(Date.now() / 1000);
  const ctx = context(payload([
    machine({
      machine: 'silent-node',
      label: 'Silent Node',
      reachable: false,
      state: 'offline',
      attention: null,
      attention_source: 'last_known',
      attention_measured_at: now - 3600,
    }),
    machine({
      machine: 'live-node',
      label: 'Live Node',
      reachable: true,
      state: 'ready',
      disk_free_gb: 120.5,
      disk_total_gb: 245.1,
      disk: { state: 'green', free_gb: 120.5, total_gb: 245.1, measured_at: now - 120 },
    }),
  ]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.match(html, /Not checked in since \d{1,2}:\d{2}/);
  assert.match(html, /as of \d{1,2}:\d{2}/);
});

