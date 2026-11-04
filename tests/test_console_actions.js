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

test('API errors show server messages and validation fields without exposing rejected input', async () => {
  const cases = [
    [{ detail: 'Pool already exists' }, 400, 'Pool already exists'],
    [{ detail: [{ loc: ['body', 'members', 0], msg: 'Member does not resolve', input: 'secret-token', ctx: { secret: 'private-context' } },
      { loc: ['body', 'name'], msg: 'Field required' }] }, 422, 'members.0: Member does not resolve; name: Field required'],
    [{ detail: { message: 'Provider is unavailable', input: 'secret-token' } }, 503, 'Provider is unavailable'],
    [{ detail: { msg: 'Please supply the key', input: 'secret-token' } }, 403, 'Please supply the key'],
    [{ detail: { message: 'Gateway key required' } }, 401, 'Gateway key required'],
    [{ detail: { unexpected: 'secret-token' } }, 500, 'HTTP 500'],
    [{ detail: [{ loc: ['body'], input: 'secret-token' }] }, 422, 'HTTP 422'],
    [null, 502, 'HTTP 502'],
  ];
  for (const [body, status, expected] of cases) {
    const { context } = actionContext(async () => response(body, status));
    const prompts = [];
    context.showTokenPrompt = message => prompts.push(message);
    await assert.rejects(context.api('/pools', { method: 'POST' }), error => {
      assert.equal(error.message, expected);
      assert.doesNotMatch(error.message, /secret-token|private-context|\[object Object\]/);
      assert.equal(error.ambiguous, status >= 500);
      return true;
    });
    assert.deepEqual(prompts, status === 401 || status === 403 ? [expected] : []);
  }
});

test('API preserves multipart bodies and leaves their Content-Type boundary to fetch', async () => {
  const body = new FormData();
  body.set('prompt', 'Image prompt');
  let received;
  const { context } = actionContext(async (_url, opts) => {
    received = opts;
    return response({ id: 'multipart-job' });
  });
  assert.equal((await context.api('/jobs', { method: 'POST', body })).id, 'multipart-job');
  assert.equal(received.body, body);
  assert.equal(received.headers['Content-Type'], undefined);
});

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
    ['key-filter', element('all')],
    ['keyless-count', element()],
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
    section('function toast(id, text', 'async function toggleOpen'),
    section('async function toggleEnabled(', 'async function saveCreds'),
    'function loadStatus() {}',
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
  assert.match(html, /role="switch" aria-label="BFL On\/Off"/);
  assert.match(html, /onchange="toggleEnabled\('bfl', this.checked, this\)"> Off/);
  assert.doesNotMatch(html, /type="checkbox"[^>]*checked /);
});

test('provider switch sends Off and restores the confirmed state on refusal', async () => {
  const provider = {
    id: 'alpha', display_name: 'Alpha', category: 'language', kind: 'openai_compatible',
    enabled: true, configured: true, configured_fields: [], modalities: ['chat'],
    generation_supported: true, model_count: 1, publishable_model_count: 1,
  };
  const calls = [];
  const harness = providerContext([
    opts => { calls.push(JSON.parse(opts.body)); return response({detail: 'Could not save'}, 500); },
    response({providers: [provider], scopes: {}}),
  ]);
  harness.context.loadEvidence = () => {};
  const message = element();
  harness.nodes.set('msg-alpha', message);
  const input = element();
  input.checked = false;
  await harness.context.toggleEnabled('alpha', false, input);
  assert.deepEqual(calls, [{enabled: false}]);
  assert.equal(input.checked, true);
  assert.equal(input.disabled, false);
  assert.match(harness.nodes.get('providers').innerHTML, /role="switch"[^>]*checked/);
  assert.match(message.textContent, /Could not save/);
});

