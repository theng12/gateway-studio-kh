/**
 * Orphaned Hub batches in the browser console: the warning line, what each batch produced, and
 * the Stop button the Gateway's guard allows.
 *
 * The incident (2026-10-29): a site restart left three **old** Hub batches running duplicate work
 * after the Gateway had re-dispatched the work elsewhere and its own jobs had finished. The old
 * batch ids survive only in the job's attempt log; the Gateway classifies them (see
 * `gateway/orphans.py`) and this page draws that answer.
 *
 * What this pins, in the page's own code:
 *
 * 1. The warning line names the count and lists each batch with its site, modality and running time.
 * 2. A figure the Hub did not send reads "not reported", never 0.
 * 3. `orphan_unknown` gets **no Stop button**, only a plain note.
 * 4. Stop posts `orphan_stop: true` to the existing cancel route and says "signalled — not yet
 *    stopped", because that is what the Hub's answer means.
 * 5. A site that did not answer reads "not checked", never "no orphaned batches".
 * 6. Automatic stop is a switch that is off unless the owner turns it on.
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

function context(snapshot, answer = { queued_cancelled: 2, running_signalled: 1, running_pending: 0 }) {
  const nodes = new Map([
    ['fleet-body', element()], ['fleet-summary', element()], ['fleet-issues', element()],
    ['fleet-filter', element()], ['fleet-sort', element()], ['fleet-threshold', element()],
    ['fleet-orphan-setting', element()], ['fleet-orphan-auto', element()],
    ['fleet-orphan-note', element()], ['fleet-orphans', element()],
    // The merged Fleet page is one tab strip over several panes; every renderer writes its own
    // node on each load, so the harness holds them all.
    ['fleet-tabs', element()], ['fleet-warnings', element()], ['fleet-machines-wrap', element()],
    ['fleet-status', element()], ['fleet-locations-wrap', element()],
    ['fleet-locations-status', element()], ['fleet-models-wrap', element()],
    ['fleet-models-status', element()], ['fleet-jobs-wrap', element()],
    ['fleet-jobs-status', element()], ['fleet-jobs-problems', element()],
    ['fleet-search', element()], ['fleet-location', element()], ['fleet-drops', element()],
    ['fleet-threshold-note', element()],
  ]);
  const calls = [];
  const ctx = vm.createContext({
    document: { getElementById: id => nodes.get(id), querySelector: () => null, activeElement: null },
    window: {}, esc, ago: () => 'just now', FLEET_STATES: {},
    state: { view: 'fleet' }, nodes, calls,
    api: async (path, opts) => {
      calls.push({ path, opts });
      return /\/cancel$/.test(path) ? { result: answer } : snapshot;
    },
    Date, Number, Math,
  });
  vm.runInContext(between("let fleetFilter = 'all';", 'async function loadJobs()'), ctx);
  return ctx;
}

function orphan(overrides = {}) {
  return {
    batch_id: 'b-old', label: 'gateway-kh', modality: 'image', model: 'qwen-image',
    started_at: 1000, running_seconds: 600,
    progress: { done: 1, total: 4, queued: 2, running: 1, retrying: 0,
                artifact_count: null, output_path: null },
    classification: 'orphan_superseded',
    job: { id: 'job-1', state: 'succeeded', outcome: 'succeeded', superseded_by: 'b-new',
           artifacts: 1, artifacts_on_disk: 1, artifacts_complete: true },
    stop: { allowed: true, reason: 'Its Gateway job succeeded and all 1 artifact(s) are on disk.' },
    ...overrides,
  };
}

function site(orphans, extra = {}) {
  return { id: 'L1', label: 'Blue Room', reachable: true, jobs_read: true,
           orphans_checked: true, orphan_batches: orphans, orphans_note: null, ...extra };
}

function payload(locations, extra = {}) {
  const count = locations.reduce((sum, L) => sum + (L.orphan_batches || []).length, 0);
  return {
    generated_at: Date.now() / 1000,
    summary: { machines: 0, machines_online: 0, locations: locations.length, locations_up: 1 },
    gateway_queue: { waiting: 0, running: 0 }, issues: [], machines: [], locations,
    fleet_health: { flapping_drops_day: 4 },
    orphan_batches: count,
    orphans_checked_sites: locations.filter(L => L.orphans_checked !== false).length,
    orphans_unchecked_sites: locations.filter(L => L.orphans_checked === false).length,
    orphans_auto_stop: false,
    ...extra,
  };
}

test('the warning line names the count and lists each orphan with its running time', async () => {
  const ctx = context(payload([site([orphan()])]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-orphans').innerHTML;
  assert.match(html, /1 Hub batch still running for work that already finished/);
  assert.match(html, /Blue Room · batch b-old · image · running 10 min/);
  assert.match(html, /replaced by batch b-new/);
});

test('a figure the Hub did not send reads "not reported", never 0', async () => {
  const ctx = context(payload([site([orphan()])]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-orphans').innerHTML;
  assert.match(html, /1\/4 items done/);
  assert.match(html, /artifacts not reported/);
  assert.doesNotMatch(html, /0 artifacts/);
});

test('a guard that does not hold shows its reason and no Stop button', async () => {
  const ctx = context(payload([site([orphan({
    job: { state: 'succeeded', superseded_by: null, artifacts: 0, artifacts_on_disk: 0 },
    stop: { allowed: false, reason: 'Its Gateway job succeeded but kept no artifact.' },
  })])]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-orphans').innerHTML;
  assert.match(html, /kept no artifact/);
  assert.doesNotMatch(html, />Stop</);
});

test('an unknown orphan gets no Stop, only a plain note', async () => {
  const ctx = context(payload([site([orphan({
    classification: 'orphan_unknown', job: null,
    stop: { allowed: false, reason: 'No Gateway job names this batch, so the Gateway cannot prove its work was kept.' },
  })])]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-orphans').innerHTML;
  assert.match(html, /No Gateway job/);
  assert.match(html, /cannot prove its work was kept/);
  assert.doesNotMatch(html, />Stop</);
});

test('Stop posts the orphan flag and says a signalled worker is not yet stopped', async () => {
  const ctx = context(payload([site([orphan()])]));
  await ctx.loadFleet();
  const button = { dataset: { site: 'L1', batch: 'b-old', modality: 'image' },
                   parentElement: { querySelector: () => null }, disabled: false };
  await ctx.stopOrphan(button);
  const sent = ctx.calls.filter(call => /\/cancel$/.test(call.path));
  assert.equal(sent.length, 1);
  assert.match(sent[0].path, /^\/fleet\/controllers\/L1\/jobs\/b-old\/cancel$/);
  assert.equal(JSON.parse(sent[0].opts.body).orphan_stop, true);
  assert.equal(JSON.parse(sent[0].opts.body).modality, 'image');
  const html = ctx.nodes.get('fleet-orphans').innerHTML;
  assert.match(html, /running signalled — not yet stopped/);
});

test('a site that timed out reads "not checked", never "no orphaned batches"', async () => {
  const ctx = context(payload([
    site(null, { reachable: false, jobs_read: null, orphans_checked: false,
                 orphan_batches: null, orphans_note: 'not checked: the site did not answer.' }),
  ]));
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-orphans').innerHTML;
  assert.match(html, /Not checked/);
  assert.match(html, /did not answer/);
  assert.doesNotMatch(html, /No orphaned Hub batches/);
});

test('the page says automatic stop is off unless the Gateway says otherwise', async () => {
  const ctx = context(payload([]));
  await ctx.loadFleet();
  // The control is written off by default in the page itself, so it reads correctly before any
  // Gateway read arrives.
  assert.match(source, /Orphaned Hub batches: automatic stop is off/);
});

test('turning automatic stop on posts a real boolean and never a string', async () => {
  const ctx = context(payload([]));
  await ctx.loadFleet();
  await ctx.saveOrphanAutoStop(true);
  const sent = ctx.calls.filter(call => call.path === '/settings/fleet-orphans');
  assert.equal(sent.length, 1);
  assert.equal(JSON.parse(sent[0].opts.body).auto_stop, true);
});
