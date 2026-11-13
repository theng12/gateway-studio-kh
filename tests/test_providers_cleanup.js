// The Providers page, read the way a person meets it: four tabs, one search field, one primary
// action, one status line, a price summary that does not dump research on the page, and a footer
// pill where the version belongs. Headless: the page source and the real functions, no window.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');

function between(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  if (first < 0 || last < 0) throw new Error(`boundary changed: ${start}`);
  return source.slice(first, last);
}

function element(value = '') {
  return { value, innerHTML: '', textContent: '', hidden: false, className: '', style: {}, files: [] };
}

function page(nodes = {}) {
  const map = new Map(Object.entries(nodes));
  const get = id => {
    if (!map.has(id)) map.set(id, element());
    return map.get(id);
  };
  const ctx = vm.createContext({
    document: { getElementById: get, querySelectorAll: () => [] },
    esc: value => String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
    navIcon: () => '<svg></svg>',
    privacyBadge: () => '',
    state: { open: {}, models: {} }, location: { hash: '' },
    loadProviders() {}, loadConcurrency() {}, loadMeasured() {}, loadQueue() {}, loadPools() {},
    loadSfxSources() {}, loadExtensionWorkers() {}, loadModels() {}, loadFreshness() {},
    loadContentShelf() {}, loadCreate() {},
    requestAnimationFrame: fn => fn(),
  });
  vm.runInContext(between('function captureProviderDrafts()', 'async function loadProviders()'), ctx);
  vm.runInContext(between('function renderProviders(drafts', '// **Grouped by what they serve.**'), ctx);
  vm.runInContext(between('function closeMenus()', 'function providerGroupsHtml(list)'), ctx);
  vm.runInContext(between('function providerGroupsHtml(list)', 'function rateLimitLine('), ctx);
  vm.runInContext(between('function providerCard(p)', 'function providerModelList('), ctx);
  vm.runInContext(between('const PROVIDER_TABS', 'async function refreshAll('), ctx);
  vm.runInContext(between('function renderProviderStatus()', '// The Providers toolbar'), ctx);
  vm.runInContext(between('function priceRowHtml(p)', 'async function importEvidence'), ctx);
  return { ctx, get };
}

test('four tabs: Providers, SFX, Workers, Capacity — with the sidebar icons', () => {
  const tabs = between('const PROVIDER_TABS', 'let providerTab');
  for (const title of ['Providers', 'SFX', 'Workers', 'Capacity']) {
    assert.match(tabs, new RegExp(`title: '${title}'`));
  }
  for (const gone of ['Browser workers', 'Concurrency', 'Queue', 'Measured', 'Pools']) {
    assert.doesNotMatch(tabs, new RegExp(`title: '${gone}'`), `${gone} must not be a tab`);
  }
  assert.match(tabs, /icon: 'providers'/);
  assert.match(tabs, /icon: 'audio'/);
  assert.match(tabs, /icon: 'person'/);
  assert.match(tabs, /icon: 'analytics'/);
  // The four subjects are sections inside Capacity, in the pane the tab names.
  const capacity = between('<div class="pane" id="provider-pane-capacity">', '</div><!-- /provider-pane-capacity -->');
  for (const id of ['provider-capacity-queue', 'provider-capacity-measured', 'provider-capacity-pools']) {
    assert.match(capacity, new RegExp(`id="${id}"`), `${id} must be a section of Capacity`);
  }
});

test('the toolbar is one search field, one filter chip, one primary action and More', () => {
  const pane = between('<div class="pane" id="provider-pane-list"', '<div class="pane" id="provider-pane-sfx"');
  assert.match(pane, /id="filter"[^>]*type="search"|type="search"[^>]*id="filter"/);
  assert.doesNotMatch(pane, /id="key-filter"/, 'the old key dropdown is gone');
  assert.match(pane, /id="keyless-chip"/);
  assert.match(pane, /id="custom-provider-toggle"[^>]*>\+ Add a Provider/);
  assert.match(pane, /id="more-btn"/);
  const more = pane.slice(pane.indexOf('id="provider-more"'));
  assert.match(more, /Re-discover all providers/);
  assert.match(more, /Import Price Research/);
});

test('the keyless count is a clickable chip that filters, and clears itself', () => {
  const { ctx, get } = page({ filter: element(), providers: element(), 'keyless-chip': element() });
  const provider = (id, configured) => ({
    id, display_name: id, category: 'language', kind: 'openai_compatible', enabled: true,
    configured, configured_fields: [], modalities: ['chat'], generation_supported: true,
    model_count: 1, publishable_model_count: 1,
  });
  vm.runInContext(`state.providers = ${JSON.stringify([provider('aaa', true), provider('bbb', false)])}`, ctx);
  vm.runInContext('renderProviders()', ctx);
  assert.equal(get('keyless-chip').hidden, false);
  assert.match(get('keyless-chip').textContent, /1 without a key/);
  vm.runInContext('toggleKeylessFilter()', ctx);
  assert.equal(vm.runInContext('state.keyFilter', ctx), 'missing');
  assert.doesNotMatch(get('providers').innerHTML, /aaa/);
  assert.match(get('providers').innerHTML, /bbb/);
  // Once every provider has a key the chip disappears and the filter resets.
  vm.runInContext(`state.providers = ${JSON.stringify([provider('aaa', true)])}`, ctx);
  vm.runInContext('renderProviders()', ctx);
  assert.equal(get('keyless-chip').hidden, true);
  assert.equal(vm.runInContext('state.keyFilter', ctx), 'all');
});