test('provider switch shows the saved Off state and success message', async () => {
  const provider = {
    id: 'alpha', display_name: 'Alpha', category: 'language', kind: 'openai_compatible',
    enabled: false, configured: true, configured_fields: [], modalities: ['chat'],
    generation_supported: true, model_count: 1, publishable_model_count: 1,
  };
  const harness = providerContext([
    response({provider}), response({providers: [provider], scopes: {}}),
  ]);
  harness.context.loadEvidence = () => {};
  const message = element();
  harness.nodes.set('msg-alpha', message);
  await harness.context.toggleEnabled('alpha', false, element());
  assert.match(harness.nodes.get('providers').innerHTML, /\"> Off<\/label>/);
  assert.match(message.textContent, /Off\. New jobs will not use this provider/);
});

test('provider key filter separates keyed from keyless providers', () => {
  const harness = providerContext([]);
  const provider = (id, name, configured) => ({
    id, display_name: name, category: 'language', kind: 'openai_compatible',
    enabled: true, configured, configured_fields: [], modalities: ['language'],
    generation_supported: true, model_count: 1, publishable_model_count: 1,
  });
  vm.runInContext(
    `state.providers = ${JSON.stringify([provider('alpha', 'Alpha', true), provider('beta', 'Beta', false)])}`,
    harness.context);

  harness.nodes.get('key-filter').value = 'missing';
  harness.context.renderProviders();
  let html = harness.nodes.get('providers').innerHTML;
  assert.match(html, /Beta/, 'the keyless provider must be visible');
  assert.doesNotMatch(html, /Alpha/, 'a keyed provider must be filtered out');
  assert.match(harness.nodes.get('keyless-count').textContent, /1 without a key/);

  harness.nodes.get('key-filter').value = 'set';
  harness.context.renderProviders();
  html = harness.nodes.get('providers').innerHTML;
  assert.match(html, /Alpha/);
  assert.doesNotMatch(html, /Beta/);

  // The filter is a view, not a one-shot: saving a key re-renders and the
  // selection must persist so the owner keeps seeing what is left.
  harness.context.renderProviders();
  assert.equal(harness.nodes.get('key-filter').value, 'set');
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

// -- concurrency settings ---------------------------------------------------
function concurrencyContext(nodes, boxes) {
  const context = vm.createContext({
    document: {
      getElementById: id => nodes.get(id),
      querySelectorAll: selector => selector === '[id^="conc-p-"]' ? boxes : [],
    },
    console,
  });
  vm.runInContext([
    'const esc = s => String(s ?? "");',
    section('function concurrencyMessage(', 'async function loadProviders()'),
  ].join('\n'), context);
  return context;
}

function providerBox(id, value) {
  const box = element(value);
  box.id = id;
  return box;
}

test('a blank concurrency box is sent as null, which clears the setting', () => {
  // Blank and 0 are different: 0 would be written as a limit nobody can run, and
  // omitting the key would leave the old value in place. Only null clears.
  const context = concurrencyContext(
    new Map([['conc-global', element('')]]),
    [providerBox('conc-p-openai-direct', '4'), providerBox('conc-p-other', '')],
  );
  const payload = vm.runInContext('concurrencyPayload()', context);
  assert.equal(payload.global, null);
  assert.equal(payload.providers['openai-direct'], 4);
  assert.equal(payload.providers.other, null);
});

test('a typed concurrency is sent as a number', () => {
  const context = concurrencyContext(new Map([['conc-global', element(' 8 ')]]), []);
  const payload = vm.runInContext('concurrencyPayload()', context);
  assert.equal(payload.global, 8);
  assert.equal('providers' in payload, false, 'no provider rows means no provider key at all');
});

test('the panel shows the limit in force and where it came from', () => {
  // "1" and "nobody said" are different answers, and only one is worth acting on.
  const target = element();
  const context = concurrencyContext(new Map([['concurrency', target]]), []);
  vm.runInContext(`renderConcurrency(${JSON.stringify({
    global: { limit: 4, source: 'gateway', detail: 'nothing set it', stated: null },
    providers: [{
      provider_id: 'openai-direct', limit: 1, source: 'gateway',
      detail: 'no limit set, so one at a time', stated: null,
    }],
    path: '/tmp/provider-concurrency.local.json',
  })})`, context);
  assert.match(target.innerHTML, /openai-direct/);
  assert.match(target.innerHTML, /no limit set/);
  assert.match(target.innerHTML, /id="conc-p-openai-direct"/);
  assert.match(target.innerHTML, /placeholder="1"/, 'the unset row must show what is in force');
  assert.match(target.innerHTML, /value=""/, 'and must not look like a row somebody set');
});

// -- measured ---------------------------------------------------------------
function measuredContext(target) {
  const context = vm.createContext({
    document: { getElementById: id => (id === 'measured' ? target : undefined) },
    console,
  });
  vm.runInContext([
    'const esc = s => String(s ?? "");',
    section('function measuredCell(', 'async function loadProviders()'),
  ].join('\n'), context);
  return context;
}

function measuredRow(overrides = {}) {
  return {
    provider_id: 'studio-hub', model_id: 'Qwen3.5-9B-4bit', samples: 13,
    max_prompt_tokens: 120, max_completion_tokens: 4096,
    median_tokens_per_second: 17.3, machines: {},
    stated: { max_output_tokens: 4096, max_output_tokens_source: 'gateway' },
    source: 'gateway', ...overrides,
  };
}

test('the measured panel puts the stated ceiling beside what really ran', () => {
  // The whole point of the feature: 4096 stated and 4096 measured, adjacent, so a
  // cap being hit is visible without anyone having to remember to compare them.
  const target = element();
  const context = measuredContext(target);
  vm.runInContext(`renderMeasured(${JSON.stringify({
    rows: [measuredRow()], window: 500, note: 'measured by us',
  })})`, context);
  assert.match(target.innerHTML, /Qwen3\.5-9B-4bit/);
  assert.match(target.innerHTML, /4096/);
  assert.match(target.innerHTML, /gateway/, 'the stated source must travel with the number');
  assert.match(target.innerHTML, /17\.3/);
  assert.match(target.innerHTML, /measured by us/);
});

test('an unmeasured figure is a dash and never a zero', () => {
  // A rate nobody measured and a rate of zero tokens/second are different facts,
  // and only one of them means the lane is unusable.
  const target = element();
  const context = measuredContext(target);
  vm.runInContext(`renderMeasured(${JSON.stringify({
    rows: [measuredRow({ median_tokens_per_second: null, max_prompt_tokens: null })],
    window: 500, note: '',
  })})`, context);
  assert.match(target.innerHTML, /—/);
  assert.doesNotMatch(target.innerHTML, />0</, 'an absent figure must not render as zero');
});

test('a model outside the catalogue reads as unknown, not as unlimited', () => {
  const target = element();
  const context = measuredContext(target);
  vm.runInContext(`renderMeasured(${JSON.stringify({
    rows: [measuredRow({ stated: null })], window: 500, note: '',
  })})`, context);
  assert.match(target.innerHTML, /not in catalogue/);
  assert.match(target.innerHTML, /4096/, 'its measured maximum is still published');
});

test('a model that IS catalogued but states no ceiling is not called missing', () => {
  // Found by reading the live record: `openai-direct::gpt-6-luna` is in the catalogue
  // with `max_output_tokens: null` (it declares no output limit). Rendering that as
  // "not in catalogue" told the reader a model was absent when it was present — two
  // different facts collapsed into one string.
  const target = element();
  const context = measuredContext(target);
  vm.runInContext(`renderMeasured(${JSON.stringify({
    rows: [measuredRow({ stated: { max_output_tokens: null, max_output_tokens_source: 'gateway' } })],
    window: 500, note: '',
  })})`, context);
  assert.match(target.innerHTML, /none stated/);
  assert.doesNotMatch(target.innerHTML, /not in catalogue/);
});

test('a chat model names no machine and the cell says so rather than guessing', () => {
  const target = element();
  const context = measuredContext(target);
  vm.runInContext(`renderMeasured(${JSON.stringify({
    rows: [measuredRow()], window: 500, note: '',
  })})`, context);
  assert.match(target.innerHTML, /not named/);
});

test('nothing measured is said in words, not shown as an empty table', () => {
  const target = element();
  const context = measuredContext(target);
  vm.runInContext(`renderMeasured(${JSON.stringify({ rows: [], window: 500, note: '' })})`, context);
  assert.match(target.innerHTML, /Nothing has run yet/);
});
