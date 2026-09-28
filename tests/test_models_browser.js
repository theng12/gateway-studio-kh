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
    alert() {}, confirm: () => true, loadStatus() {},
    URLSearchParams, setTimeout, clearTimeout, console,
  });
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
  assert.ok(html.includes('<tr class="m-group"><td colspan="5">LLM chat <span class="n">2</span></td></tr>'),
    'a group header names its operation and counts its own rows');
  assert.ok(html.includes('<tr class="m-group"><td colspan="5">Image <span class="n">1</span></td></tr>'));
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
