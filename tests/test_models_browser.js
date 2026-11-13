// The Models page is a browser over a thousand-row catalogue, so its filters and
// its grouping are the feature, not the trim. These drive the page's own functions
// in a VM with a hand-built DOM — no app, no gateway, no network.
//
// The three things that are easy to get quietly wrong, and are asserted here:
//   1. the chip state reaches the server as the *same* filter the read used,
//   2. a bulk action describes every filter that narrowed it, and
//   3. grouping counts the group, not the page.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
const START = source.indexOf('/* ---------------------------------- models --------------------------------- */');
const END = source.indexOf('/* -------------------------------- providers -------------------------------- */');
if (START < 0 || END < 0) throw new Error('Models section boundary changed in admin_ui.html');

function element(id) {
  return {
    id,
    value: '',
    innerHTML: '',
    textContent: '',
    disabled: false,
    style: {},
    dataset: {},
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    closest() { return null; },
  };
}

function emptyPage(overrides = {}) {
  return {
    models: [], matched: 0, returned: 0, listable_total: 0, enabled_total: 0,
    modalities: [], providers: [], facets: {}, ...overrides,
  };
}

function harness(responses = [emptyPage()]) {
  const nodes = new Map();
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, element(id));
    return nodes.get(id);
  };
  const calls = [];
  const writes = [];
  let index = 0;
  const context = vm.createContext({
    document: { getElementById: get, querySelectorAll: () => [] },
    state: {
      modelRows: [], modelLocations: {}, meta: {},
      modelModality: '', modelStatus: '', modelOrder: 'asc',
    },
    esc: s => String(s ?? '').replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    api: async (page, options) => {
      calls.push(page);
      if (options) writes.push({ page, options });
      return responses[Math.min(index++, responses.length - 1)];
    },
    alert() {}, confirm: () => true, loadStatus() {}, loadProviders() {},
    URLSearchParams, setTimeout, clearTimeout, console,
  });
// Every question in the page is now an in-app dialog; the tests' own confirm/alert/prompt
// stubs are where each answer comes from.
vm.runInContext(`globalThis.uiConfirm = async (t) => (typeof confirm === 'function' ? confirm(t) : true);
globalThis.uiAlert = async (t) => { if (typeof alert === 'function') alert(t); };
globalThis.uiPrompt = async (t, v) => (typeof prompt === 'function' ? prompt(t, v) : v);`, context);

  vm.runInContext(source.slice(START, END), context);
  return { context, get, calls, writes };
}

function paramsOf(page) {
  return new URLSearchParams(page.split('?')[1] || '');
}

test('chip and select state becomes exactly the server filter names', () => {
  const { context, get } = harness();
  get('m-q').value = '  voice  ';
  get('m-provider').value = 'openai-direct';
  get('m-source').value = 'user';
  get('m-lane').value = 'free';
  get('m-privacy').value = 'unverified';
  get('m-provider-lane').value = 'off';
  context.state.modelModality = 'chat';
  context.state.modelStatus = 'enabled';

  const query = { ...context.modelQuery() };
  assert.deepEqual(query, {
    q: 'voice', modality: 'chat', status: 'enabled', provider_id: 'openai-direct',
    source: 'user', lane: 'free', privacy: 'unverified', provider_lane: 'off',
  });
});

test('an empty filter is dropped, never sent as "match nothing"', () => {
  const { context } = harness();
  assert.deepEqual({ ...context.modelFilterBody() }, {},
    'a cleared page must describe no filters at all');
});

test('the read carries the sort, the page size and the grouping', async () => {
  const { context, get, calls } = harness();
  context.state.modelModality = 'video';
  context.state.modelStatus = 'disabled';
  context.state.modelOrder = 'desc';
  get('m-sort').value = 'modality';
  get('m-show').value = '400';

  await context.loadModels();
  const params = paramsOf(calls[0]);
  assert.equal(params.get('modality'), 'video');
  assert.equal(params.get('status'), 'disabled');
  assert.equal(params.get('sort'), 'modality');
  assert.equal(params.get('order'), 'desc');
  assert.equal(params.get('limit'), '400');
  assert.equal(params.has('q'), false, 'an empty filter must not appear in the query');
});

