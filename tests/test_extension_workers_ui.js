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
  return { value, innerHTML: '', textContent: '', className: '', disabled: false, style: {}, dataset: {},
    type: 'text', setAttribute() {}, getAttribute() { return null; } };
}

function page({ inApp = true, answers = [] } = {}) {
  const nodes = Object.fromEntries(['ew-workers', 'ew-pair', 'ew-token', 'ew-token-value', 'ew-token-label',
    'ew-msg', 'ew-worker-id', 'ew-kind', 'ew-manager', 'ew-manager-id', 'ew-accounts'].map(id => [id, el()]));
  const calls = [];
  const scope = [];
  const context = {
    esc, IN_APP: inApp, Date, JSON, Math,
    document: {
      getElementById: id => nodes[id] || null,
      querySelectorAll: selector => selector === '[data-ew-scope]:checked' ? scope : [],
    },
    api: async (p, opts = {}) => {
      calls.push([p, opts.method || 'GET', opts.body]);
      const next = answers.shift();
      if (next instanceof Error) throw next;
      return next;
    },
  };
  vm.createContext(context);
  vm.runInContext(block, context);
  return { context, nodes, calls, scope };
}

function failure(status, message = 'x', ambiguous = false) {
  return Object.assign(new Error(message), { status, ambiguous });
}

test('the rotate consequence sits beside the button, not only after clicking', () => {
  const { context } = page();
  const html = context.ewCard(WORKER);
  assert.ok(html.includes('Rotate turns dispatch off'), 'the visible hint is missing');
  assert.ok(html.includes('title="Issues a new token'), 'the button has no title');
});

test('a minted token is masked, and Show reveals it without losing the value', async () => {
  const { context, nodes } = page();
  const field = nodes['ew-token-value'];
  field.value = 'ekw_secret';
  field.type = 'password';
  const btn = el();
  context.ewTokenReveal(btn);
  assert.equal(field.type, 'text');
  assert.equal(btn.textContent, 'Hide');
  assert.equal(field.value, 'ekw_secret');
  context.ewTokenReveal(btn);
  assert.equal(field.type, 'password');
  assert.equal(btn.textContent, 'Show');
});

test('the page says plainly that a token cannot be read again', () => {
  assert.ok(source.includes('stored as a hash'), 'the hash explanation is missing');
  assert.ok(source.includes('never read again'), 'the reason it cannot be re-shown is missing');
  assert.ok(source.includes('Rotate'), 'no rotate path is offered');
});

test('Rotate posts the same worker and kind, shows a new token, and says dispatch is off', async () => {
  const { context, calls, nodes } = page({ answers: [{ token: 'ekw_new' }] });
  const btn = el();
  await context.ewRotate('chrome-<a>', 'video', btn);
  assert.deepEqual(calls[0][0], '/extension-workers');
  assert.equal(calls[0][1], 'POST');
  assert.deepEqual(JSON.parse(calls[0][2]), { worker_id: 'chrome-<a>', kind: 'video' });
  assert.equal(nodes['ew-token-value'].value, 'ekw_new');
  assert.equal(nodes['ew-token-value'].type, 'password');
  assert.ok(nodes['ew-msg'].textContent.includes('dispatch is off'));
  assert.equal(btn.disabled, false);
});

test('an unknown rotate outcome says so rather than inviting a second attempt', async () => {
  const { context, nodes } = page({ answers: [failure(500, 'boom', true)] });
  await context.ewRotate('chrome-<a>', 'video', el());
  assert.ok(nodes['ew-msg'].textContent.includes('Not known whether the token was rotated'));
});

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
  assert.equal(nodes['ew-manager'].style.display, 'none');
  assert.equal(nodes['ew-accounts'].innerHTML, '');
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
  assert.equal(app.nodes['ew-manager'].style.display, '');

  const browser = page({ inApp: false, answers: [{ workers: [WORKER] }] });
  await browser.context.loadExtensionWorkers();
  assert.doesNotMatch(browser.nodes['ew-workers'].innerHTML, /Unpair/);
  assert.doesNotMatch(browser.nodes['ew-workers'].innerHTML, /manager scope/);
  assert.equal(browser.nodes['ew-pair'].style.display, 'none');
  assert.equal(browser.nodes['ew-manager'].style.display, 'none');
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

