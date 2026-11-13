/**
 * The Fleet page's one sortable list: offline, problematic, and the likely cause beside it.
 *
 * The owner, 2026-10-29: *"i want to be able to sort for the offline site. and also 'problematic'
 * machine … and also a way for me to see which ones could be the likely cause"*.
 *
 * What this pins, in the page's own code (a VM over `admin_ui.html`, never the app or a live
 * gateway):
 *
 * 1. **One list holds machines and sites**, so a site that is down is a row, not a heading with
 *    nothing under it.
 * 2. **The filter really filters**: "Offline now" and "Problematic" each leave only what they name,
 *    and an empty result says how many were read rather than looking like an empty fleet.
 * 3. **`problematic` is the Gateway's verdict**, drawn as sent — the page must not re-decide it from
 *    a number it invents.
 * 4. **Sorting by drops, site and last-seen reorders the same rows** (not a second fetch).
 * 5. **The likely cause is in plain words, with its evidence kept in the tooltip**, and a figure
 *    nobody measured says so instead of drawing a healthy bar.
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
  const nodes = new Map([
    ['fleet-body', element()], ['fleet-summary', element()], ['fleet-issues', element()],
    ['fleet-filter', element()], ['fleet-threshold', element()],
    ['fleet-machines-wrap', element()], ['fleet-status', element()], ['fleet-warnings', element()],
    ['fleet-locations-wrap', element()], ['fleet-locations-status', element()],
    ['fleet-models-wrap', element()], ['fleet-models-status', element()],
    ['fleet-jobs-wrap', element()], ['fleet-jobs-status', element()], ['fleet-jobs-problems', element()],
    ['fleet-tabs', element()], ['fleet-search', element()], ['fleet-location', element()],
    ['fleet-drops', element()], ['fleet-threshold-note', element()],
  ]);
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
      needs_attention: ['warn', 'Needs attention'], offline: ['bad', 'Offline'],
      ready: ['ok', 'Ready'],
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
    observed: true, online: true, observed_since: 1000, observed_at: Date.now() / 1000,
    since: 1000, online_since: 1000, offline_since: null,
    drops_day: 0, drops_week: 0, flips_day: 0, flips_week: 0,
    offline_seconds_day: 0, offline_seconds_week: 0,
    days: [
      { date: '2026-10-23', offline_fraction: 0 },
      { date: '2026-10-24', offline_fraction: 0.25 },
      { date: '2026-10-25', offline_fraction: null },
    ],
    pattern: null, problematic: false, last_drop: null, ...overrides,
  };
}

function machine(overrides = {}) {
  return {
    machine: '1301', label: '1301', location: 'Blue Room', location_id: 'L1',
    state: 'ready', reachable: true, hub_version: '0.6.206',
    history: history(), causes: [{ code: 'fine', text: 'Nothing wrong reported', evidence: null }],
    ...overrides,
  };
}

function payload(machines, locations = []) {
  return {
    summary: { machines: machines.length, machines_online: machines.filter(m => m.reachable).length,
      locations: locations.length, locations_up: locations.filter(l => l.reachable).length },
    gateway_queue: { waiting: 0, running: 0 },
    locations, machines, issues: [], generated_at: Math.floor(Date.now() / 1000),
    fleet_health: { flapping_drops_day: 4, default: 4, min: 1, max: 100, window_days: 7 },
  };
}

const OFFLINE_HUB = [{
  code: 'hub_not_running',
  text: 'Studio Hub is not running on this Mac — the Mac is awake and answered.',
  evidence: 'Its controller reached the Mac but found nothing listening on the Hub port (no_hub).',
}];

test('one list holds both sites and machines', async () => {
  const ctx = context(payload(
    [machine(), machine({ machine: '1207', label: '1207' })],
    [{ id: 'L1', label: 'Blue Room', reachable: true, history: history(),
       causes: [{ code: 'fine', text: 'Nothing wrong reported' }] }]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-body').innerHTML;
  assert.match(html, /Blue Room/);
  assert.match(html, />Site</);
  assert.match(html, /1301/);
  assert.match(html, /1207/);
  // One grid, not one card per location wrapping its own Macs.
  assert.equal((html.match(/class="fleet-macs"/g) || []).length, 1);
});

test('"Offline now" leaves only what is offline, sites included', async () => {
  const ctx = context(payload(
    [machine(), machine({ machine: '1108', label: '1108', reachable: false, state: 'offline',
                          history: history({ online: false, offline_since: Date.now() / 1000 - 7200,
                                             drops_day: 1 }),
                          causes: OFFLINE_HUB })],
    [{ id: 'L2', label: 'Blue Room 2', reachable: false, history: history({ online: false }),
       causes: [{ code: 'controller_silent', text: 'The site answered nothing in time.', evidence: 'No answer within 6s.' }] }]));
  await ctx.loadFleet();
  ctx.fleetSetFilter('offline');
  const html = ctx.nodes.get('fleet-body').innerHTML;
  assert.match(html, /1108/);
  assert.match(html, /Blue Room 2/);
  assert.doesNotMatch(html, /1301/);
});

test('problematic is the Gateway verdict, drawn as sent', async () => {
  const flagged = machine({ machine: '1108', label: '1108',
    history: history({ drops_day: 5, drops_week: 9, problematic: true }),
    causes: [{ code: 'flapping', text: 'It keeps dropping out — 5 drops in the last 24 h.',
               evidence: 'The threshold is 4 drops in 24 h; this Mac has 9 in the last 7 days.' }] });
  // A Mac with a low drop count is NOT re-judged here, whatever the threshold: the Gateway decided.
  const quiet = machine({ machine: '1301', label: '1301',
    history: history({ drops_day: 3, drops_week: 3, problematic: false }) });
  const ctx = context(payload([flagged, quiet]));
  await ctx.loadFleet();
  ctx.fleetSetFilter('problematic');
  const html = ctx.nodes.get('fleet-body').innerHTML;
  assert.match(html, /1108/);
  assert.doesNotMatch(html, /1301/);
  assert.match(html, /Problematic/);
});

test('an empty filter says how many were read instead of looking like an empty fleet', async () => {
  const ctx = context(payload([machine()]));
  await ctx.loadFleet();
  ctx.fleetSetFilter('problematic');
  const html = ctx.nodes.get('fleet-body').innerHTML;
  assert.match(html, /Nothing matches/);
  assert.match(html, /1 machine and site/);
});

test('sorting by drops puts the flappiest first', async () => {
  const ctx = context(payload([
    machine({ machine: 'a', label: 'a', history: history({ drops_day: 1 }) }),
    machine({ machine: 'c', label: 'c', history: history({ drops_day: 6 }) }),
    machine({ machine: 'b', label: 'b', history: history({ drops_day: 3 }) }),
  ]));
  await ctx.loadFleet();
  ctx.fleetSetSort('drops');
  const html = ctx.nodes.get('fleet-body').innerHTML;
  assert.ok(html.indexOf('>c<') < html.indexOf('>b<'), 'drops descending');
  assert.ok(html.indexOf('>b<') < html.indexOf('>a<'), 'drops descending');
});

test('sorting by site groups the Macs of one site together', async () => {
  const ctx = context(payload([
    machine({ machine: 'm1', label: 'm1', location: 'Zebra' }),
    machine({ machine: 'm2', label: 'm2', location: 'Alpha' }),
  ]));
  await ctx.loadFleet();
  ctx.fleetSetSort('site');
  const html = ctx.nodes.get('fleet-body').innerHTML;
  assert.ok(html.indexOf('>m2<') < html.indexOf('>m1<'), 'Alpha before Zebra');
});

test('sorting by last seen puts the longest unseen first', async () => {
  const now = Date.now() / 1000;
  const ctx = context(payload([
    machine({ machine: 'fresh', label: 'fresh', history: history({ observed_at: now }) }),
    machine({ machine: 'stale', label: 'stale', reachable: false, state: 'offline',
              history: history({ online: false, offline_since: now - 3 * 86400 }) }),
  ]));
  await ctx.loadFleet();
  ctx.fleetSetSort('lastseen');
  const html = ctx.nodes.get('fleet-body').innerHTML;
  assert.ok(html.indexOf('>stale<') < html.indexOf('>fresh<'), 'three days unseen comes first');
});

test('the likely cause is plain words, its evidence in the tooltip', async () => {
  const ctx = context(payload([machine({ machine: '1108', label: '1108', reachable: false,
    state: 'offline', causes: OFFLINE_HUB })]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-body').innerHTML;
  assert.match(html, /Likely cause: Studio Hub is not running on this Mac/);
  assert.match(html, /title="Its controller reached the Mac[^"]*no_hub/);
});

test('a machine with nothing to go on says "No evidence yet", never a guess', async () => {
  const ctx = context(payload([machine({ machine: '1108', label: '1108', reachable: false,
    state: 'offline', causes: [{ code: 'no_evidence', text: 'No evidence yet',
                                 evidence: 'Nothing this Mac reports points at a cause.' }] })]));
  await ctx.loadFleet();
  assert.match(ctx.nodes.get('fleet-body').innerHTML, /No evidence yet/);
});

test('the seven-day strip fills the days it knows and hatches the day nobody saw', async () => {
  const ctx = context(payload([machine({ history: history() })]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-body').innerHTML;
  assert.match(html, /fleet-week/);
  assert.equal((html.match(/class="fleet-day"/g) || []).length, 3);
  assert.match(html, /2026-10-24: 25% offline/);
  assert.match(html, /2026-10-25: not recorded/);
  assert.match(html, /repeating-linear-gradient/);
});

test('a machine this Gateway has no history for says so, not "fine"', async () => {
  const ctx = context(payload([machine({
    history: { observed: false, drops_day: null, drops_week: null, offline_seconds_week: null,
               days: [{ date: '2026-10-25', offline_fraction: null }], problematic: null },
    causes: [{ code: 'fine', text: 'Nothing wrong reported', evidence: null }],
  })]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-body').innerHTML;
  assert.match(html, /Not measured yet/);
  // **No history means no figure.** "Nobody looked" must not draw as "0 drops".
  assert.doesNotMatch(html, /0 drops in 24 h/);
  assert.doesNotMatch(html, /offline in 7 days/);
  assert.match(html, /not recorded/);
});

test('every figure in the caption names its own window', async () => {
  // Live bug in 0.2.209: "5 in 24 h · 42 h off", where the count covered 24 h and the offline
  // time covered seven days, with nothing saying so (Production, 2026-10-30).
  const ctx = context(payload([machine({ label: 'a',
    history: history({ drops_day: 5, drops_week: 9, offline_seconds_week: 151200 }) })]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-body').innerHTML;
  assert.match(html, /5 drops in 24 h/);
  assert.match(html, /9 drops in 7 days/);
  assert.match(html, /42 h offline in 7 days/);
  assert.doesNotMatch(html, /· 9 in 7 days/);
});

test('the strip tooltip carries how long its drops lasted, and how often they come', async () => {
  const ctx = context(payload([machine({ label: 'a', history: history({
    drops_day: 3, drops_week: 3, drop_median_seconds: 120, drop_longest_seconds: 600,
    drop_gap_seconds: 480, drops_completed: 3,
    last_drop: { at: 1, reason: 'timeout', text: 'the connection timed out',
                 duration_seconds: 120, ongoing: false, recovered_by: 'by_itself' },
  }) })]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-body').innerHTML;
  assert.match(html, /Drops up to 10 min, median 2 min/);
  assert.match(html, /about every 8 min/);
  assert.match(html, /last recovery: by itself/);
});

test('a drop length nobody measured says so instead of reading as short', async () => {
  const ctx = context(payload([machine({ label: 'a', history: history({ drops_day: 2 }) })]));
  await ctx.loadFleet();
  assert.match(ctx.nodes.get('fleet-body').innerHTML, /Drop length not measured/);
});

test('the summary counts problematic from the rows below it', async () => {
  const ctx = context(payload([
    machine({ machine: 'a', label: 'a', history: history({ drops_day: 5, problematic: true }) }),
    machine({ machine: 'b', label: 'b', history: history({ drops_day: 1, problematic: false }) }),
  ]));
  await ctx.loadFleet();
  assert.match(ctx.nodes.get('fleet-summary').innerHTML, /1 problematic/);
});

test('the Machines tab is the native table, sorted from its own headers', async () => {
  // The native page has no Sort dropdown: a header click is the sort. The browser page keeps
  // that, so the columns and their order are the native ones.
  const ctx = context(payload([machine(), machine({ machine: '1207', label: '1207' })]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  for (const heading of ['Machine', 'State', 'Health', 'Seen', 'Drops · 7 days',
    'Likely cause', 'Version', 'Disk', 'Memory', 'Network']) {
    assert.match(html, new RegExp(heading.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  }
  for (const key of ['machine', 'health', 'week', 'cause', 'disk', 'memory', 'network']) {
    assert.match(html, new RegExp(`fleetSortBy\\('${key}'\\)`));
  }
});

test('the summary figure is a button that shows only the problematic ones', async () => {
  // A span with an onclick is not focusable — `tests/test_admin_accessibility.js` fails on it, and a
  // keyboard cannot reach it. The figure is a real button, and pressing it applies the filter.
  const ctx = context(payload([
    machine({ machine: 'a', label: 'a', history: history({ drops_day: 5, problematic: true }) }),
    machine({ machine: 'b', label: 'b' }),
  ]));
  await ctx.loadFleet();
  const summary = ctx.nodes.get('fleet-summary').innerHTML;
  assert.match(summary, /<button[^>]*onclick="fleetOnlyProblematic\(\)"/);
  assert.match(summary, /1 problematic/);
  ctx.fleetOnlyProblematic();
  const html = ctx.nodes.get('fleet-body').innerHTML;
  assert.match(html, />a</);
  assert.doesNotMatch(html, />b</);
});
