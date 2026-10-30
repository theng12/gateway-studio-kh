/**
 * "Recent automatic stops" on the Fleet page: what each stop was, why it was allowed, which switch
 * did it, and what the Hub answered.
 *
 * **Why (0.2.212).** An automatic stop nobody can see afterwards is worse than none. The stops were
 * already written to the Gateway's audit; the page now draws them, beside the two switches that
 * make them, and a Stop the owner pressed appears in the same list marked **manual**.
 *
 * What this pins, in the page's own code:
 *
 * 1. A row names its site, Hub batch, Gateway job, the guard's reason, and the switch that did it.
 * 2. The Hub's own counts are drawn, and "signalled" is said in full — asked to stop, not yet
 *    confirmed.
 * 3. A manual stop is marked manual, so it never reads as automatic.
 * 4. An unreadable audit reads "not available", never "nothing was stopped"; an empty list says it
 *    is empty.
 * 5. The time comes from the row, rendered by the page's own stamp helper.
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

function context(answer) {
  const nodes = new Map([
    ['fleet-auto-stops', element()],
    // Every renderer in the copied range writes its own node on each load; the harness holds them.
    ['fleet-body', element()], ['fleet-summary', element()], ['fleet-orphans', element()],
    ['fleet-filter', element()], ['fleet-sort', element()], ['fleet-threshold', element()],
    ['fleet-orphan-setting', element({ querySelector: () => element() })],
    ['fleet-orphan-auto', element()], ['fleet-orphan-superseded', element()],
    ['fleet-orphan-note', element()], ['fleet-orphan-superseded-note', element()],
    ['fleet-tabs', element()], ['fleet-jobs-wrap', element()], ['fleet-jobs-problems', element()],
    ['fleet-jobs-status', element()], ['fleet-location', element()], ['fleet-search', element()],
  ]);
  const calls = [];
  const ctx = vm.createContext({
    document: { getElementById: id => nodes.get(id), querySelector: () => null, activeElement: null },
    window: {}, esc, ago: () => 'just now', FLEET_STATES: {},
    state: { view: 'fleet' }, nodes, calls,
    api: async (path) => {
      calls.push({ path });
      return answer;
    },
    confirm: () => true,
    Date, Number, Math, isFinite,
  });
  vm.runInContext(between("let fleetFilter = 'all';", 'async function loadJobs()'), ctx);
  return ctx;
}

const AT = 1_760_000_000;  // a fixed instant, so the rendered stamp is the row's own time

function stop(overrides = {}) {
  return {
    at: AT, site: 'studio-hub-L1', site_label: 'Blue Room', batch: 'b-old', job: 'job-1',
    modality: 'image', reason: 'Its Gateway job succeeded and all 1 artifact(s) are on disk.',
    switch: 'superseded_stop', requested_by: 'gateway-auto', ok: true, error: null,
    hub: { queued_cancelled: 0, running_signalled: 1, running_pending: 0 },
    ...overrides,
  };
}

function payload(stops, extra = {}) {
  return { available: true, stops, count: stops.length, days: 7, limit: 200, note: null, ...extra };
}

test('a stop names its site, batch, job, reason and the switch that did it', async () => {
  const ctx = context(payload([stop()]));
  await ctx.loadAutoStops();
  const html = ctx.nodes.get('fleet-auto-stops').innerHTML;
  assert.match(html, /superseded_stop: a copy this Gateway replaced/);
  assert.match(html, /Blue Room · batch b-old · image/);
  assert.match(html, /Gateway job job-1/);
  assert.match(html, /Why: Its Gateway job succeeded and all 1 artifact\(s\) are on disk\./);
  // The time is the row's own, drawn by the page's stamp helper (the same Date, same locale).
  assert.ok(html.includes(esc(new Date(AT * 1000).toLocaleString())), html);
});

test('the Hub answer is drawn in full, and signalled is not "stopped"', async () => {
  const ctx = context(payload([stop()]));
  await ctx.loadAutoStops();
  const html = ctx.nodes.get('fleet-auto-stops').innerHTML;
  assert.match(html, /0 queued cancelled/);
  assert.match(html, /1 running asked to stop, not yet confirmed/);
  assert.match(html, /0 still pending/);
  assert.doesNotMatch(html, /1 running signalled — not yet stopped/);
});

test('a stop the owner pressed is marked manual, never automatic', async () => {
  const ctx = context(payload([stop({ switch: 'manual', requested_by: 'admin' })]));
  await ctx.loadAutoStops();
  const html = ctx.nodes.get('fleet-auto-stops').innerHTML;
  assert.match(html, /manual stop/);
  assert.doesNotMatch(html, /auto_stop: an orphaned Hub batch/);
});

test('an unreadable audit reads not available, never an empty list', async () => {
  const ctx = context({ available: false, stops: null, count: null, days: 7, limit: 200,
                        note: 'not available: the audit log could not be read (PermissionError).' });
  await ctx.loadAutoStops();
  const html = ctx.nodes.get('fleet-auto-stops').innerHTML;
  assert.match(html, /not available/);
  assert.match(html, /could not be read/);
  assert.doesNotMatch(html, /No automatic stop/);
});

test('an audit with nothing in range says so plainly', async () => {
  const ctx = context(payload([], { count: 0 }));
  await ctx.loadAutoStops();
  assert.match(ctx.nodes.get('fleet-auto-stops').innerHTML, /No automatic stop in the last 7 days\./);
});

test('the list is read from the Gateway, bounded, and cached until refreshed', async () => {
  const ctx = context(payload([stop(), stop({ switch: 'auto_stop', batch: 'b-new' })]));
  await ctx.loadAutoStops();
  assert.match(ctx.calls[0].path, /\/fleet\/startup\/auto-stops\?days=7&limit=200$/);
  assert.match(ctx.nodes.get('fleet-auto-stops').innerHTML, /2 stops in the last 7 days, newest first/);
  await ctx.loadAutoStops();               // cached: no second read
  assert.equal(ctx.calls.length, 1);
  await ctx.loadAutoStops(true);           // the Refresh button
  assert.equal(ctx.calls.length, 2);
});

test('the replaced-copy switch starts off, and an absent value reads off', async () => {
  // 0.2.213: both switches start off, so "the Gateway said nothing" must never draw as on.
  assert.match(source, /let fleetOrphanSupersededStop = false;/);
  assert.match(source, /fleetOrphanSupersededStop = d\.orphans_superseded_stop === true;/);
  assert.doesNotMatch(source, /superseded_stop !== false/);
  const tag = between('id="fleet-orphan-superseded"', 'onchange="saveOrphanSupersededStop');
  assert.doesNotMatch(tag, /checked/, 'the markup must not draw it on before a Gateway read');
  assert.match(source, /Off by default\. When a site went/);
});
