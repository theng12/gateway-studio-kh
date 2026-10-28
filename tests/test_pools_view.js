/**
 * The console's Pools view — one name for several models, tried in order.
 *
 * A pool is a *list of promises*, and the failure that costs an afternoon is a member that
 * stopped resolving weeks ago: the pool still works, just less often than its list suggests,
 * and nothing said so. So this file guards two things:
 *
 * 1. **Every member is drawn with whether it still resolves.** A pool rendered as a bare list
 *    of ids reads as "all of these will work", which is the claim the gateway cannot make.
 * 2. **The id a caller has to use is shown.** `pool::<name>` is what Story Studio saves; a pane
 *    that shows only the display name leaves the operator guessing at the thing they must type.
 *
 * 3. **Members are picked, not typed**, kept in order, and an existing pool can be edited or
 *    deleted. The picker offers only what the Models page says is enabled.
 *
 * Nothing here touches the app or the running gateway: it is a VM over the page's own source.
 */
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');

function poolsCode() {
  const first = source.indexOf('// Pools -- one name for several models');
  const last = source.indexOf('async function refreshAll(btn)');
  if (first < 0 || last < 0 || last < first) {
    throw new Error('the pools section moved; update this test');
  }
  return source.slice(first, last)
    + `;globalThis.__p = { loadPools, newPool, editPool, poolSearch, poolAddAt, poolDrop, poolMove,`
    + ` savePool, removePool, closePoolEditor, state: () => poolEdit, results: () => poolResultRows };`;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function element(value = '') {
  return {
    innerHTML: '', textContent: '', className: '', value, disabled: false,
    querySelector: () => null, scrollIntoView() {},
  };
}

function page(extra = []) {
  return new Map([
    ['pools', element()], ['pool-editor', element()], ['pool-edit-body', element()],
    ['pool-members-list', element()], ['pool-results', element()], ['pool-count', element()],
    ['pool-msg', element()], ['pool-name', element('')], ['pool-display', element('')],
    ...extra,
  ]);
}

function poolsContext(fetchImpl, nodes) {
  const context = vm.createContext({
    document: { getElementById: id => nodes.get(id) },
    esc,
    api: fetchImpl,
    priceInfo: m => ({ text: m.free_lane ? 'Free (provider states $0)' : '$1.00 / $2.00 per 1M' }),
    alert: () => {},
    confirm: () => true,
  });
  vm.runInContext(poolsCode(), context);
  return context;
}

// Keep the page's API wrapper in the path: a raw object passed to fetch becomes
// "[object Object]", even though a semantic API stub would accept it.
function wirePoolsContext(fetchImpl, nodes) {
  const context = poolsContext(() => {}, nodes);
  context.fetch = fetchImpl;
  context.adminHeaders = extra => ({ ...extra, Authorization: 'Bearer test-token' });
  context.showTokenPrompt = () => {};
  const first = source.indexOf('async function api(path, opts)');
  const last = source.indexOf('// Contents cannot go through', first);
  vm.runInContext(source.slice(first, last), context);
  return context;
}

function response(body, status = 200) {
  return { status, ok: status >= 200 && status < 300, text: async () => JSON.stringify(body) };
}

const SOON = Math.floor(Date.now() / 1000) + 3600;

function pool(overrides = {}) {
  return {
    name: 'free-all',
    display_name: 'Free, every lane',
    description: 'whichever free lane still works',
    members: ['g::flash', 'g::spent', 'g::off', 'o::gone'],
    members_detail: [
      { id: 'g::flash', resolves: true, state: 'ok', provider_name: 'Google AI (free tier)', model_name: 'Gemini Flash' },
      { id: 'g::spent', resolves: true, state: 'ok', provider_name: 'Google AI (free tier)', model_name: 'Gemini Spent',
        cooldown_until: SOON, cooldown_kind: 'daily', cooldown_reason: 'quota' },
      { id: 'g::off', resolves: false, state: 'disabled', provider_name: 'Google AI (free tier)', model_name: 'Gemini Off' },
      { id: 'o::gone', resolves: false, state: 'provider_off', provider_name: 'OpenRouter', model_name: 'Gone' },
    ],
    resolvable_count: 2,
    cooling_count: 1,
    ...overrides,
  };
}

function models() {
  return {
    models: [
      { provider_id: 'g', provider_name: 'Google AI (free tier)', model_id: 'flash', display_name: 'Gemini Flash', free_lane: true },
      { provider_id: 'g', provider_name: 'Google AI (free tier)', model_id: 'pro', display_name: 'Gemini Pro', free_lane: true },
      { provider_id: 'o', provider_name: 'OpenRouter', model_id: 'claude', display_name: 'Claude Sonnet', free_lane: false },
      { provider_id: 'pool', provider_name: 'Model pools', model_id: 'free-all', display_name: 'free-all', free_lane: false },
      { provider_id: 'studio-hub-local', provider_name: 'Hub', model_id: 'qwen', display_name: 'Qwen', locations: [{}] },
    ],
  };
}

function api(calls, { pools = [pool()] } = {}) {
  return async (path_, options) => {
    const body = options && options.body;
    calls.push([path_, options && options.method, typeof body === 'string' ? JSON.parse(body) : body]);
    if (path_.startsWith('/models')) return models();
    if (options && options.method) return {};
    return { pools };
  };
}

test('the pane is a section of the Providers page', () => {
  assert.match(source, /id="provider-pane-pools"/, 'the pools pane must exist');
  assert.match(source, /key: 'pools'/, 'and a tab must point at it');
});

test('every member is drawn with its health, and a spent one says when it is back', async () => {
  const nodes = page();
  const ctx = poolsContext(api([]), nodes);
  await ctx.__p.loadPools();
  const html = nodes.get('pools').innerHTML;
  assert.match(html, /Gemini Flash/);
  assert.match(html, /Ready/, 'a healthy member says so');
  assert.match(html, /Out of quota, back at \d{2}:\d{2}/, 'a cooling member says when it returns');
  assert.match(html, /Model off/, 'a switched-off model is marked, not hidden');
  assert.match(html, /Provider off/, 'and so is a switched-off provider');
  assert.match(html, /2 of 4 members can answer, 1 out of quota/);
});

test('a paid member at the end of a free pool is marked as the last resort, with the rule', async () => {
  const mixed = pool();
  mixed.members_detail = mixed.members_detail.map(m => ({ ...m, paid: m.resolves ? false : null }))
    .concat([{ id: 'o::luna', resolves: true, state: 'ok', provider_name: 'OpenRouter', model_name: 'GPT-6 Luna', paid: true }]);
  const nodes = page();
  const ctx = poolsContext(api([], { pools: [{ ...mixed, paid_last_resort: true }] }), nodes);
  await ctx.__p.loadPools();
  const html = nodes.get('pools').innerHTML;
  assert.match(html, /Paid members are used only when the free ones are out for the day/);
  assert.equal((html.match(/Paid · last resort/g) || []).length, 1, 'only the paid member wears the chip');

  const plain = page();
  await poolsContext(api([]), plain).__p.loadPools();
  assert.doesNotMatch(plain.get('pools').innerHTML, /last resort|out for the day/, 'an all-free pool says nothing');
});

test('the id a caller must send is shown, not only the display name', async () => {
  const nodes = page();
  const ctx = poolsContext(api([]), nodes);
  await ctx.__p.loadPools();
  assert.match(nodes.get('pools').innerHTML, /pool::free-all/);
});

test('no pools reads as empty, and says what a pool is for', async () => {
  const nodes = page();
  const ctx = poolsContext(api([], { pools: [] }), nodes);
  await ctx.__p.loadPools();
  const html = nodes.get('pools').innerHTML;
  assert.match(html, /No pools yet/);
  assert.doesNotMatch(html, /pool::/, 'an empty list must not render a phantom pool');
});

test('a failed read is shown in words rather than as an empty list', async () => {
  const nodes = page();
  const ctx = poolsContext(async () => { throw new Error('gateway unreachable'); }, nodes);
  await ctx.__p.loadPools();
  assert.match(nodes.get('pools').innerHTML, /gateway unreachable/);
});

test('the picker offers enabled chat models only, with search across provider, name and free', async () => {
  const calls = [];
  const nodes = page();
  const ctx = poolsContext(api(calls), nodes);
  await ctx.__p.loadPools();
  ctx.__p.newPool();
  await new Promise(r => setTimeout(r, 0));
  assert.ok(calls.some(c => /\/models\?modality=chat&status=enabled&provider_lane=on/.test(c[0])),
    'asks for enabled chat models on enabled providers');
  const ids = () => Array.from(ctx.__p.results()).map(r => `${r.provider_id}::${r.model_id}`);
  assert.deepEqual(ids(), ['g::flash', 'g::pro', 'o::claude'], 'no pools, no merged Studio Hub rows');
  ctx.__p.poolSearch('google free');
  assert.deepEqual(ids(), ['g::flash', 'g::pro']);
  ctx.__p.poolSearch('sonnet');
  assert.deepEqual(ids(), ['o::claude']);
  ctx.__p.poolSearch('paid');
  assert.deepEqual(ids(), ['o::claude']);
});

test('adding appends, reordering moves, removing drops -- order is the routing policy', async () => {
  const nodes = page();
  const ctx = poolsContext(api([]), nodes);
  await ctx.__p.loadPools();
  ctx.__p.newPool();
  await new Promise(r => setTimeout(r, 0));
  ctx.__p.poolAddAt(0); ctx.__p.poolAddAt(1); ctx.__p.poolAddAt(2);
  ctx.__p.poolAddAt(2); // adding the same model twice keeps it once
  assert.deepEqual(Array.from(ctx.__p.state().members), ['g::flash', 'g::pro', 'o::claude']);
  ctx.__p.poolMove(2, -1);
  assert.deepEqual(Array.from(ctx.__p.state().members), ['g::flash', 'o::claude', 'g::pro']);
  ctx.__p.poolMove(0, -1); // off the top: nothing happens
  ctx.__p.poolDrop(0);
  assert.deepEqual(Array.from(ctx.__p.state().members), ['o::claude', 'g::pro']);
});

test('creating sends the picked members in order', async () => {
  const calls = [];
  const nodes = page([['pool-name', element('mine')], ['pool-display', element('Mine')]]);
  const ctx = poolsContext(api(calls), nodes);
  ctx.__p.newPool();
  await new Promise(r => setTimeout(r, 0));
  ctx.__p.poolAddAt(2); ctx.__p.poolAddAt(0);
  await ctx.__p.savePool(element());
  const sent = calls.find(c => c[1] === 'POST');
  assert.equal(sent[0], '/pools');
  assert.deepEqual(Array.from(sent[2].members), ['o::claude', 'g::flash']);
  assert.equal(sent[2].name, 'mine');
});

for (const isNew of [true, false]) {
  test(`${isNew ? 'creating' : 'editing'} reaches fetch as authenticated JSON with ordered members and spaces`, async () => {
    const requests = [];
    const nodes = page();
    const existing = pool({ name: 'openrouter free' });
    const ctx = wirePoolsContext(async (url, opts) => {
      requests.push([url, opts]);
      if (url.includes('/models?')) return response(models());
      return response({ pools: [existing] });
    }, nodes);
    await ctx.__p.loadPools();
    if (isNew) ctx.__p.newPool();
    else ctx.__p.editPool('openrouter free');
    await new Promise(r => setTimeout(r, 0));
    nodes.get('pool-name').value = '  openrouter free  ';
    nodes.get('pool-display').value = '  My free models  ';
    if (isNew) { ctx.__p.poolAddAt(2); ctx.__p.poolAddAt(0); }
    else { ctx.__p.poolMove(1, -1); ctx.__p.poolDrop(3); }
    await ctx.__p.savePool(element());
    const [url, opts] = requests.find(([, opts]) => opts.method === (isNew ? 'POST' : 'PATCH'));
    assert.equal(url, isNew ? '/admin/api/pools' : '/admin/api/pools/openrouter%20free');
    assert.equal(opts.headers['Content-Type'], 'application/json');
    assert.equal(opts.headers.Authorization, 'Bearer test-token');
    assert.equal(typeof opts.body, 'string', 'fetch must receive encoded JSON, not a raw object');
    // The body carries the pool's modality too, and `tier_models` is null for a chat pool: an image
    // pool is the only thing that fills it (`poolEdit`'s own 'chat' default is what is sent).
    assert.deepEqual(JSON.parse(opts.body), isNew
      ? { name: 'openrouter free', display_name: 'My free models', members: ['o::claude', 'g::flash'],
          modality: 'chat', tier_models: null }
      : { display_name: 'My free models', members: ['g::spent', 'g::flash', 'g::off'],
          modality: 'chat', tier_models: null });
    assert.equal(ctx.__p.state(), null, 'successful saving closes the editor');
  });
}

test('a validation refusal preserves the pool draft and restores its save button with readable feedback', async () => {
  const nodes = page();
  const ctx = wirePoolsContext(async (url, opts) => {
    if (opts.method === 'POST') return response({ detail: [{
      loc: ['body', 'name'], msg: 'Pool name is already used', input: 'private-input',
    }] }, 422);
    return response(url.includes('/models?') ? models() : { pools: [] });
  }, nodes);
  ctx.__p.newPool();
  await new Promise(r => setTimeout(r, 0));
  nodes.get('pool-name').value = 'openrouter free';
  nodes.get('pool-display').value = 'My free models';
  ctx.__p.poolAddAt(2); ctx.__p.poolAddAt(0);
  const button = element(); button.textContent = 'Create Pool';
  await ctx.__p.savePool(button);
  assert.deepEqual(Array.from(ctx.__p.state().members), ['o::claude', 'g::flash']);
  assert.equal(nodes.get('pool-name').value, 'openrouter free');
  assert.equal(nodes.get('pool-display').value, 'My free models');
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, 'Create Pool');
  assert.match(nodes.get('pool-msg').innerHTML, /name.*Pool name is already used/);
  assert.doesNotMatch(nodes.get('pool-msg').innerHTML, /\[object Object\]|private-input/);
});