test('one status line says what is down, why, and offers the place that acts', () => {
  const { ctx, get } = page();
  vm.runInContext('state.hub = null; state.localRunners = 0', ctx);
  vm.runInContext('renderProviderStatus()', ctx);
  assert.equal(get('provider-status').hidden, true, 'nothing to say, nothing drawn');

  vm.runInContext('state.hub = {running: false, probe: "did not answer within 2 s"}; state.localRunners = 8', ctx);
  vm.runInContext('renderProviderStatus()', ctx);
  const down = get('provider-status').innerHTML;
  assert.equal(get('provider-status').hidden, false);
  assert.match(down, /Studio Hub is not running — did not answer within 2 s/);
  assert.match(down, /8 local runners are managed on the Fleet page/);
  assert.match(down, /Open Fleet/);
  assert.equal((down.match(/Open Fleet/g) || []).length, 1, 'one action, not two');

  vm.runInContext('state.hub = {running: true, base_url: "http://127.0.0.1:47873"}; state.localRunners = 1', ctx);
  vm.runInContext('renderProviderStatus()', ctx);
  const up = get('provider-status').innerHTML;
  assert.match(up, /Studio Hub running\./);
  assert.doesNotMatch(up, /127\.0\.0\.1/, 'no endpoint on the surface');
  assert.doesNotMatch(up, /not running/);
});

test('prices are one sentence, and the rows are on demand', () => {
  const details = Object.assign(element(), { hidden: true });
  const { ctx, get } = page({ 'price-summary': element(), 'price-details': details });
  vm.runInContext(`state.evidence = {providers: [
    {provider_id: 'openai-direct', status: 'found', researched_at: '2026-10-09', accepted_prices: 3,
     accepted_rate_limits: 1, dropped: []},
    {provider_id: 'google-ai', status: 'partial', researched_at: '2026-10-04', accepted_prices: 2,
     accepted_rate_limits: 0, dropped: [{kind: 'price', model_id: 'x', unit: 'per_image', reason: 'no quote'}]}
  ], rate_limits: {}}`, ctx);
  vm.runInContext('renderPriceSummary()', ctx);
  const summary = get('price-summary');
  assert.equal(summary.style.display, '');
  assert.match(summary.innerHTML, /Prices on file for 2 providers, last researched 9 Oct 2026\./);
  assert.doesNotMatch(summary.innerHTML, /accepted, \d+ dropped/, 'no research dump on the page');
  assert.match(summary.innerHTML, /id="price-details"[^>]*hidden/);
  assert.match(summary.innerHTML, /Show details/);

  vm.runInContext('togglePriceDetails()', ctx);
  assert.equal(get('price-details').hidden, false);
  assert.match(summary.innerHTML, /openai-direct/, 'the rows live in the opened details');
  vm.runInContext('togglePriceDetails()', ctx);
  assert.equal(get('price-details').hidden, true);
});

test('version and What’s New live in the bottom-left pill, not a top row', () => {
  // The page's own sidebar footer mirrors the app's standard: a one-line pill that opens the
  // notes. The old top-of-page version row is gone.
  assert.match(source, /class="nav-pill" onclick="openNav\('whatsnew'\)"/);
  assert.match(source, /<span id="app-version">unknown<\/span>/);
  assert.doesNotMatch(source, /id="app-version"[^>]*>Version not read yet/,
    'the top-left version row is gone');
  assert.doesNotMatch(source, /<div class="row" style="margin-bottom:var\(--s16\)">\s*<span class="hint" id="app-version"/);
  // One door: the sidebar no longer carries a What's New row beside the pill.
  assert.doesNotMatch(source, /view: 'whatsnew', label:/);
});

test('a card hides its identity behind Configure and offers one Configure plus an overflow', () => {
  const card = between('function providerCard(p)', 'function providerModelList(');
  assert.doesNotMatch(card, /<div class="cat">/, 'id · kind · endpoint must not sit on every card');
  assert.match(card, /card-details/);
  assert.match(card, /toggleOpen\('\$\{esc\(p\.id\)\}'\)/, 'one Configure');
  assert.doesNotMatch(card, /refreshProvider\('\$\{esc\(p\.id\)\}', this\)[^\n]*<\/button>\s*<button class="sm" onclick="toggleOpen/,
    'Re-discover is not a second row button');
  assert.match(card, /class="badge bad chip-action"[^>]*onclick="configureProvider\('\$\{esc\(p\.id\)\}'\)"/,
    '"no key" must act, not just report');
  assert.match(card, /pmenu-\$\{esc\(p\.id\)\}/);
});
