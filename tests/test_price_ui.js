// 0.2.85 — the owner could not see prices in the app, and the Providers page opened on Concurrency.
// Both live in the console page (`gateway/admin_ui.html`) that the native Models / Providers pages
// show in a WKWebView, so these drive the page's own functions in a VM: no app, no screen, no network.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const between = (a, b) => {
  const i = source.indexOf(a), j = source.indexOf(b);
  assert.ok(i >= 0 && j > i, `page section moved: ${a}`);
  return source.slice(i, j);
};

// -- price cell --------------------------------------------------------------------------------
const priceCtx = vm.createContext({ esc, state: {} });
vm.runInContext(between('const PRICE_UNITS', 'function machineRow('), priceCtx);
const info = row => priceCtx.priceInfo(row);

test('price text for each price_source', () => {
  assert.equal(info({ price_source: 'local', price_usd: { per_image: 0 } }).text, 'Free (your Macs)');
  assert.equal(info({ price_source: 'unpublished', price_usd: null }).text, 'Not published');
  assert.equal(info({}).text, 'Not published', 'a row from an older gateway must not read as free');
  const chat = info({ price_source: 'provider', price_as_of: '2026-09-30T08:12:44+00:00',
    price_usd: { input_per_1m_tokens: 1.5, output_per_1m_tokens: 9, cached_input_per_1m_tokens: 0.15 } });
  assert.equal(chat.text, '$1.50 / $9.00 per 1M');
  assert.match(chat.title, /cached input \$0\.15/);
  assert.equal(chat.source, 'provider API, 2026-09-30');
  assert.equal(info({ price_source: 'provider', price_usd: { per_image: 0.02 } }).text, '$0.02 per image');
  assert.equal(info({ price_source: 'provider', price_usd: { per_second: 0.075 } }).text, '$0.075 per second');
  assert.equal(info({ price_source: 'provider', price_usd: { input_per_1m_tokens: 0, output_per_1m_tokens: 0 } }).text,
    'Free (provider states $0)');
});

test('a page price links to its page; several numbers say Varies and list every one', () => {
  const single = info({ price_source: 'provider_page', price_url: 'https://example.com/pricing',
    price_as_of: '2026-09-30', price_usd: { per_image: 0.04 },
    price_variants: [{ unit: 'per_image', variant: '', source_url: 'https://example.com/pricing', usd: 0.04 }] });
  assert.equal(single.text, '$0.04 per image');
  assert.equal(single.url, 'https://example.com/pricing');
  assert.equal(single.source, 'pricing page, 2026-09-30');
  assert.match(priceCtx.priceCell({ price_source: 'provider_page', price_url: 'https://example.com/pricing',
    price_usd: {}, price_variants: [] }), /<a href="https:\/\/example\.com\/pricing"/);

  const varies = info({ price_source: 'provider_page', price_url: 'https://api-docs.deepseek.com/pricing',
    price_as_of: '2026-09-30', price_usd: {},
    price_variants: [
      { unit: 'input_per_1m_tokens', variant: 'peak', source_url: 'u', usd: 0.6 },
      { unit: 'input_per_1m_tokens', variant: 'off-peak', source_url: 'u', usd: 0.3 },
      { unit: 'other', variant: '', source_url: 'u', unit_text: '$15.00 / M UTF-8 bytes' }] });
  assert.equal(varies.text, 'Varies — 3 prices');
  assert.match(varies.title, /peak: \$0\.60 per 1M tokens/);
  assert.match(varies.title, /off-peak: \$0\.30/);
  assert.match(varies.title, /\$15\.00 \/ M UTF-8 bytes/);

  const mixed = info({ price_source: 'provider_page', price_url: 'https://x.test/p', price_usd: { per_image: 0.04 },
    price_variants: [{ unit: 'per_image', variant: '', usd: 0.04 }, { unit: 'other', variant: '', unit_text: 'Free of charge' }] });
  assert.equal(mixed.text, '$0.04 per image · Varies — 2 prices');
});

test('a non-https price_url is never made a link, and prices are escaped', () => {
  assert.equal(info({ price_source: 'provider_page', price_url: 'javascript:alert(1)', price_usd: { per_image: 1 } }).url, '');
  assert.doesNotMatch(priceCtx.priceCell({ price_source: 'provider_page', price_url: 'http://x.test', price_usd: { per_image: 1 } }), /<a /);
});

