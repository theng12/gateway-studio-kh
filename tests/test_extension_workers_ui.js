// The Browser workers pane (Providers > Browser workers), run in a VM over the page's own source.
//
// What it must not get wrong: a switched-off Gateway (404) reads "off", not an error; unreported
// credits read "unknown", never 0; server text is escaped; pairing and unpairing are drawn only
// in this Mac's app; the token is shown once and dropped on Done; an unknown pairing outcome
// says so instead of inviting a second Pair (which would revoke the first token).

const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
const start = source.indexOf('function ewAgo(');
const end = source.indexOf('// Pools -- one name', start);
assert.ok(start > 0 && end > start, 'admin_ui.html moved: the Browser workers block');
const block = source.slice(start, end);

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function el(value = '') {
  return { value, innerHTML: '', textContent: '', className: '', disabled: false, style: {}, dataset: {} };
}

function page({ inApp = true, answers = [] } = {}) {
  const nodes = Object.fromEntries(['ew-workers', 'ew-pair', 'ew-token', 'ew-token-value', 'ew-msg',
    'ew-worker-id'].map(id => [id, el()]));
  const calls = [];
  const context = {
    esc, IN_APP: inApp, Date, JSON, Math,
    document: { getElementById: id => nodes[id] || null },
    api: async (p, opts = {}) => {
      calls.push([p, opts.method || 'GET', opts.body]);
      const next = answers.shift();
      if (next instanceof Error) throw next;
      return next;
    },
  };
  vm.createContext(context);
  vm.runInContext(block, context);
  return { context, nodes, calls };
}

function failure(status, message = 'x', ambiguous = false) {
  return Object.assign(new Error(message), { status, ambiguous });
}

const WORKER = {
  worker_id: 'chrome-<a>', kind: 'video', has_live_token: true, online: true,
  last_seen_at: Date.now() / 1000 - 5, credits: null, extension_version: null, queue_depth: 0,
  current_job: null, held_jobs: [], created_at: 1, revoked_at: null,
};

test('a switched-off Gateway reads off, not an error, and hides pairing', async () => {
  const { context, nodes } = page({ answers: [failure(404, 'Not Found')] });
  await context.loadExtensionWorkers();
  assert.match(nodes['ew-workers'].innerHTML, /switched off/);
  assert.doesNotMatch(nodes['ew-workers'].innerHTML, /Could not/);
  assert.equal(nodes['ew-pair'].style.display, 'none');
});

test('unreported credits read unknown, server text is escaped, unpair only in the app', async () => {
  const app = page({ answers: [{ workers: [WORKER] }] });
  await app.context.loadExtensionWorkers();
  const html = app.nodes['ew-workers'].innerHTML;
  assert.match(html, /unknown — not reported yet/);
  assert.doesNotMatch(html, /chrome-<a>/);
  assert.match(html, /chrome-&lt;a&gt;/);
  assert.match(html, /Unpair/);
  assert.equal(app.nodes['ew-pair'].style.display, '');

  const browser = page({ inApp: false, answers: [{ workers: [WORKER] }] });
  await browser.context.loadExtensionWorkers();
  assert.doesNotMatch(browser.nodes['ew-workers'].innerHTML, /Unpair/);
  assert.equal(browser.nodes['ew-pair'].style.display, 'none');
});

test('stated credits, a current job and held jobs are drawn', () => {
  const { context } = page();
  const html = context.ewCard({
    ...WORKER, credits: { value: 0, source: 'flow.nzlxg', observedAt: '2026-11-09T10:00:00Z' },
    current_job: { jobId: 'job_1', phase: 'generating', message: null }, held_jobs: ['job_0'],
  });
  assert.match(html, /0 Flow credits/);   // a stated 0 is a claim, drawn as 0
  assert.match(html, /job_1 — generating/);
  assert.match(html, /Held, outcome unknown: job_0/);
});

test('the token is shown once and dropped on Done', async () => {
  const { context, nodes, calls } = page({ answers: [{ ok: true, worker_id: 'w', token: 'ekw_once' }, { workers: [] }] });
  nodes['ew-worker-id'].value = ' w ';
  await context.ewPair(el());
  assert.deepEqual(calls[0], ['/extension-workers', 'POST', JSON.stringify({ worker_id: 'w', kind: 'video' })]);
  assert.equal(nodes['ew-token-value'].value, 'ekw_once');
  assert.doesNotMatch(nodes['ew-msg'].textContent, /ekw_/);
  context.ewTokenDone();
  assert.equal(nodes['ew-token-value'].value, '');
  assert.equal(nodes['ew-token'].style.display, 'none');
});

test('an unknown pairing outcome says so and shows no token', async () => {
  const { context, nodes } = page({ answers: [failure(502, 'gateway gone', true)] });
  nodes['ew-worker-id'].value = 'w';
  await context.ewPair(el());
  assert.match(nodes['ew-msg'].textContent, /Not known whether pairing happened/);
  assert.equal(nodes['ew-token-value'].value, '');
});

test('unpair calls DELETE with the id encoded', async () => {
  const { context, calls } = page({ answers: [{ ok: true, revoked: 1 }, { workers: [] }] });
  await context.ewRevoke('a/b', el());
  assert.deepEqual(calls[0].slice(0, 2), ['/extension-workers/a%2Fb', 'DELETE']);
});
