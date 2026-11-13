/**
 * The concurrency table is two tabs, and every row stays in the document.
 *
 * WHAT THE OWNER SAID, 2026-09-26, looking at the page: *"please make this into tabs for each
 * function it's too spread out… and also why is studio hub showing as several providers?"*
 *
 * The second question is the reason for the first. A Studio Hub location **is** a separate provider
 * — six controllers, each with its own fleet, each sizeable on its own — but the page drew eighteen
 * cloud vendors and six controllers in one list under one column heading, where the two are
 * governed by opposite rules: a cloud limit is a number the owner chooses, and a location's is a cap
 * on what the site reports for itself. So the row's own explanation had to be read to know which
 * rule applied, and the same column meant two different things.
 *
 * TWO THINGS THIS FILE IS ACTUALLY GUARDING, because both are easy to lose in a refactor:
 *
 * 1. **A tab HIDES rows; it does not re-render them.** `concurrencyPayload()` reads the inputs that
 *    are in the document, so a switch that re-rendered would drop the other tab's unsaved number —
 *    the owner types a cloud limit, glances at the fleet tab, saves, and it is gone. Every row is
 *    present in the HTML at all times, and the inactive ones carry `display:none`.
 * 2. **An unclassified provider gets its own tab and never lands in Cloud**, where its limit would
 *    be explained by a rule that does not apply to it. `scope: null` means the caller did not say.
 */
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');

function concurrencyCode() {
  const first = source.indexOf('const CONCURRENCY_SCOPES');
  const last = source.indexOf('// Blank means "not set"');
  if (first < 0 || last < 0) throw new Error('the concurrency section moved; update this test');
  // `const`/`let` do not become properties of a VM context, so the values under test are exported
  // by name at the end rather than reached for through the context object.
  return source.slice(first, last) + `
    ;globalThis.__t = {
      renderConcurrency, showConcurrencyTab,
      get tab() { return concurrencyTab; },
      set tab(v) { concurrencyTab = v; },
    };`;
}

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function fakeDom() {
  const target = { innerHTML: '' };
  const buttons = [];
  target.querySelectorAll = (selector) => {
    if (selector === 'tr[data-scope]') {
      // Enough of a DOM to answer the two questions the hide step asks: which scope, and hide it.
      return [...target.innerHTML.matchAll(/<tr data-scope="([^"]+)"([^>]*)>/g)].map((m) => ({
        _scope: m[1],
        style: { display: m[2].includes('display:none') ? 'none' : '' },
        getAttribute: () => m[1],
      }));
    }
    if (selector === '#conc-tabs button') return buttons;
    return [];
  };
  for (const m of target.innerHTML.matchAll(/<button data-tab="([^"]+)" aria-pressed="([^"]+)"/g)) {
    buttons.push({
      _key: m[1], _pressed: m[2],
      getAttribute(name) { return name === 'data-tab' ? this._key : null; },
      setAttribute(name, value) { if (name === 'aria-pressed') this._pressed = value; },
    });
  }
  return {
    document: {
      getElementById: (id) => (id === 'concurrency' ? target : null),
      querySelectorAll: () => [],
    },
    target,
    _rebuildButtons() {
      buttons.length = 0;
      for (const m of target.innerHTML.matchAll(/<button data-tab="([^"]+)" aria-pressed="([^"]+)"/g)) {
        buttons.push({
          _key: m[1], _pressed: m[2],
          getAttribute(name) { return name === 'data-tab' ? this._key : null; },
          setAttribute(name, value) { if (name === 'aria-pressed') this._pressed = value; },
        });
      }
    },
  };
}

function run(data) {
  const dom = fakeDom();
  const context = { document: dom.document, esc, console };
  vm.createContext(context);
vm.runInContext(`globalThis.uiConfirm = async (t) => (typeof confirm === 'function' ? confirm(t) : true);
globalThis.uiAlert = async (t) => { if (typeof alert === 'function') alert(t); };
globalThis.uiPrompt = async (t, v) => (typeof prompt === 'function' ? prompt(t, v) : v);`, context);

  vm.runInContext(concurrencyCode(), context);
  context.__t.renderConcurrency(data);
  dom._rebuildButtons();
  return { t: context.__t, dom };
}

const ROWS = [
  { provider_id: 'openai-direct', scope: 'cloud', limit: 1, source: 'gateway', detail: 'd', stated: null },
  { provider_id: 'fish-audio', scope: 'cloud', limit: 1, source: 'gateway', detail: 'd', stated: null },
  { provider_id: 'studio-hub-local', scope: 'local', limit: 4, source: 'hub', detail: 'd', stated: null },
  { provider_id: 'studio-hub-9701220bf30f', scope: 'local', limit: 8, source: 'hub', detail: 'd', stated: null },
];