test('grouping widens the request to the whole catalogue', async () => {
  const { context, get, calls } = harness();
  get('m-show').value = '200';
  get('m-group').value = 'modality';

  await context.loadModels();
  assert.equal(paramsOf(calls[0]).get('limit'), '2000',
    'group headers over one page would count the page, not the group');
  assert.equal(get('m-show').value, '2000', 'the size control must show what was asked for');
  assert.equal(get('m-show').disabled, true,
    'and must not offer a page size grouping ignores');
});

test('the operation chips carry server counts and show which is pressed', () => {
  const { context, get } = harness();
  context.renderModelChips(emptyPage({
    modalities: ['chat', 'image', 'video'],
    facets: { modality: { chat: 12, image: 3, video: 1 }, status: { enabled: 4, disabled: 12 } },
  }));
  const html = get('m-modality-chips').innerHTML;
  assert.match(html, /LLM chat/);
  assert.match(html, /data-modality=""[^>]*aria-pressed="true"[^>]*>All<span class="n">16<\/span>/,
    'the All chip is pressed by default and totals the operations');
  assert.match(html, /data-modality="chat"[^>]*aria-pressed="false"[^>]*>LLM chat<span class="n">12<\/span>/);

  const state = get('m-state-chips').innerHTML;
  assert.match(state, /data-status=""[^>]*aria-pressed="true"[^>]*>Any<span class="n">16<\/span>/);
  assert.match(state, /data-status="enabled"[^>]*aria-pressed="false"[^>]*>Enabled<span class="n">4<\/span>/);
  assert.match(state, /data-status="disabled"[^>]*aria-pressed="false"[^>]*>Not enabled<span class="n">12<\/span>/);

  context.state.modelModality = 'video';
  context.state.modelStatus = 'disabled';
  context.renderModelChips(emptyPage({
    modalities: ['chat', 'video'],
    facets: { modality: { chat: 12, video: 1 }, status: { enabled: 0, disabled: 0 } },
  }));
  assert.match(get('m-modality-chips').innerHTML, /data-modality="video"[^>]*aria-pressed="true"/);
  assert.match(get('m-state-chips').innerHTML, /data-status="disabled"[^>]*aria-pressed="true"/);
});

test('a chip press sets view state and reloads', async () => {
  const { context, get, calls } = harness();
  const chip = { dataset: { modality: 'transcription' } };
  get('m-modality-chips').listeners.click[0]({ target: { closest: () => chip } });
  // The handler calls loadModels without awaiting; let the microtask queue drain.
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(context.state.modelModality, 'transcription');
  assert.equal(paramsOf(calls.at(-1)).get('modality'), 'transcription');

  const pressed = { dataset: { status: 'disabled' } };
  get('m-state-chips').listeners.click[0]({ target: { closest: () => pressed } });
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(context.state.modelStatus, 'disabled');
  assert.equal(paramsOf(calls.at(-1)).get('status'), 'disabled');
});

test('grouping buckets by operation and counts each bucket', () => {
  const { context, get } = harness();
  const row = (id, modality, enabled) => ({
    provider_id: 'p', provider_name: 'P', model_id: id, display_name: id,
    modality, enabled, provider_enabled: true, free_lane: false, data_use: 'unverified',
  });
  context.state.modelRows = [
    row('a', 'image', true), row('b', 'chat', false), row('c', 'chat', false),
  ];
  context.state.meta = { matched: 3, returned: 3, listable_total: 3, enabled_total: 1 };
  get('m-group').value = 'modality';

  context.renderModels();
  const html = get('m-rows').innerHTML;
  assert.ok(html.includes('<tr class="m-group"><td colspan="6">LLM chat <span class="n">2</span></td></tr>'),
    'a group header names its operation and counts its own rows');
  assert.ok(html.includes('<tr class="m-group"><td colspan="6">Image <span class="n">1</span></td></tr>'));
  assert.equal((html.match(/m-group/g) || []).length, 2, 'one header per operation, not per row');
});

