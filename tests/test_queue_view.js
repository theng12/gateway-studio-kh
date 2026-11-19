/**
 * The console's Queue view — H9, and the owner's "i need the UI for that".
 *
 * Audit H9: *"The console has no queue view at all. Four views exist (models, providers, create,
 * packs); 'queue' appears twice in 104 KB (a per-row badge). `worker.budgets` — written 'so the
 * console can say what is bounding a lane' — has no reader outside tests."*
 *
 * Two things this file guards, both easy to lose in a refactor:
 *
 * 1. **A position is drawn with its bounding number.** `3` alone cannot be told from `3 of 7`, and
 *    the whole point of the view is that the owner can see how deep the queue is.
 * 2. **A job that has never been refused says so, and is not given a reason.** The gateway
 *    publishes `defer_reason: null` for a fresh submission; rendering that as pacing or as
 *    "provider busy" would be a claim about a provider nobody measured.
 *
 * Nothing here touches the app or the running gateway: it is a VM over the page's own source.
 */
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');

function queueCode() {
  const first = source.indexOf('// -- queue ---');
  const last = source.indexOf('// -- measured ---');
  if (first < 0 || last < 0) throw new Error('the queue section moved; update this test');
  return source.slice(first, last) + `
    ;globalThis.__q = { renderQueue, loadQueue, renderQueueSummary: null };`;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function ago(t) {
  if (!t) return 'never';
  const s = Date.now() / 1000 - t;
  return s < 60 ? `${Math.floor(s)}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : `${Math.floor(s / 3600)}h ago`;
}

function element() {
  return { innerHTML: '', textContent: '', className: '' };
}

function queueContext(fetchImpl) {
  const nodes = new Map([['queue', element()], ['queue-summary', element()], ['queue-msg', element()]]);
  const context = vm.createContext({
    document: { getElementById: id => nodes.get(id) },
    esc,
    ago,
    api: fetchImpl,
    console,
  });
// Every question in the page is now an in-app dialog; the tests' own confirm/alert/prompt
// stubs are where each answer comes from.
vm.runInContext(`globalThis.uiConfirm = async (t) => (typeof confirm === 'function' ? confirm(t) : true);
globalThis.uiAlert = async (t) => { if (typeof alert === 'function') alert(t); };
globalThis.uiPrompt = async (t, v) => (typeof prompt === 'function' ? prompt(t, v) : v);`, context);

  vm.runInContext(queueCode(), context);
  return { context, nodes };
}

function waitingJob(overrides = {}) {
  return {
    id: 'job-1', short_id: 'job-1', state: 'accepted', modality: 'image',
    provider_id: 'studio-hub-blue', provider_name: 'Blue Room',
    model_id: 'mlx-community/Z-Image', model_name: 'Z-Image',
    created_at: Date.now() / 1000 - 30, age_seconds: 30,
    queue_position: 3, queue_size: 7, defer_reason: null, retry_in_s: null,
    attempts_used: 0, attempts_allowed: 3, attempts: [], attempt_policy: 'A failed job is retried on a different machine, up to 3 attempts in total.',
    ...overrides,
  };
}

function payload(overrides = {}) {
  return {
    generated_at: Date.now() / 1000, waiting: 1, truncated: false,
    jobs: [waitingJob()],
    lanes: [{ modality: 'image', waiting: 1, in_flight: 0, offered: 3, used: 0, idle: 3,
              offer_source: 'fleet._eligible_macs over registry.models.enabled_models',
              offer_detail: '3 Macs can run this lane now, at 1 site',
              limit: 2, source: 'curated', detail: 'provider-concurrency.local.json caps image at 2' }],
    sites: [{ provider_id: 'studio-hub-blue', provider_name: 'Blue Room', in_flight: 1,
              budget: { limit: 1, source: 'hub', detail: 'the Hub reports 1 free Mac for this operation' } }],
    pool: { limit: 4, source: 'gateway', detail: 'nothing set it, so 4', in_flight: 1 },
    in_flight: { 'studio-hub-blue': 1 },
    budgets: { 'studio-hub-blue': { limit: 1, source: 'hub', detail: 'the Hub reports 1 free Mac for this operation' } },
    queue_wait: { max_deferrals: 8, defer_cap_seconds: 60, source: 'gateway' },
    ...overrides,
  };
}

test('a waiting job draws its position, its lane and its model', () => {
  const { context, nodes } = queueContext(async () => payload());
  context.renderQueue(payload());
  const html = nodes.get('queue').innerHTML;
  assert.match(html, /3 <span class="muted">of 7<\/span>/, 'position 3 must read as 3 of 7');
  assert.match(html, /Z-Image/);
  assert.match(html, /Blue Room/);
  assert.match(html, /image/);
  assert.match(nodes.get('queue-summary').textContent, /1 waiting · 1 running · pool 4 \(gateway\)/);
});

test('a fresh job is queued, never given a reason it does not have', () => {
  const { context, nodes } = queueContext(async () => payload());
  context.renderQueue(payload());
  const html = nodes.get('queue').innerHTML;
  assert.match(html, /queued for the next free slot/);
  assert.doesNotMatch(html, /provider_pace/);
  assert.doesNotMatch(html, /retry in/);
});

test('a deferred job quotes its reason, when it will retry, and how often it was put back', () => {
  const { context, nodes } = queueContext(async () => payload());
  context.renderQueue(payload({
    jobs: [waitingJob({
      defer_reason: 'LOCATION_NOT_READY: MACHINE_BUSY', retry_in_s: 30,
      queue_position: 1, queue_size: 2, deferrals: 4,
    })],
  }));
  const html = nodes.get('queue').innerHTML;
  assert.match(html, /LOCATION_NOT_READY: MACHINE_BUSY/);
  assert.match(html, /retry in 30s/);
  assert.match(html, /put back 4 times/);
  assert.match(html, /1 <span class="muted">of 2<\/span>/);
});

test('a job that has never been put back does not claim it was', () => {
  const { context, nodes } = queueContext(async () => payload());
  context.renderQueue(payload());
  assert.doesNotMatch(nodes.get('queue').innerHTML, /put back \d+ time/);
});

test('the retry policy reads attempt n of 3, with the last machine and reason beside it', () => {
  const { context, nodes } = queueContext(async () => payload());
  context.renderQueue(payload({
    jobs: [waitingJob({
      attempts_used: 1, attempts_allowed: 3,
      attempts: [{ number: 1, machine: 'studio-hub-red', failure_code: 'MACHINE_BUSY',
                   failure_reason: 'no free machine for image' }],
    })],
  }));
  const html = nodes.get('queue').innerHTML;
  assert.match(html, /attempt 2 of 3/, 'one settled attempt means the second is in progress');
  assert.match(html, /studio-hub-red/);
  assert.match(html, /MACHINE_BUSY: no free machine for image/);
  assert.match(html, /title="A failed job is retried on a different machine, up to 3 attempts in total\."/);
});

test('a job that has never been tried says so and is still numbered', () => {
  const { context, nodes } = queueContext(async () => payload());
  context.renderQueue(payload());
  const html = nodes.get('queue').innerHTML;
  assert.match(html, /attempt 1 of 3/);
  assert.match(html, /not tried yet/);
});

test('each lane draws the capacity it offers, and the site budgets are still live', () => {
  const { context, nodes } = queueContext(async () => payload());
  context.renderQueue(payload());
  const html = nodes.get('queue').innerHTML;
  assert.match(html, /What each lane can run/);
  assert.match(html, /the Hub reports 1 free Mac for this operation/);
  assert.match(html, /<th>Offered<\/th>/);
  assert.match(html, /<th>Used<\/th>/);
  assert.match(html, /<th>Idle<\/th>/);
  assert.match(html, /3 Macs can run this lane now, at 1 site/,
    'the figure carries the sentence saying where it came from');
  // The lane's cap is the gate's number, not a capacity claim: it must not appear as one here.
  assert.doesNotMatch(html, /Cap in force/);
  assert.doesNotMatch(html, /provider-concurrency\.local\.json caps image at 2/);
});

test('a lane whose sites do not report per-machine detail says not reported, never 0', () => {
  const { context, nodes } = queueContext(async () => payload());
  context.renderQueue(payload({
    lanes: [{ modality: 'image', waiting: 1, in_flight: 0, offered: null, used: 0, idle: null,
              offer_source: 'fleet._eligible_macs over registry.models.enabled_models',
              offer_detail: 'not reported: no site in this lane publishes per-machine detail',
              limit: null, source: 'fleet', detail: 'follows the fleet' }],
  }));
  const html = nodes.get('queue').innerHTML;
  assert.match(html, /not reported/, 'an absent reading must be written as absent');
  assert.match(html, /no site in this lane publishes per-machine detail/);
});

test('a site with no resolved budget says so rather than defaulting to a number', () => {
  const { context, nodes } = queueContext(async () => payload());
  context.renderQueue(payload({
    sites: [{ provider_id: 'openai-direct', provider_name: 'OpenAI', in_flight: 1, budget: null }],
  }));
  const html = nodes.get('queue').innerHTML;
  assert.match(html, /not resolved yet/);
  assert.doesNotMatch(html, /0 <span class="muted">gateway/);
});

test('an empty queue reads as empty, not as a table with no rows', () => {
  const { context, nodes } = queueContext(async () => payload());
  context.renderQueue(payload({ jobs: [], waiting: 0 }));
  const html = nodes.get('queue').innerHTML;
  assert.match(html, /Nothing is waiting\. Every accepted job has been dispatched\./);
});

test('a truncated listing says so', () => {
  const { context, nodes } = queueContext(async () => payload());
  context.renderQueue(payload({ truncated: true }));
  assert.match(nodes.get('queue-summary').textContent, /listing truncated/);
});

test('loadQueue reads the admin queue route and shows a failure in words', async () => {
  const seen = [];
  const { context, nodes } = queueContext(async (path) => {
    seen.push(path);
    return payload();
  });
  await context.loadQueue();
  assert.deepEqual(seen, ['/queue']);
  assert.match(nodes.get('queue').innerHTML, /Z-Image/);

  const failing = queueContext(async () => { throw new Error('the gateway did not answer'); });
  await failing.context.loadQueue();
  assert.equal(failing.nodes.get('queue').innerHTML, '');
  assert.equal(failing.nodes.get('queue-msg').textContent, 'the gateway did not answer');
  assert.equal(failing.nodes.get('queue-msg').className, 'msg bad');
});

test('the queue is a section of the Providers page and is loaded with it', () => {
  assert.match(source, /<div id="queue"><\/div>/, 'the queue needs a place to draw');
  assert.match(source, /loadConcurrency\(\); loadMeasured\(\); loadQueue\(\);/,
    'opening the Providers page must load the queue with the ceilings it explains');
  assert.match(source, /if \(hash\.startsWith\('queue'\)\) return 'providers';/,
    'a #queue link must land on the page that carries it');
  // **The mechanism changed; the guarantee did not.** This asserted a `scrollIntoView`, because
  // every section of the Providers page shared one column, so the queue was reachable by scrolling
  // once the view was shown. The page now splits those sections into tabs — the owner asked for it,
  // because reaching any one of them meant scrolling past the other three — so the queue is no
  // longer in that column and the scroll would land on a hidden element. What must still hold is
  // the reason the assertion existed: **the link moves the operator to the queue, and it does so
  // after the view is shown**, not while it is still hidden.
  assert.match(source, /if \(\(location\.hash \|\| ''\)\.startsWith\('#queue'\)\) \{ selectProviderTab\('capacity'\); scrollToSection\('provider-capacity-queue'\); \}/,
    'a #queue link must open the Capacity tab and scroll to the queue, not leave the operator on the list');
  const setView = source.slice(source.indexOf('function setView(view)'),
                               source.indexOf('function renderProviders('));
  assert.doesNotMatch(setView, /scrollIntoView/,
    'the scroll targets an element that is now behind a tab, where it would do nothing');
});

test('a waiting fleet job says how many Macs could take it, and a cloud job says nothing', () => {
  const { context, nodes } = queueContext(async () => payload());
  context.renderQueue(payload({ jobs: [waitingJob({ eligible_macs: 12 })] }));
  assert.match(nodes.get('queue').innerHTML, /Blue Room · 12 Macs eligible/);
  context.renderQueue(payload({ jobs: [waitingJob({ eligible_macs: 1 })] }));
  assert.match(nodes.get('queue').innerHTML, /1 Mac eligible/);
  context.renderQueue(payload({ jobs: [waitingJob({ eligible_macs: null })] }));
  assert.doesNotMatch(nodes.get('queue').innerHTML, /eligible/);
});
