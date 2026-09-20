const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');

function section(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  if (first < 0 || last < 0) throw new Error(`Console function boundary changed: ${start}`);
  return source.slice(first, last);
}

function response(body, status = 200) {
  return { status, ok: status >= 200 && status < 300, text: async () => body == null ? '' : JSON.stringify(body) };
}

function element(value = '') {
  return {
    value,
    innerHTML: '',
    textContent: '',
    className: '',
    disabled: false,
    style: {},
    files: [],
    addEventListener() {},
    querySelector() { return null; },
    insertAdjacentHTML(_position, html) { this.innerHTML += html; },
  };
}

function actionContext(fetchImpl) {
  const nodes = new Map([
    ['cf-msg', element()],
    ['cf-model', element('mock::chat')],
    ['keep-msg-job-0', element()],
  ]);
  const context = vm.createContext({
    document: {
      getElementById: id => nodes.get(id),
      querySelectorAll: () => [],
      createElement: () => element(),
      body: { appendChild() {} },
    },
    currentTab: () => ({ key: 'chat', modality: 'chat' }),
    CREATE_FIELDS: { chat: [] },
    cfValue: () => '',
    crypto: { randomUUID: () => 'first-key' },
    adminHeaders: extra => extra || {},
    showTokenPrompt() {},
    fetch: fetchImpl,
    renderCreateResult() {},
    pollCreate() {},
    stopAllCreatePolls() {},
    setTimeout,
    clearTimeout,
    URL,
    console,
  });
  vm.runInContext([
    'let pendingCreateAttempt = null;',
    section('async function api(path, opts)', '// Contents cannot go through'),
    section('async function submitCreate(btn)', 'function pollCreate(id)'),
    section('async function keepArtifact(jobId, assetId, btn)', 'function renderCreateResult(job)'),
  ].join('\n'), context);
  return { context, nodes };
}

function providerContext(responses) {
  const providers = element();
  const inputs = [];
  Object.defineProperty(providers, 'innerHTML', {
    get() { return this._html || ''; },
    set(html) {
      this._html = html;
      const match = html.match(/data-provider="([^"]+)" data-field="([^"]+)"[^>]*value="([^"]*)"/);
      inputs.splice(0, inputs.length);
      if (match) {
        const input = element(match[3]);
        input.dataset = { provider: match[1], field: match[2] };
        inputs.push(input);
      }
    },
  });
  const nodes = new Map([
    ['filter', element()],
    ['providers', providers],
    ['providers-msg', element()],
    ['local-note', element()],
    ['skipped-note', element()],
  ]);
  let promptCount = 0;
  const context = vm.createContext({
    document: {
      getElementById: id => nodes.get(id),
      querySelectorAll: selector => selector === '#providers input[data-provider][data-field]' ? inputs : [],
    },
    fetch: async (_url, opts) => {
      const next = responses.shift();
      if (next instanceof Error) throw next;
      if (typeof next === 'function') return next(opts);
      return next;
    },
    adminHeaders: extra => extra || {},
    showTokenPrompt() { promptCount += 1; },
    localStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    window: {},
    console,
  });
  vm.runInContext([
    'const state = { providers: [], open: {}, models: {} };',
    'const esc = s => String(s ?? "");',
    'const ago = () => "never";',
    section('async function api(path, opts)', '// Contents cannot go through'),
    section('function captureProviderDrafts()', 'async function loadProviders()'),
    section('async function loadProviders()', 'async function openInBrowserFilter'),
    section('function renderProviders', 'function providerModelList'),
    section('function providerCard(p)', 'async function openInBrowserFilter'),
    'function privacyBadge() { return ""; }',
  ].join('\n'), context);
  return { context, nodes, inputs, get promptCount() { return promptCount; } };
}