// A model several machines run used to be one row per machine, with the same name
// and no note that it was already offered elsewhere. The server merges those into
// one row with a per-Mac sub-row list; the page must draw it once, say how many
// machines have it, and keep each Mac's own toggle reachable.
function locationRow(overrides = {}) {
  return {
    provider_id: 'studio-hub-local', provider_name: 'Studio Hub', provider_enabled: true,
    provider_ids: ['studio-hub-local', 'studio-hub-aaa'],
    model_id: 'mock-hub/clone-model', display_name: 'Clone Model', modality: 'tts',
    source: 'discovered', enabled: true, free_lane: false, data_use: 'unverified',
    is_location: true, locations_total: 2, locations_enabled: 1,
    locations: [
      { provider_id: 'studio-hub-local', provider_name: 'This Mac', provider_enabled: true,
        modality: 'tts', enabled: true },
      { provider_id: 'studio-hub-aaa', provider_name: 'Studio Two', provider_enabled: true,
        modality: 'tts', enabled: false },
    ],
    ...overrides,
  };
}

test('a model several machines run is drawn once, with its count and its Macs', () => {
  const { context, get } = harness();
  const row = locationRow();
  context.state.modelRows = [row];
  context.state.meta = { matched: 1, returned: 1, listable_total: 1, enabled_total: 1 };

  context.renderModels();
  const html = get('m-rows').innerHTML;
  assert.equal((html.match(/class="m-name"/g) || []).length, 1,
    'the model is drawn once, whatever number of machines run it');
  assert.equal((html.match(/<tr class="m-machine/g) || []).length, 2,
    'and each machine that reported it is a sub-row beneath it');
  assert.match(html, /1 of 2 machines/, 'the Machines cell is the served-of-discovered count');
  assert.match(html, /partial/, 'a partially rolled-out model says so rather than reading as on');
  assert.match(html, /↳ This Mac/);
  assert.match(html, /↳ Studio Two/);
  assert.match(html, /not enabled/, 'each Mac states its own enabled/disabled state');
});

test('the merged tick switches the model on for every machine that has it', async () => {
  const { context, get, writes } = harness();
  context.state.modelRows = [locationRow()];
  context.state.modelLocations = {
    [context.modelLocationKey('mock-hub/clone-model', 'tts')]:
      ['studio-hub-local', 'studio-hub-aaa'],
  };
  context.state.meta = { matched: 1, returned: 1, listable_total: 1, enabled_total: 1 };
  context.renderModels();

  await context.toggleModelEverywhere('mock-hub/clone-model', 'tts', true);
  const body = JSON.parse(writes.at(-1).options.body ?? '{}');
  assert.deepEqual(body.items, [
    { provider_id: 'studio-hub-local', model_id: 'mock-hub/clone-model' },
    { provider_id: 'studio-hub-aaa', model_id: 'mock-hub/clone-model' },
  ], 'one request covers every Mac on the row');
  assert.equal(body.enabled, true);
});

test('the server sends the Macs, and the page never derives them', async () => {
  const { context, get, calls } = harness([
    emptyPage({
      models: [locationRow()],
      matched: 1, returned: 1, listable_total: 1, enabled_total: 1,
    }),
  ]);
  get('m-show').value = '400';
  await context.loadModels();
  assert.deepEqual(
    context.state.modelLocations[context.modelLocationKey('mock-hub/clone-model', 'tts')],
    ['studio-hub-local', 'studio-hub-aaa']);
  assert.equal(paramsOf(calls[0]).get('limit'), '400');
});

test('a Mac sub-row toggles only that Mac', async () => {
  const { context, get, calls } = harness();
  context.state.modelRows = [locationRow()];
  context.state.meta = { matched: 1, returned: 1, listable_total: 1, enabled_total: 1 };
  context.renderModels();
  const html = get('m-rows').innerHTML;
  assert.match(html,
    /onchange="toggleModel\('studio-hub-aaa','mock-hub\/clone-model', this\.checked\)"/,
    'the sub-row checkbox addresses that machine provider id and nothing else');
});

test('a cloud row keeps one row per vendor and publishes no machines', () => {
  const { context, get } = harness();
  context.state.modelRows = [{
    provider_id: 'openai-direct', provider_name: 'OpenAI', provider_enabled: true,
    provider_ids: ['openai-direct'], model_id: 'gpt-4o', display_name: 'GPT-4o',
    modality: 'chat', source: 'discovered', enabled: true, free_lane: false,
    data_use: 'private', is_location: false, locations: null,
    locations_enabled: null, locations_total: null,
  }];
  context.state.meta = { matched: 1, returned: 1, listable_total: 1, enabled_total: 1 };
  context.renderModels();
  const html = get('m-rows').innerHTML;
  assert.equal((html.match(/m-machine/g) || []).length, 0, 'a vendor is not a machine');
  assert.match(html, /onchange="toggleModel\('openai-direct','gpt-4o', this\.checked\)"/);
});

test('every active filter appears in the bulk confirmation, and none is invented', () => {
  const { context } = harness();
  const phrases = Array.from(context.modelFilterPhrases({
    q: 'voice', modality: 'chat', status: 'enabled', provider_id: 'openai-direct',
    source: 'user', lane: 'free', privacy: 'non_private', provider_lane: 'off',
  }));
  assert.deepEqual(phrases, [
    'matching “voice”', 'doing LLM chat', 'that are enabled right now',
    'from openai-direct', 'from the added by hand set', 'on free lanes',
    'whose privacy is “may retain prompts”', 'on switched-off providers',
  ]);
  assert.deepEqual(Array.from(context.modelFilterPhrases({
    q: '', modality: '', status: '', provider_id: '', source: '',
    lane: '', privacy: '', provider_lane: '',
  })), [], 'a cleared page must say "all of them" out loud, not list filters');
});

test('clearing resets the chips, the sort and the direction', () => {
  const { context, get } = harness();
  context.state.modelModality = 'video';
  context.state.modelStatus = 'enabled';
  context.state.modelOrder = 'desc';
  get('m-show').value = '2000';
  get('m-sort').value = 'modality';
  get('m-group').value = 'operation';
  get('m-q').value = 'voice';

  context.clearFilters();

  assert.equal(context.state.modelModality, '');
  assert.equal(context.state.modelStatus, '');
  assert.equal(context.state.modelOrder, 'asc');
  assert.equal(get('m-show').value, '400');
  assert.equal(get('m-sort').value, '');
  assert.equal(get('m-group').value, '');
  assert.equal(get('m-q').value, '');
});

test('the order toggle is dead until something is sorted by', () => {
  const { context, get } = harness();
  context.syncModelOrder();
  assert.equal(get('m-order').disabled, true, 'direction means nothing without a sort');
  assert.equal(get('m-order').textContent, '↑');

  get('m-sort').value = 'name';
  context.syncModelOrder();
  assert.equal(get('m-order').disabled, false);
  context.toggleModelOrder();
  assert.equal(context.state.modelOrder, 'desc');
  assert.equal(get('m-order').textContent, '↓');
});

// ---------------------------------------------------------------------------
// The inventory age, and the re-discover button the owner asked for.
//
// His report: *"openrouter has a new model called space bunny and i just enabled it in model tab
// but it's not refreshed or shown in the chat tab. do i have to click sth or restart the app? i
// prefer a refresh button in the model's tab."*
//
// The list on this page is a **discovery snapshot**, not a live view — a provider's catalogue is
// fetched once and cached until something re-discovers it, and nothing does that on a timer or at
// startup. So a model the provider added since the last fetch is simply absent, and the page said
// nothing about how old its inventory was. A reader with no age and no refresh control reasonably
// concludes the list is current and the fault is elsewhere.
//
// Two assertions matter here and they are different: the page must be able to **ask** for a newer
// list, and it must be able to **say** how old the one it has is.

test('the inventory age is rendered from the honest field', () => {
  const { context } = harness();
  context.state.freshness = { inventoryAgeMs: 6 * 3600000,
    inventoryOldestProvider: 'openai-direct' };
  const html = context.modelInventoryAge();
  assert.match(html, /inventory 6h old/, 'the age the operator needs is not on the page');
  assert.match(html, /oldest openai-direct/, 'the age must name what it is the age of');
});

test('a list more than a day old is marked, not just stated', () => {
  const { context } = harness();
  context.state.freshness = { inventoryAgeMs: 3 * 86400000 };
  const html = context.modelInventoryAge();
  assert.match(html, /3d old/);
  assert.match(html, /⚠/, 'a three-day-old inventory is old enough to hide a new model silently');
  assert.match(html, /title=/, 'the mark must say what it means');
});

test('an unknown age says unknown, and never reads as fresh', () => {
  const { context } = harness();
  context.state.freshness = { inventoryAgeMs: null };
  assert.match(context.modelInventoryAge(), /unknown/,
    'a provider that reported no fetch time is unknown, which is not the same as current');
  context.state.freshness = null;
  assert.match(context.modelInventoryAge(), /unknown/,
    'a page that has not read the age yet must say unknown, not nothing');
});

test('the age is read from the catalogue route, not bolted onto the models read', async () => {
  // The models route is the one request that runs on every keystroke of the filter box, and it
  // reads only the provider registry. Answering the age from there would pull a full catalogue
  // rebuild into it. The age changes on discovery, not on filtering, so it is its own read.
  const { context, calls } = harness([{ freshness: { inventoryAgeMs: 60000 } }]);
  await context.loadFreshness();
  assert.deepEqual(calls, ['/catalog'], 'the age must come from the catalogue route');
  assert.equal(context.state.freshness.inventoryAgeMs, 60000);
});

test('a failed age read leaves the page working and says unknown', async () => {
  const { context } = harness([{ freshness: { inventoryAgeMs: 60000 } }]);
  context.api = async () => { throw new Error('nope'); };
  await context.loadFreshness();
  assert.equal(context.state.freshness, null);
  assert.match(context.modelInventoryAge(), /unknown/,
    'a secondary read that failed must not take the model list down with it');
});

test('the age badge never reads the stale flag', () => {
  // `stale` is false by construction — it is computed from the last catalogue *rebuild*, which
  // `freshness()` performs before reading it — so a badge wired to it would never light. The
  // repository's AGENTS.md says so; this is the test that keeps someone from "fixing" it back.
  const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
  const start = source.indexOf('function modelInventoryAge()');
  const body = source.slice(start, source.indexOf('\n}', start));
  assert.ok(start > 0, 'modelInventoryAge is gone');
  assert.doesNotMatch(body, /\.stale\b/,
    'the badge reads a flag that is false by construction, so it can never light');
  assert.match(body, /inventoryAgeMs/, 'the badge must read the measured age');
});

test('re-discovering reloads this page, not only the providers page', async () => {
  // The Providers page's own button reloads providers and status, which is everything that page
  // shows. Doing only that here would re-fetch every catalogue and leave the model list unchanged —
  // the exact "I pressed refresh and nothing moved" outcome.
  const { context, get, calls } = harness([emptyPage(), emptyPage()]);
  const button = get('m-rediscover-btn');
  await context.refreshModels(button);
  assert.ok(calls.includes('/refresh-all'), 're-discover must actually re-discover');
  assert.ok(calls.filter(c => c.startsWith('/models?')).length >= 1,
    're-discover must redraw the model list it just refreshed');
  assert.equal(button.disabled, false, 'the button must come back');
  assert.equal(button.textContent, 'Re-discover all providers', 'the label must be restored');
});

test('the button is on the Models page itself', () => {
  // The owner asked for it *in the model's tab*. The console's only re-discover control used to be
  // on the Providers tab, so the page listing models gave him no way to ask for a newer list.
  const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
  const models = source.slice(source.indexOf('<section id="view-models"'),
                              source.indexOf('<section id="view-providers"'));
  assert.match(models, /refreshModels\(this\)/,
    'the Models page has no re-discover control again');
});

// ---------------------------------------------------------------------------
// **Which** providers a re-discover touches.
//
// The owner, 2026-10-19: *"when i click re-discover models, i don't know if it runs across all
// the providers or the one selected. i think it should just run for the one selected or let us
// choose... i only needed magpie refresh."* The button posted `/refresh-all` unconditionally, so
// a press he meant for Magpie re-read every provider's catalogue. These drive the page's own
// functions and assert on the **wire** — the route that was called — not on a helper's return
// value, because the bug was exactly that the wrong route was reached.

function functionSource(name) {
  const at = source.indexOf(`function ${name}(`);
  if (at < 0) throw new Error(`${name} is gone from admin_ui.html`);
  // Keep an `async ` prefix: sliced off, the body is a syntax error, not a missing function.
  const start = source.lastIndexOf('async ', at) === at - 6 ? at - 6 : at;
  return source.slice(start, source.indexOf('\n}', at) + 2);
}

test('a re-discover filtered to one provider asks that provider alone', async () => {
  const { context, get, calls } = harness([
    { discovered: 175, error: null },                                   // the provider's own route
    emptyPage({ providers: [['magpie-main', 'Magpie']] }),              // /models
    { freshness: { inventoryAgeMs: 1000 } },                            // /catalog
  ]);
  context.state.modelProviders = [['magpie-main', 'Magpie']];
  get('m-provider').value = 'magpie-main';
  const button = get('m-rediscover-btn');
  await context.refreshModels(button);

  assert.ok(calls.includes('/providers/magpie-main/refresh'),
    'the scoped button must take the per-provider route the gateway already has');
  assert.ok(!calls.includes('/refresh-all'),
    'a scoped press must not become the whole-fleet fetch the owner complained about');
  assert.equal(button.textContent, 'Re-discover Magpie',
    'the button must name the provider it acts on, not just "Models"');
  assert.equal(get('m-rediscover-msg').textContent, 'Magpie: 175 models found',
    'the run must say what it found, or "nothing changed" and "it failed" read the same');
});

test('a scoped re-discover reports the provider\'s error rather than a count', async () => {
  const { context, get } = harness([
    { discovered: 0, error: 'invalid api key' },
    emptyPage(), { freshness: { inventoryAgeMs: 1000 } },
  ]);
  context.state.modelProviders = [['magpie-main', 'Magpie']];
  get('m-provider').value = 'magpie-main';
  await context.refreshModels(get('m-rediscover-btn'));

  assert.match(get('m-rediscover-msg').textContent, /Magpie: invalid api key/);
  assert.equal(get('m-rediscover-msg').className, 'msg bad',
    'a failed discovery must not be drawn as a success');
});

test('with no provider filtered, the page offers the choice instead of making it', () => {
  const { context, get } = harness();
  context.state.modelProviders = [['magpie-main', 'Magpie']];

  context.syncRediscover();
  assert.notEqual(get('m-rediscover-scope').style.display, 'none',
    'with nothing filtered the picker is the only thing that says what will be fetched');
  assert.equal(get('m-rediscover-btn').textContent, 'Re-discover all providers');

  get('m-rediscover').value = 'magpie-main';
  context.syncRediscover();
  assert.equal(get('m-rediscover-btn').textContent, 'Re-discover Magpie',
    'choosing a provider in the picker must name it on the button');

  get('m-provider').value = 'magpie-main';
  context.syncRediscover();
  assert.equal(get('m-rediscover-scope').style.display, 'none',
    'the filter is the scope when it names one; a second control could only contradict it');
  assert.equal(get('m-rediscover').value, '',
    'the hidden picker must not keep a scope that would come back on Clear Filters');
});

test('the picker scopes the run when no filter is set', async () => {
  const { context, get, calls } = harness([
    { discovered: 12, error: null }, emptyPage(), { freshness: { inventoryAgeMs: 1 } },
  ]);
  context.state.modelProviders = [['magpie-main', 'Magpie']];
  get('m-rediscover').value = 'magpie-main';
  await context.refreshModels(get('m-rediscover-btn'));

  assert.ok(calls.includes('/providers/magpie-main/refresh'));
  assert.ok(!calls.includes('/refresh-all'));
});

test('a whole-fleet run reports the total and names the providers that failed', async () => {
  const { context, get, calls } = harness([
    { outcomes: { 'magpie-main': { count: 175, error: null },
                  'openrouter-main': { count: 0, error: 'timed out' } } },
    emptyPage(), { freshness: { inventoryAgeMs: 1 } },
  ]);
  context.state.modelProviders = [['magpie-main', 'Magpie'], ['openrouter-main', 'OpenRouter']];
  await context.refreshModels(get('m-rediscover-btn'));

  assert.ok(calls.includes('/refresh-all'), 'with no provider chosen the whole fleet is the run');
  assert.deepEqual(calls.filter(c => c.startsWith('/providers/')), [],
    'the fleet run must not become N per-provider reads');
  const said = get('m-rediscover-msg').textContent;
  assert.match(said, /2 providers re-discovered · 175 models found/,
    `the run must say what it found: ${said}`);
  assert.match(said, /failed: OpenRouter \(timed out\)/,
    `a provider that did not answer must be named: ${said}`);
});

test('the Providers page control says it is the all-providers one, and says what it did', async () => {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, element(id));
    return nodes.get(id);
  };
  const calls = [];
  const context = vm.createContext({
    document: { getElementById: node, querySelectorAll: () => [] },
    // This page never reads `/models`, so the only names it has are its own provider list —
    // the fallback that keeps a failure from being reported as a bare provider id.
    state: { providers: [{ id: 'magpie-main', display_name: 'Magpie' },
                         { id: 'openrouter-main', display_name: 'OpenRouter' }] },
    esc: s => String(s ?? ''),
    api: async page => {
      calls.push(page);
      return { outcomes: { 'magpie-main': { count: 175, error: null },
                           'openrouter-main': { count: 0, error: 'timed out' } } };
    },
    loadProviders: async () => {},
    loadStatus: async () => {},
    alert() {},
  });
// Every question in the page is now an in-app dialog; the tests' own confirm/alert/prompt
// stubs are where each answer comes from.
vm.runInContext(`globalThis.uiConfirm = async (t) => (typeof confirm === 'function' ? confirm(t) : true);
globalThis.uiAlert = async (t) => { if (typeof alert === 'function') alert(t); };
globalThis.uiPrompt = async (t, v) => (typeof prompt === 'function' ? prompt(t, v) : v);`, context);

  const refreshAll = source.indexOf('async function refreshAll(');
  vm.runInContext(
    functionSource('providerName') + functionSource('rediscoverAll')
      + source.slice(refreshAll, source.indexOf('\n}', refreshAll) + 2),
    context);

  const button = element('btn');
  await context.refreshAll(button);
  assert.deepEqual(calls, ['/refresh-all'], 'this one is deliberately the whole fleet');
  assert.equal(button.textContent, 'Re-discover all providers', 'the label must be restored');
  const said = node('providers-msg').textContent;
  assert.match(said, /2 providers re-discovered · 175 models found/,
    `the page must say what the run found, after the reload that clears the line: ${said}`);
  assert.match(said, /failed: OpenRouter \(timed out\)/,
    `a provider that did not answer must be named, not numbered: ${said}`);
  assert.equal(node('providers-msg').className, 'msg ok');
});