// -- rate limits + evidence --------------------------------------------------------------------
test('rate-limit tiers render per provider from the evidence GET', () => {
  const ctx = vm.createContext({ esc, state: { evidence: { rate_limits: { 'openai-direct': [
    { tier: 'Tier 1', how_reached: '$5 paid', scope: 'per org', rpm: 500, tpm: 200000, rpd: null,
      source_url: 'https://platform.openai.com/docs/guides/rate-limits' }] } } } });
  vm.runInContext(between('function rateLimitLine', 'function providerCard('), ctx);
  const html = ctx.rateLimitsHtml('openai-direct');
  assert.match(html, /Rate limits \(from pricing research\)/);
  assert.match(html, /Tier 1<\/b> \(\$5 paid · per org\): 500 requests\/min, 200,000 tokens\/min/);
  assert.match(html, /href="https:\/\/platform\.openai\.com/);
  assert.equal(ctx.rateLimitsHtml('nobody'), '', 'no research, no section');
});

test('the Providers page carries the import button, the price column and the evidence read', () => {
  const list = between('id="provider-pane-list"', '</div><!-- /provider-pane-list -->');
  assert.match(list, /id="evidence-import"/);
  assert.match(list, /id="evidence-file"/);
  assert.match(source, /api\('\/pricing\/evidence'\)/);
  assert.match(between('<table>', 'id="m-rows"'), /<th[^>]*>Price<\/th>/);
});

test('the app can open the import file picker and pricing links', () => {
  // WKWebView shows no file picker unless the UI delegate supplies one, so the button was dead.
  const swift = fs.readFileSync(path.join(__dirname, '../menubar/MainWindow.swift'), 'utf8');
  assert.match(swift, /runOpenPanelWith parameters: WKOpenPanelParameters/);
  assert.match(swift, /createWebViewWith configuration: WKWebViewConfiguration/);
});

// -- Providers opens on the providers list -----------------------------------------------------
// Root cause: the HTML shows the Concurrency pane first, `providerTab` says 'list', and `setView`
// only painted the tab buttons — so the strip read "Providers" over the Concurrency pane until a tab
// was clicked. The nodes below start from the page's own initial `display` values.
function providersPage() {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, { id, style: {}, innerHTML: '', value: '', scrollIntoView() {} });
    return nodes.get(id);
  };
  for (const m of source.matchAll(/<div class="pane" id="(provider-pane-[a-z]+)"([^>]*)>/g)) {
    node(m[1]).style.display = m[2].includes('display:none') ? 'none' : '';
  }
  const ctx = vm.createContext({
    document: { getElementById: node }, esc, navIcon: () => '', state: {}, location: { hash: '' },
    requestAnimationFrame: fn => fn(),
    loadModels() {}, loadFreshness() {}, loadProviders() {}, loadConcurrency() {}, loadMeasured() {},
    loadQueue() {}, loadPools() {}, loadContentShelf() {}, loadCreate() {},
  });
  vm.runInContext(between('const PROVIDER_TABS', 'async function loadPools('), ctx);
  vm.runInContext(between('function setView(view)', 'const SHELF_KIND_LABELS'), ctx);
  const visible = () => ['list', 'sfx', 'extension-workers', 'capacity']
    .filter(k => node(`provider-pane-${k}`).style.display !== 'none');
  return { ctx, node, visible };
}

test('Providers opens on the providers list, on first and repeated activation', () => {
  const { ctx, visible } = providersPage();
  for (const round of [1, 2, 3]) {
    ctx.setView('providers');
    assert.deepEqual(visible(), ['list'], `activation ${round} showed ${visible()}`);
    ctx.setView('models');
  }
  assert.match(ctx.document.getElementById('provider-tabs').innerHTML,
    /data-tab="list" aria-pressed="true"/);
});

test('a sub-tab the owner chose this session is remembered; nothing else is', () => {
  const { ctx, visible } = providersPage();
  ctx.setView('providers');
  ctx.selectProviderTab('capacity');
  ctx.setView('models');
  ctx.setView('providers');
  assert.deepEqual(visible(), ['capacity']);
  ctx.selectProviderTab('list');
  ctx.setView('providers');
  assert.deepEqual(visible(), ['list']);
  ctx.location.hash = '#queue';
  ctx.setView('providers');
  assert.deepEqual(visible(), ['capacity'], 'the #queue link opens the Capacity tab that carries it');
});