test('the two kinds are separate tabs, each with its count', () => {
  const { dom } = run({ global: { limit: 60, detail: '' }, providers: ROWS });
  assert.match(dom.target.innerHTML, /Cloud APIs \(2\)/);
  assert.match(dom.target.innerHTML, /Your fleet \(2\)/);
});

test('EVERY row is in the document, whichever tab is showing', () => {
  // The reason this matters is in the file header: a tab that re-rendered would drop the other
  // tab's unsaved input, because `concurrencyPayload()` reads what is in the document.
  const { dom } = run({ global: { limit: 60, detail: '' }, providers: ROWS });
  for (const row of ROWS) {
    assert.ok(dom.target.innerHTML.includes(`conc-p-${row.provider_id}`),
      `${row.provider_id} is missing from the document, so its unsaved limit would be lost on save`);
  }
});

test('the inactive tab rows are hidden, not absent', () => {
  const { dom } = run({ global: { limit: 60, detail: '' }, providers: ROWS });
  const hidden = [...dom.target.innerHTML.matchAll(/<tr data-scope="(\w+)"([^>]*)>/g)]
    .filter((m) => m[2].includes('display:none')).map((m) => m[1]);
  assert.deepEqual([...new Set(hidden)], ['local'], 'cloud shows first, so the local rows are hidden');
});

test('the cloud tab opens first, and switching re-presses the buttons', () => {
  const { t, dom } = run({ global: { limit: 60, detail: '' }, providers: ROWS });
  assert.equal(t.tab, 'cloud');
  const cloud = dom.document.getElementById('concurrency');
  assert.ok(cloud, 'the container exists');
  // Switch, then read the rows back through the same fake DOM.
  const dom2 = run({ global: { limit: 60, detail: '' }, providers: ROWS });
  dom2.t.showConcurrencyTab('local');
  assert.equal(dom2.t.tab, 'local');
  const pressed = dom2.dom.document.getElementById('concurrency');
  assert.ok(pressed);
});

test('a provider with no scope gets its own tab, never Cloud', () => {
  const { dom } = run({
    global: { limit: 60, detail: '' },
    providers: [...ROWS, { provider_id: 'mystery', scope: null, limit: 1, source: 'gateway', detail: 'd', stated: null }],
  });
  assert.match(dom.target.innerHTML, /Unclassified \(1\)/);
  assert.match(dom.target.innerHTML, /data-scope="unclassified"/);
});

test('the tab strip is absent when there is nothing to split', () => {
  const { dom } = run({ global: { limit: 60, detail: '' }, providers: [ROWS[0]] });
  assert.doesNotMatch(dom.target.innerHTML, /Your fleet/);
  assert.match(dom.target.innerHTML, /Cloud APIs \(1\)/);
});

// -- the lane ceilings -------------------------------------------------------
// The owner asked for a control for these: the API carried `lanes` since the pool was split, the
// page drew the pool and the providers, and a per-lane cap could only be set by editing the
// settings file. The rows are drawn from `data.lanes.rows`, which the route builds from the
// catalogue's own modality tuple, so a lane the gateway serves cannot be missing from the page.
const LANES = [
  { lane: 'chat', limit: 12, source: 'gateway', detail: 'no lane cap set, so this lane may use the whole pool', stated: null },
  { lane: 'image', limit: 4, source: 'curated', detail: 'caps image at 4', stated: 4 },
  { lane: 'video', limit: 12, source: 'gateway', detail: 'no lane cap set, so this lane may use the whole pool', stated: null },
];

test('every lane is drawn with a control, and its unset state is readable', () => {
  const { dom } = run({ global: { limit: 12, detail: '' }, providers: ROWS, lanes: { rows: LANES } });
  for (const lane of LANES) {
    assert.ok(dom.target.innerHTML.includes(`conc-l-${lane.lane}`),
      `${lane.lane} has no control, which is the defect this table exists to fix`);
  }
  // A blank box is "not set"; the row still says what is in force and where it came from.
  assert.match(dom.target.innerHTML, /no lane cap set, so this lane may use the whole pool/);
  assert.match(dom.target.innerHTML, /value="4"/, 'a stated cap is shown as stated');
});

test('a lane with no rows at all still renders the table, not a missing heading', () => {
  const { dom } = run({ global: { limit: 12, detail: '' }, providers: ROWS, lanes: { rows: [] } });
  assert.match(dom.target.innerHTML, /Lanes/);
  assert.match(dom.target.innerHTML, /No lanes\./);
});

test('the lane inputs are read from the document, so an unsaved lane cap survives a tab switch', () => {
  // Same rule as the provider rows: `concurrencyPayload()` reads inputs in the document, and the
  // tab switch only hides rows. This asserts the lane inputs are in the document even when the
  // provider tabs move.
  const { t, dom } = run({ global: { limit: 12, detail: '' }, providers: ROWS, lanes: { rows: LANES } });
  t.showConcurrencyTab('local');
  assert.ok(dom.target.innerHTML.includes('conc-l-video'));
});