test('every provider card carries its own Re-discover in the row overflow', () => {
  // Two controls on the row (On/Off and Configure) and the rest behind ⋯: "i only needed magpie
  // refresh" stays one click away without a third button on every card.
  const card = source.slice(source.indexOf('function providerCard(p)'),
                            source.indexOf('function providerModelList('));
  const head = card.slice(card.indexOf('<div class="card-actions">'));
  const actions = head.slice(0, head.indexOf('</div>'));
  assert.match(actions, /aria-label="More actions for \$\{esc\(p\.display_name\)\}"/,
    'the card row has no overflow control');
  assert.match(actions, /id="pmenu-\$\{esc\(p\.id\)\}"/,
    'the overflow menu must be per card');
  assert.match(actions, /refreshProvider\('\$\{esc\(p\.id\)\}', this\)/,
    'Re-discover left the row without moving into the card menu');
  assert.match(head, /id="msg-\$\{esc\(p\.id\)\}"/,
    'the result of that button is written into a span that must exist on the collapsed row');
  assert.doesNotMatch(card, /<div class="cat">/,
    'the id · kind · endpoint line belongs behind the detail, not on every card');
  assert.match(card, /card-details/);
});

test('the Models page button and its picker are both in the page, not only in the script', () => {
  const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
  const models = source.slice(source.indexOf('<section id="view-models"'),
                              source.indexOf('<section id="view-providers"'));
  assert.match(models, /id="m-rediscover"/,
    'the picker the script drives has no element to drive');
  assert.match(models, /id="m-rediscover-msg"/,
    'the run would have nowhere to say what it found');
  const providers = source.slice(source.indexOf('<section id="view-providers"'),
                                 source.indexOf('<section id="view-create"'));
  assert.match(providers, />Re-discover all providers</,
    'the Providers page button must be plainly the all-providers one');
  assert.doesNotMatch(providers, /Refresh All Models/,
    'the old label is back, and it is the one the owner could not tell from the per-provider one');
});