function createPollContext() {
  const result = element();
  const timers = [];
  const intervalCalls = [];
  const nodes = new Map([['create-result', result]]);
  const pending = new Map();
  const calls = [];
  const context = vm.createContext({
    document: { getElementById: id => nodes.get(id) },
    esc: s => String(s ?? ''),
    currentTab: () => ({ modality: 'image' }),
    api: pathName => {
      const id = decodeURIComponent(pathName.split('/').pop());
      calls.push(id);
      if (!pending.has(id)) pending.set(id, {});
      return new Promise(resolve => { pending.get(id).resolve = resolve; });
    },
    setTimeout: (fn, delay) => {
      const timer = { fn, delay, cancelled: false };
      timers.push(timer);
      return timer;
    },
    clearTimeout: timer => { if (timer) timer.cancelled = true; },
    setInterval: fn => { intervalCalls.push(fn); return fn; },
    clearInterval() {},
    renderCreateRecent() {},
    artifactsHtml: () => '',
    console,
  });
  const createStart = source.indexOf('let createTab =');
  const createEnd = source.indexOf('function artifactsHtml', createStart);
  if (createStart < 0 || createEnd < 0) throw new Error('Create section boundary changed');
  vm.runInContext([
    "const CREATE_TABS = [{ key: 'image', modality: 'image' }, { key: 'chat', modality: 'chat' }];",
    source.slice(createStart, createEnd),
  ].join('\n'), context);
  vm.runInContext(section('function renderCreateResult(job)', 'async function renderCreateRecent'), context);
  return { context, result, timers, intervalCalls, pending, calls };
}

test('Generate consumes the parsed API object and attaches the accepted job', async () => {
  const requests = [];
  const harness = actionContext(async (_url, opts) => {
    requests.push(opts);
    return response({ id: 'job-1', state: 'accepted', modality: 'chat' });
  });
  const rendered = [];
  const polled = [];
  harness.context.renderCreateResult = job => rendered.push(job);
  harness.context.pollCreate = id => polled.push(id);

  await harness.context.submitCreate(element());

  assert.equal(requests.length, 1);
  assert.equal(harness.nodes.get('cf-msg').textContent, 'queued');
  assert.equal(rendered.length, 1);
  assert.equal(rendered[0].id, 'job-1');
  assert.equal(rendered[0].state, 'accepted');
  assert.deepEqual(polled, ['job-1']);
});

test('Keep renders the server response instead of parsing it twice', async () => {
  const harness = actionContext(async () => response({ pinned: true }));
  const button = element();

  await harness.context.keepArtifact('job', '0', button);

  assert.equal(harness.nodes.get('keep-msg-job-0').textContent, 'kept — the 7-day prune will not take it');
  assert.equal(button.textContent, 'Kept');
  assert.equal(button.disabled, true);
});

test('an ambiguous Generate response keeps the same idempotency key for recovery', async () => {
  const requests = [];
  let attempt = 0;
  const harness = actionContext(async (_url, opts) => {
    requests.push(JSON.parse(opts.body));
    attempt += 1;
    if (attempt === 1) throw new Error('connection lost');
    return response({ id: 'job-recovered', state: 'accepted', modality: 'chat' });
  });
  let sequence = 0;
  harness.context.crypto.randomUUID = () => `key-${++sequence}`;
  const polled = [];
  harness.context.pollCreate = id => polled.push(id);

  await harness.context.submitCreate(element());
  harness.nodes.get('cf-model').value = 'mock::changed-model';
  await harness.context.submitCreate(element());

  assert.equal(requests.length, 2);
  assert.equal(requests[0].idempotency_key, requests[1].idempotency_key);
  assert.deepEqual(requests[0], requests[1], 'retry changed the original request');
  assert.deepEqual(polled, ['job-recovered']);
});

test('missing job or unreadable successful response retains the original attempt', async () => {
  for (const broken of [response({}), {status:200,ok:true,text:async () => {throw Error('body lost');}}]) {
    const requests = [];
    const harness = actionContext(async (_url, opts) => {
      requests.push(JSON.parse(opts.body));
      return requests.length === 1 ? broken : response({id:'recovered',state:'accepted'});
    });
    let sequence = 0;
    harness.context.crypto.randomUUID = () => `key-${++sequence}`;
    await harness.context.submitCreate(element());
    await harness.context.submitCreate(element());
    assert.equal(requests[0].idempotency_key, requests[1].idempotency_key);
  }
});