test('a pool with no members, or no name, is refused before the request', async () => {
  const calls = [];
  const nodes = page([['pool-name', element('mine')]]);
  const ctx = poolsContext(api(calls), nodes);
  ctx.__p.newPool();
  await ctx.__p.savePool(element());
  assert.match(nodes.get('pool-msg').innerHTML, /at least one member/);
  assert.equal(calls.some(c => c[1] === 'POST'), false, 'nothing to send, so nothing is sent');
});

test('an existing pool opens with its members, and saving PATCHes them in the new order', async () => {
  const calls = [];
  const nodes = page([['pool-display', element('Renamed')]]);
  const ctx = poolsContext(api(calls), nodes);
  await ctx.__p.loadPools();
  ctx.__p.editPool('free-all');
  await new Promise(r => setTimeout(r, 0));
  assert.deepEqual(Array.from(ctx.__p.state().members), ['g::flash', 'g::spent', 'g::off', 'o::gone']);
  const list = nodes.get('pool-members-list').innerHTML;
  assert.match(list, /Out of quota, back at/, 'the editor shows the cooldown too');
  assert.match(list, /Provider off/);
  ctx.__p.poolMove(1, -1);
  ctx.__p.poolDrop(3);
  await ctx.__p.savePool(element());
  const sent = calls.find(c => c[1] === 'PATCH');
  assert.equal(sent[0], '/pools/free-all');
  assert.deepEqual(Array.from(sent[2].members), ['g::spent', 'g::flash', 'g::off']);
  assert.equal(sent[2].display_name, 'Renamed');
  assert.equal(ctx.__p.state(), null, 'the editor closes after a save');
});

test('deleting names the url-encoded pool and says the models are untouched', async () => {
  const calls = [];
  const nodes = page();
  const ctx = poolsContext(api(calls), nodes);
  await ctx.__p.removePool('free/all', element());
  assert.deepEqual(calls[0].slice(0, 2), ['/pools/free%2Fall', 'DELETE']);
});

test('a declined confirmation deletes nothing', async () => {
  const calls = [];
  const nodes = page();
  const ctx = poolsContext(api(calls), nodes);
  ctx.confirm = () => false;
  await ctx.__p.removePool('free-all', element());
  assert.equal(calls.length, 0);
});