// ---------------------------------------------------------------------------
// The Providers page's tab strip.
//
// The owner: *"inside the gateway kh's providers, there's so many stuff that i have to scroll.
// can you make it like fleet which it is split and have separate tabs for different stuff?"*
// Four unrelated subjects were stacked in one column — twenty-three provider cards, the
// concurrency settings, the live queue and the measured numbers — so reaching any one meant
// scrolling past the other three. The concurrency block had already been moved to the top once
// for that same reason, which is a fix that does not scale.
//
// The assertions below are about the two ways a tab strip rots: a pane that no tab can reach,
// and a tab that leaves the previous pane on screen.

test('every provider pane is named by exactly one tab, and every tab opens a pane', () => {
  const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
  const block = source.slice(source.indexOf('const PROVIDER_TABS'), source.indexOf('let providerTab'));
  const tabs = [...block.matchAll(/key: '([a-z-]+)'/g)].map(m => m[1]);
  const panes = [...source.matchAll(/id="provider-pane-([a-z-]+)"/g)].map(m => m[1]);
  assert.deepEqual(tabs.slice().sort(), panes.slice().sort(),
    'a pane with no tab is unreachable, and a tab with no pane opens nothing');
  assert.ok(tabs.length >= 4, `the page still stacks its subjects: ${tabs.join(', ')}`);
});

