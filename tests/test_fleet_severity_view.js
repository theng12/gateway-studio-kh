/**
 * The Fleet page's colours: red is broken or down, yellow is degraded but running, blue is fine.
 *
 * The owner, 2026-10-30, looking at a Working Mac on Wi-Fi and a Working Mac on a 100 Mb/s link:
 * *"slow link should be yellow and not red. red means it's either broken or down. slow is yellow."*
 * and *"on wifi or sth should be blue i guess. i mean as long as it's still running fine. it
 * shouldn't be red"*.
 *
 * The colour is the `severity` the Gateway states beside each cause code — the page keeps no
 * second list of codes. Unknown or missing severity is neutral, never red. A cause may not look
 * healthier than its subject: on a Mac that is down, an informational cause is secondary text, not
 * blue. The words stay in every case; colour is never the only signal.
 *
 * Run with: `node --test tests/test_fleet_severity_view.js`
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
    observed: true, online: true, observed_since: 1000, observed_at: 1000,
    since: 1000, online_since: 1000, offline_since: null,
    drops_day: 0, drops_week: 0, flips_day: 0, flips_week: 0,
    offline_seconds_day: 0, offline_seconds_week: 0,
    days: [{ date: '2026-10-29', offline_fraction: 0 }],
    pattern: null, problematic: false, last_drop: null, ...overrides,
  };
}

function cause(code, severity, text = `${code} cause`) {
  return { code, severity, text, evidence: `${code} evidence` };
}

function machine(overrides = {}) {
  return {
    machine: '1301', label: '1301', location: 'Blue Room', location_id: 'L1', network: 'A',
    state: 'ready', reachable: true, chip: 'Apple M4', memory_gb: 32, available_gb: 18.4,
    disk_free_gb: 61, disk_total_gb: 460, hub_version: '0.6.126', version_status: 'latest',
    history: history(), causes: [cause('fine', 'none', 'Nothing wrong reported')], ...overrides,
  };
}

function location(overrides = {}) {
  return {
    id: 'L1', label: 'Blue Room', url: 'http://100.64.0.11:47873', reachable: true,
    role: 'member', version: '0.6.126', version_status: 'latest',
    node_summary: { total: 3, reachable: 2, unauthorized: 0 },
    models_publishable: 3, models_discovered: 4, machine_summary: { total: 1 }, machines: [{}],
    history: history(), causes: [cause('fine', 'none', 'Nothing wrong reported')], ...overrides,
  };
}

function payload(machines, locations = []) {
  return {
    summary: { machines: machines.length,
      machines_online: machines.filter(m => m.reachable !== false).length,
      locations: locations.length, locations_up: locations.filter(l => l.reachable !== false).length,
      pulse: { working: 0 } },
    gateway_queue: { waiting: 0, running: 0 },
    locations, machines, running: [], issues: [],
    generated_at: Math.floor(Date.now() / 1000),
    fleet_health: { flapping_drops_day: 4, default: 4, min: 1, max: 100, window_days: 7 },
  };
}

const GROUPS = 'fleet-body';
const TABLE = 'fleet-machines-wrap';
const LOCATIONS = 'fleet-locations-wrap';

test('a slow Ethernet link is yellow in the card and the table, never red', async () => {
  const ctx = context(payload([machine({
    connection: { status: 'measured', kind: 'ethernet', speed_mbps: 100, duplex: 'full',
      slow: true, text: 'Ethernet 100 Mb/s full' },
    causes: [cause('slow_link', 'warn', 'Its Ethernet link runs at 100 Mb/s')],
  })]));
  await ctx.loadFleet();
  const card = ctx.nodes.get(GROUPS).innerHTML;
  assert.match(card, /class="msg warn">Slow link — check cable\/port/);
  assert.match(card, /class="msg warn">Likely cause: Its Ethernet link runs at 100 Mb\/s/);
  assert.doesNotMatch(card, /class="msg bad"[^>]*>Likely cause/);
  assert.doesNotMatch(card, /class="msg bad">Slow link/);
  const table = ctx.nodes.get(TABLE).innerHTML;
  assert.match(table, /style="color:var\(--warn-text\)">Its Ethernet link runs at 100 Mb\/s/);
  assert.doesNotMatch(table, /color:var\(--bad-text\)">Its Ethernet link/);
});

test('a Wi-Fi Mac that is running fine is blue, and its Network badge is blue too', async () => {
  const ctx = context(payload([machine({
    connection: { status: 'measured', kind: 'wifi', speed_mbps: null, slow: false,
      text: 'Wi-Fi 432 Mb/s' },
    causes: [cause('on_wifi', 'info', 'It is on Wi-Fi, which drops in and out more than a cable')],
  })]));
  await ctx.loadFleet();
  const card = ctx.nodes.get(GROUPS).innerHTML;
  assert.match(card, /class="msg info">Likely cause: It is on Wi-Fi/);
  assert.doesNotMatch(card, /class="msg bad"[^>]*>Likely cause/);
  const table = ctx.nodes.get(TABLE).innerHTML;
  assert.match(table, /style="color:var\(--info-text\)">It is on Wi-Fi/);
  // The Network badge itself is blue: Wi-Fi is information, not a warning.
  assert.match(table, /class="badge info">Wi-Fi 432 Mb\/s</);
  assert.doesNotMatch(table, /class="badge warn">Wi-Fi/);
});

test('a cause that is broken or down is still red', async () => {
  const ctx = context(payload([machine({
    reachable: false, state: 'offline',
    history: history({ online: false, offline_since: Math.floor(Date.now() / 1000) - 600 }),
    causes: [cause('asleep_or_off', 'bad', 'It is not answering at all')],
  })]));
  await ctx.loadFleet();
  assert.match(ctx.nodes.get(GROUPS).innerHTML,
    /class="msg bad">Likely cause: It is not answering at all/);
  assert.match(ctx.nodes.get(TABLE).innerHTML,
    /style="color:var\(--bad-text\)">It is not answering at all/);
});

test('a Wi-Fi line on a Mac that is down is honest secondary text, not blue', async () => {
  const ctx = context(payload([machine({
    reachable: false, state: 'offline',
    connection: { status: 'measured', kind: 'wifi', slow: false, text: 'Wi-Fi 432 Mb/s' },
    history: history({ online: false, offline_since: Math.floor(Date.now() / 1000) - 600 }),
    causes: [cause('on_wifi', 'info', 'It is on Wi-Fi, which drops in and out more than a cable')],
  })]));
  await ctx.loadFleet();
  const card = ctx.nodes.get(GROUPS).innerHTML;
  // The words and the red state pill stay; the informational line is not blue.
  assert.match(card, />Offline</);
  assert.match(card, /Likely cause: It is on Wi-Fi/);
  assert.doesNotMatch(card, /class="msg info">Likely cause/);
  assert.doesNotMatch(card, /color:var\(--info-text\)">It is on Wi-Fi/);
});

test('an unknown or missing severity is neutral, never red', async () => {
  const ctx = context(payload([
    machine({ machine: 'a', label: 'a', causes: [{ code: 'some_new_code', text: 'A new hint' }] }),
    machine({ machine: 'b', label: 'b',
      causes: [{ code: 'some_other_code', severity: 'critical', text: 'An odd hint' }] }),
  ]));
  await ctx.loadFleet();
  const card = ctx.nodes.get(GROUPS).innerHTML;
  assert.match(card, /Likely cause: A new hint/);
  assert.match(card, /Likely cause: An odd hint/);
  assert.doesNotMatch(card, /class="msg bad">Likely cause: A new hint/);
  assert.doesNotMatch(card, /class="msg bad">Likely cause: An odd hint/);
  const table = ctx.nodes.get(TABLE).innerHTML;
  assert.doesNotMatch(table, /color:var\(--bad-text\)">A new hint/);
  assert.doesNotMatch(table, /color:var\(--bad-text\)">An odd hint/);
});

test('"Nothing wrong reported" and "No evidence yet" stay plain hints', async () => {
  const ctx = context(payload([
    machine({ machine: 'a', label: 'a' }),
    machine({ machine: 'b', label: 'b', reachable: false, state: 'offline',
      history: history({ online: false, offline_since: Math.floor(Date.now() / 1000) - 60 }),
      causes: [cause('no_evidence', 'none', 'No evidence yet')] }),
  ]));
  await ctx.loadFleet();
  const card = ctx.nodes.get(GROUPS).innerHTML;
  assert.match(card, /class="hint">Likely cause: Nothing wrong reported/);
  assert.match(card, /class="hint">Likely cause: No evidence yet/);
});

test('a degraded-but-running Mac is yellow: its attention line and its chips', async () => {
  const ctx = context(payload([machine({
    state: 'needs_attention', attention: 'Music studio not running',
    history: history({ drops_day: 6, drops_week: 20, problematic: true }),
    causes: [{ code: 'fine', text: 'Nothing wrong reported', severity: 'none' }],
  })]));
  await ctx.loadFleet();
  assert.match(ctx.nodes.get(GROUPS).innerHTML, /class="msg warn">Music studio not running/);
  const summary = ctx.nodes.get('fleet-summary').innerHTML;
  assert.match(summary, /badge warn[^>]*>1 problematic/);
  assert.match(ctx.nodes.get(TABLE).innerHTML, /class="badge warn"[^>]*>Problematic</);
});

test('"N on slow Ethernet" is yellow, and so is the table status line', async () => {
  const d = payload([machine()]);
  d.summary.connections = { slow_ethernet: 2, slow_machines: ['a', 'b'], unmeasured: 0, total: 2 };
  const ctx = context(d);
  await ctx.loadFleet();
  assert.match(ctx.nodes.get('fleet-summary').innerHTML, /class="badge warn"[^>]*>2 on slow Ethernet/);
  assert.match(ctx.nodes.get('fleet-status').innerHTML, /class="badge warn"[^>]*>2 on slow Ethernet/);
});

test('a site cause is coloured by its own severity, and a down controller is red', async () => {
  const ctx = context(payload([], [
    location({ reachable: false, error: 'timed out',
      causes: [cause('controller_silent', 'bad', 'The site\'s address answered nothing in time')] }),
  ]));
  await ctx.loadFleet();
  assert.match(ctx.nodes.get(GROUPS).innerHTML,
    /class="msg bad">Likely cause: The site&#39;s address answered nothing in time/);
  assert.match(ctx.nodes.get(LOCATIONS).innerHTML,
    /style="color:var\(--bad-text\)">The site&#39;s address answered nothing in time/);
});