test('each worker shows its own state, freshness and actual versus desired pacing', () => {
  const { context } = page();
  const state = {
    observed_at: Date.now() / 1000 - 30, connection: 'connected', sign_in: 'signed_in',
    readiness: 'ready', activity: 'idle', paused: false, enabled: true, attention: false,
    next_eligible_at: null, pacing: { minSeconds: 5, maxSeconds: 15 },
  };
  const fresh = context.ewCard({
    ...WORKER, fresh: true, receivedAt: Date.now() / 1000,
    control: { pacing: { minSeconds: 10, maxSeconds: 30 } }, state,
  });
  assert.match(fresh, /State \(fresh, observed/);
  assert.match(fresh, /connection connected · sign-in signed_in/);
  assert.match(fresh, /Readiness: ready · activity: idle/);
  assert.match(fresh, /Pacing: 5–15 s actual · 10–30 s desired · next eligible: unknown/);

  const stale = context.ewCard({ ...WORKER, fresh: false, control: { pacing: null }, state });
  assert.match(stale, /State \(stale/);
  assert.match(stale, /Pacing: 5–15 s actual · unknown desired/);
  assert.match(context.ewCard(WORKER), /State: unknown — not reported yet · pacing unknown desired/);
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

const ACCOUNT = {
  provider: 'flow', account_id: 'acct-1', source: 'flow.account',
  observed_at: Date.now() / 1000 - 600, received_at: Date.now() / 1000 - 590,
  connection: 'connected', sign_in: 'signed_in', readiness: 'ready', activity: 'idle',
  attention: false, paused: true, enabled: false, gateway_enabled: false, next_eligible_at: null,
  plan: 'Plus', credit_buckets: null, balance: null, reset_at: null, fresh: false,
  reserved: 0, available: null, reservation_states: {}, needs_reconciliation: false, workers: ['w1'],
};

test('observed accounts keep unknown/stale honest and readiness apart from activity', async () => {
  const { context, nodes } = page({ answers: [{ workers: [], accounts: [ACCOUNT, ACCOUNT] }] });
  await context.loadExtensionWorkers();
  const html = nodes['ew-accounts'].innerHTML;
  assert.equal(html.match(/<div class="card">/g).length, 1, 'a repeated row is drawn once');
  assert.match(html, /spending OFF/);
  assert.match(html, /stale/);
  assert.match(html, /Source: flow\.account · observed/);
  assert.match(html, /Connection: connected · sign-in: signed_in/);
  assert.match(html, /Readiness: ready · activity: idle/);
  assert.match(html, /Balance: unknown — not observed · reset: unknown/);
  assert.match(html, /Plan: Plus/);
  assert.match(html, /Enable spending/);

  const balance = { value: 4, unit: 'credits', source: 'flow.credits', fresh: false };
  const card = context.ewAccountCard({ ...ACCOUNT, fresh: true, balance });
  assert.match(card, /4 credits \(stale\)/);
  assert.match(context.ewAccountCard({ ...ACCOUNT, fresh: true, balance: { ...balance, value: 0, fresh: true } }),
    /Balance: 0 credits/);
});

test('account spending is an explicit app-only PUT, never a side effect of refresh', async () => {
  const app = page({ answers: [{ ok: true, enabled: true }, { workers: [], accounts: [] }] });
  await app.context.ewAccountToggle('flow', 'acct/1', false, el());
  assert.deepEqual(app.calls[0], ['/extension-workers/accounts/enabled', 'PUT',
    JSON.stringify({ provider: 'flow', account_id: 'acct/1', enabled: true })]);
  assert.match(app.nodes['ew-msg'].textContent,
    /Spending enabled for this account\. Queued jobs still need an enabled ready worker and an observed quote\./);

  const browser = page({ inApp: false, answers: [{ workers: [], accounts: [ACCOUNT] }] });
  await browser.context.loadExtensionWorkers();
  assert.doesNotMatch(browser.nodes['ew-accounts'].innerHTML, /Enable spending/);
});

test('pairing offers the backed kinds and keeps video as the default', async () => {
  const { context, nodes, calls } = page({ answers: [{ ok: true, token: 'pair-token' }, { workers: [] },
    { ok: true, token: 'pair-token-2' }, { workers: [] }] });
  nodes['ew-worker-id'].value = 'w';
  await context.ewPair(el());
  assert.deepEqual(calls[0][2], JSON.stringify({ worker_id: 'w', kind: 'video' }));

  nodes['ew-kind'].value = 'audio';
  await context.ewPair(el());
  assert.deepEqual(calls[2][2], JSON.stringify({ worker_id: 'w', kind: 'audio' }));
  assert.match(nodes['ew-token-label'].textContent, /into the extension/);
});

test('a manager mint posts the exact ticked scope and shows its token once', async () => {
  const once = 'test-manager-token';
  const { context, nodes, calls, scope } = page({ answers: [{ ok: true, manager_id: 'relay', token: once }, { workers: [] }] });
  nodes['ew-manager-id'].value = ' relay ';
  scope.push({ dataset: { ewScope: 'w-video' } }, { dataset: { ewScope: 'w-image' } });
  await context.ewMintManager(el());
  assert.deepEqual(calls[0], ['/extension-workers/manager', 'POST',
    JSON.stringify({ manager_id: 'relay', worker_ids: ['w-video', 'w-image'] })]);
  assert.equal(nodes['ew-token-value'].value, once);
  assert.match(nodes['ew-token-label'].textContent, /Relay.*cannot enable account spending or pair workers/);
  assert.doesNotMatch(nodes['ew-msg'].textContent, /test-manager-token/);
  context.ewTokenDone();
  assert.equal(nodes['ew-token-value'].value, '');
  assert.equal(nodes['ew-token'].style.display, 'none');
});

test('a manager needs a name and an exact nonempty scope before any request', async () => {
  const { context, nodes, calls } = page();
  await context.ewMintManager(el());
  assert.match(nodes['ew-msg'].textContent, /Give the manager a name/);
  nodes['ew-manager-id'].value = 'relay';
  await context.ewMintManager(el());
  assert.match(nodes['ew-msg'].textContent, /Tick at least one paired worker/);
  assert.equal(calls.length, 0);
});

test('an unknown manager outcome says so and shows no token', async () => {
  const { context, nodes, scope } = page({ answers: [failure(502, 'gateway gone', true)] });
  nodes['ew-manager-id'].value = 'relay';
  scope.push({ dataset: { ewScope: 'w1' } });
  await context.ewMintManager(el());
  assert.match(nodes['ew-msg'].textContent, /Not known whether the manager token was minted/);
  assert.equal(nodes['ew-token-value'].value, '');
});