test('selecting a tab shows one pane and hides the others', () => {
  const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
  const shown = {};
  const nodes = new Map();
  const node = key => {
    if (!nodes.has(key)) nodes.set(key, { style: {}, innerHTML: '', id: key });
    return nodes.get(key);
  };
  const context = vm.createContext({
    document: { getElementById: id => node(id) },
    state: {},
    esc: s => String(s ?? ''),
    PROVIDER_TABS: [
      { key: 'list', title: 'Providers', hint: '' },
      { key: 'capacity', title: 'Concurrency', hint: '' },
      { key: 'queue', title: 'Queue', hint: '' },
      { key: 'measured', title: 'Measured', hint: '' },
    ],
    providerTab: 'list',
  });
// Every question in the page is now an in-app dialog; the tests' own confirm/alert/prompt
// stubs are where each answer comes from.
vm.runInContext(`globalThis.uiConfirm = async (t) => (typeof confirm === 'function' ? confirm(t) : true);
globalThis.uiAlert = async (t) => { if (typeof alert === 'function') alert(t); };
globalThis.uiPrompt = async (t, v) => (typeof prompt === 'function' ? prompt(t, v) : v);`, context);

  // Evaluate just the two functions, so this drives the real code and not a copy.
  const start = source.indexOf('function providerPane(');
  const end = source.indexOf('async function refreshAll(');
  vm.runInContext(source.slice(start, end), context);

  context.selectProviderTab('queue');
  const visible = ['list', 'capacity', 'queue', 'measured']
    .filter(key => node(`provider-pane-${key}`).style.display !== 'none');
  assert.deepEqual(visible, ['queue'],
    `selecting Queue left more than one pane on screen: ${visible.join(', ')}`);
  assert.equal(context.providerTab, 'queue');
});