test('definitive validation refusal allows a corrected new request', async () => {
  const requests=[];
  const harness=actionContext(async (_url,opts) => {
    requests.push(JSON.parse(opts.body));
    return requests.length===1 ? response({detail:'invalid model'},422) : response({id:'new',state:'accepted'});
  });
  let sequence=0;
  harness.context.crypto.randomUUID=()=>`key-${++sequence}`;
  await harness.context.submitCreate(element());
  harness.nodes.get('cf-model').value='corrected';
  await harness.context.submitCreate(element());
  assert.notEqual(requests[0].idempotency_key,requests[1].idempotency_key);
  assert.equal(requests[1].model_id,'corrected');
});

test('provider support is labelled separately from configured availability', () => {
  const harness = providerContext([]);
  const provider = {
    id: 'bfl', display_name: 'BFL', category: 'image', kind: 'bfl', enabled: false,
    configured: false, configured_fields: [], modalities: ['image'],
    generation_supported: true, model_count: 4, publishable_model_count: 4,
  };

  const html = harness.context.providerCard(provider);

  assert.match(html, /4\/4 supported/);
  assert.doesNotMatch(html, /4\/4 offered/);
  assert.match(html, /no key/);
  assert.match(html, />Enable<\/button>/);
});

test('provider 401, 500, and network failures render a retry state', async () => {
  for (const failure of [response({ detail: 'unauthorized' }, 401), response({ detail: 'upstream down' }, 500), new Error('offline')]) {
    const harness = providerContext([failure]);

    await assert.doesNotReject(() => harness.context.loadProviders());
    assert.match(harness.nodes.get('providers-msg').className, /bad/);
    assert.match(harness.nodes.get('providers-msg').innerHTML, /Retry/);
  }
});

test('provider retry preserves an edited credential field', async () => {
  const provider = {
    id: 'alpha', display_name: 'Alpha', category: 'image', kind: 'bfl', enabled: false,
    configured: false, configured_fields: [{ key: 'token', label: 'Token', secret: true, present: false, source: 'env', env_var: 'ALPHA_TOKEN' }],
    modalities: ['image'], generation_supported: true, model_count: 1, publishable_model_count: 1,
  };
  const harness = providerContext([
    response({ providers: [provider], scopes: {}, skipped_custom_providers: [] }),
    response({ detail: 'temporary failure' }, 500),
    response({ providers: [provider], scopes: {}, skipped_custom_providers: [] }),
  ]);
  vm.runInContext("state.open.alpha = true", harness.context);

  await harness.context.loadProviders();
  harness.inputs[0].value = 'typed-before-retry';
  await harness.context.loadProviders();
  assert.equal(harness.inputs[0].value, 'typed-before-retry');
  await harness.context.loadProviders();

  assert.equal(harness.inputs[0].value, 'typed-before-retry');
  assert.equal(harness.nodes.get('providers-msg').textContent, '');
});

test('polling uses serialized reads, ignores stale jobs, and treats cancellation as terminal', async () => {
  const harness = createPollContext();
  harness.context.pollCreate('job-a');
  assert.equal(harness.intervalCalls.length, 0);
  const firstTimer = harness.timers.shift();
  const firstRead = firstTimer.fn();
  harness.context.pollCreate('job-b');
  const secondTimer = harness.timers.shift();
  const secondRead = secondTimer.fn();
  harness.pending.get('job-b').resolve({ id: 'job-b', state: 'cancelled', failure_reason: 'stopped by user' });
  await secondRead;
  harness.pending.get('job-a').resolve({ id: 'job-a', state: 'succeeded', outputs: [] });
  await firstRead;

  assert.match(harness.result.innerHTML, /stopped by user/);
  assert.doesNotMatch(harness.result.innerHTML, /spinner|succeeded|generated|job-a/);
});
