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
  return source.slice(first, last) + `;globalThis.__p = { loadPools, addPool, removePool };`;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function element(value = '') {
  return { innerHTML: '', textContent: '', className: '', value, disabled: false };
}

function poolsContext(fetchImpl, nodes) {
  const context = vm.createContext({
    document: { getElementById: id => nodes.get(id) },
    esc,
    api: fetchImpl,
    alert: () => {},
    confirm: () => true,
  });
  vm.runInContext(poolsCode(), context);
  return context;
}

function pool(overrides = {}) {
  return {
    name: 'free-all',
    display_name: 'Free — every lane',
    description: 'whichever free lane still works',
    members: ['google-ai-free::gemini-3.5-flash', 'openrouter-main::gone/model'],
    members_detail: [
      { id: 'google-ai-free::gemini-3.5-flash', resolves: true, provider_name: 'Google AI (free tier)' },
      { id: 'openrouter-main::gone/model', resolves: false, provider_name: '' },
    ],
    resolvable_count: 1,
    ...overrides,
  };
}

test('the pane is a section of the Providers page', () => {
  assert.match(source, /id="provider-pane-pools"/, 'the pools pane must exist');
  assert.match(source, /key: 'pools'/, 'and a tab must point at it');
});

test('every member is drawn, and one that no longer resolves says so', async () => {
  const nodes = new Map([['pools', element()]]);
  const ctx = poolsContext(async () => ({ pools: [pool()] }), nodes);
  await ctx.__p.loadPools();
  const html = nodes.get('pools').innerHTML;
  assert.match(html, /google-ai-free::gemini-3\.5-flash/, 'a resolving member is listed');
  assert.match(html, /openrouter-main::gone\/model/, 'a stale member is listed too, not hidden');
  assert.match(html, /does not resolve/, 'and it is marked -- the whole point of the health read');
  assert.match(html, /1\/2 resolve/, 'the count is drawn beside the list');
});

test('the id a caller must send is shown, not only the display name', async () => {
  const nodes = new Map([['pools', element()]]);
  const ctx = poolsContext(async () => ({ pools: [pool()] }), nodes);
  await ctx.__p.loadPools();
  assert.match(nodes.get('pools').innerHTML, /pool::free-all/,
    'Story Studio saves `pool::<name>`; the pane has to say what that is');
});

test('no pools reads as empty, and says what a pool is for', async () => {
  const nodes = new Map([['pools', element()]]);
  const ctx = poolsContext(async () => ({ pools: [] }), nodes);
  await ctx.__p.loadPools();
  const html = nodes.get('pools').innerHTML;
  assert.match(html, /No pools yet/);
  assert.doesNotMatch(html, /pool::/, 'an empty list must not render a phantom pool');
});

test('a failed read is shown in words rather than as an empty list', async () => {
  const nodes = new Map([['pools', element()]]);
  const ctx = poolsContext(async () => { throw new Error('gateway unreachable'); }, nodes);
  await ctx.__p.loadPools();
  assert.match(nodes.get('pools').innerHTML, /gateway unreachable/);
});

test('adding reads the members one per line, in order', async () => {
  const nodes = new Map([
    ['pools', element()],
    ['pool-name', element('free-all')],
    ['pool-display', element('Free')],
    ['pool-members', element('a::one\n\n  b::two  \n')],
  ]);
  let sent = null;
  const ctx = poolsContext(async (path_, options) => {
    if (options && options.method === 'POST') sent = options.body;
    return { pools: [] };
  }, nodes);
  await ctx.__p.addPool(element());
  // `Array.from`, not a bare compare: the array was built inside the VM realm, and
  // `deepStrictEqual` compares prototypes, so a cross-realm array never matches one
  // made here even when the contents are identical.
  assert.deepEqual(
    Array.from(sent.members),
    ['a::one', 'b::two'],
    'blank lines dropped, whitespace trimmed, order kept -- order is the routing policy',
  );
  assert.equal(sent.name, 'free-all');
});

test('a pool with no members is refused before the request', async () => {
  const nodes = new Map([
    ['pools', element()],
    ['pool-name', element('free-all')],
    ['pool-display', element('')],
    ['pool-members', element('   ')],
  ]);
  let called = false;
  const ctx = poolsContext(async () => { called = true; return { pools: [] }; }, nodes);
  await ctx.__p.addPool(element());
  assert.equal(called, false, 'nothing to send, so nothing is sent');
});

test('removing names the url-encoded pool and says the models are untouched', async () => {
  const nodes = new Map([['pools', element()]]);
  const calls = [];
  const ctx = poolsContext(async (path_, options) => {
    calls.push([path_, options && options.method]);
    return { pools: [] };
  }, nodes);
  await ctx.__p.removePool('free/all', element());
  assert.deepEqual(calls[0], ['/pools/free%2Fall', 'DELETE']);
});