test('an unknown tab key falls back to the first pane rather than showing none', () => {
  // A stale `#queue`-style link, or a key renamed without the link, must not leave the page
  // blank — a blank page reads as a broken app, not as a bad link.
  const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
  const nodes = new Map();
  const node = key => {
    if (!nodes.has(key)) nodes.set(key, { style: {}, innerHTML: '', id: key });
    return nodes.get(key);
  };
  const context = vm.createContext({
    document: { getElementById: id => node(id) },
    esc: s => String(s ?? ''),
    PROVIDER_TABS: [{ key: 'list', title: 'Providers', hint: '' }],
    providerTab: 'list',
  });
// Every question in the page is now an in-app dialog; the tests' own confirm/alert/prompt
// stubs are where each answer comes from.
vm.runInContext(`globalThis.uiConfirm = async (t) => (typeof confirm === 'function' ? confirm(t) : true);
globalThis.uiAlert = async (t) => { if (typeof alert === 'function') alert(t); };
globalThis.uiPrompt = async (t, v) => (typeof prompt === 'function' ? prompt(t, v) : v);`, context);

  const start = source.indexOf('function providerPane(');
  vm.runInContext(source.slice(start, source.indexOf('async function refreshAll(')), context);
  context.selectProviderTab('gone');
  assert.equal(context.providerTab, 'list');
  assert.notEqual(node('provider-pane-list').style.display, 'none',
    'an unrecognised key must show the default pane, not nothing');
});

test('the queue link opens Capacity and scrolls to the queue section', () => {
  // The queue is now a section of Capacity: the link must select that tab first, then scroll to
  // the section once it is displayed.
  const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
  const setView = source.slice(source.indexOf('function setView(view)'),
                               source.indexOf('function renderProviders('));
  assert.match(setView, /selectProviderTab\('capacity'\); scrollToSection\('provider-capacity-queue'\)/,
    'the #queue link no longer opens the queue section');
});
