// The SFX tab and the Providers page's headings — driven in a VM over the page's own source.
//
// No app, no gateway, no network: `admin_ui.html`'s functions are run against a hand-built DOM, so
// what is asserted here is the drawing code itself rather than a rendering of it.
//
// Four things this pane must not get wrong, all of them ways a key field goes quietly wrong:
//
//   1. **A stored secret is never drawn.** The server sends a mask; the input stays empty and the
//      mask is only ever a placeholder. A `value="…"` carrying the key back into the page is the
//      defect, and it is invisible in a screenshot.
//   2. **An untouched field is not sent.** A body carrying an empty secret means "clear it" on the
//      other side, so a Save after loading the page would delete a key nobody touched.
//   3. **A redraw before the message, not after.** `renderSfxSources` replaces every card,
//      including the span the result is written into — so a result written first is wiped by the
//      redraw it triggered, and the button appears to report nothing.
//   4. **No reading is not zero.** Quota comes from the upstream's headers; before a search there
//      is nothing to print, and the card says so instead of drawing 0.

const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');

function between(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + 1);
  if (first < 0 || last < 0) throw new Error(`admin_ui.html moved: ${start}`);
  return source.slice(first, last);
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function element(value = '') {
  return {
    value, innerHTML: '', textContent: '', className: '', disabled: false, style: {},
    dataset: {}, querySelector: () => null,
  };
}

const SECRET = 'SECRET-token-abcdef123456';

function openverseSource(overrides = {}) {
  return {
    id: 'openverse',
    display_name: 'Openverse',
    kind: 'openverse',
    category: 'sfx',
    description: 'Free, commercial-safe audio.',
    docs_url: 'https://docs.openverse.org/',
    register_url: 'https://api.openverse.org/v1/',
    notes: 'Anonymous search works; a registered token raises the rate limit.',
    configured: true,
    registered: true,
    quota: { scopes: {}, last_seen_at: null, exhausted: false },
    credential_fields: [
      { key: 'access_token', label: 'Access token', secret: true, present: true, used: true,
        source: 'stored', masked: '{{SE••••••it}}', env_var: 'OPENVERSE_ACCESS_TOKEN',
        placeholder: 'paste the token', help_text: 'This is the one the gateway sends.' },
      { key: 'client_id', label: 'Client ID', secret: false, present: true, used: false,
        source: 'stored', masked: 'cid-1234', env_var: 'OPENVERSE_CLIENT_ID',
        placeholder: '', help_text: 'Not sent.' },
    ],
    ...overrides,
  };
}

/// The SFX pane's functions, with the DOM they touch.
///
/// The pane's state lives in a `let` inside the page's own script, which is not a property of the
/// VM context — so the harness reaches it through an explicit handle rather than by assignment,
/// and drives the real functions rather than a copy of them.
function sfxHarness(responses = [{}]) {
  const nodes = new Map([
    ['sfx-sources', element()], ['sfx-msg', element()], ['sfx-msg-openverse', element()],
  ]);
  const inputs = [];
  const calls = [];
  let index = 0;
  const context = vm.createContext({
    document: {
      getElementById: id => nodes.get(id),
      querySelectorAll: selector =>
        (selector === '#sfx-sources input[data-sfx="openverse"][data-field]' ? inputs : []),
    },
    esc, console, confirm: () => true,
    api: async (page, options) => {
      calls.push({ page, options });
      const next = responses[Math.min(index++, responses.length - 1)];
      if (next instanceof Error) throw next;
      return next;
    },
  });
  vm.runInContext(between('// SFX — sound sources', '// Adding a provider without editing a file')
    + ';globalThis.__sfx = {'
    + ' setSources: list => { sfxSources = list; },'
    + ' sources: () => sfxSources,'
    + ' render: () => renderSfxSources(),'
    + ' load: () => loadSfxSources(),'
    + ' save: (id, btn) => saveSfxCreds(id, btn),'
    + ' clear: (id, btn) => clearSfxCreds(id, btn),'
    + ' quota: source => sfxQuota(source) };', context);
  return { context, sfx: context.__sfx, nodes, inputs, calls };
}

// ---------------------------------------------------------------------------
// The tab and its pane
// ---------------------------------------------------------------------------
test('the SFX tab and its pane both exist, and are named by one list', () => {
  const tabs = between('const PROVIDER_TABS', 'let providerTab');
  assert.match(tabs, /key: 'sfx'/, 'the SFX tab is gone from the strip');
  assert.match(source, /id="provider-pane-sfx"/, 'the tab opens nothing');
  assert.match(between('function setView(view)', 'const SHELF_KIND_LABELS'),
    /selectProviderTab\('sfx'\)/, '#sfx does not open the SFX pane');
});

// ---------------------------------------------------------------------------
// Drawing a key field
// ---------------------------------------------------------------------------
test('a stored secret is masked and never drawn into the input', () => {
  const harness = sfxHarness();
  harness.sfx.setSources([openverseSource()]);
  harness.sfx.render();
  const html = harness.nodes.get('sfx-sources').innerHTML;

  assert.match(html, /Access token/);
  assert.match(html, /placeholder="\{\{SE••••••it\}\}"/, 'the mask must be visible as the placeholder');
  assert.doesNotMatch(html, new RegExp(`value="[^"]*${SECRET.slice(2, 10)}`),
    'a secret must never be written back into a value attribute');
  assert.match(html, /data-field="access_token"/, 'the field the adapter sends must be submittable');
  assert.match(html, /id="sfx-msg-openverse"/, 'the card must have somewhere to report a save');
  assert.match(html, /key set/);
  assert.match(html, /https:\/\/api\.openverse\.org\/v1\//, 'the registration link is not drawn');
});

test('a non-https link is not turned into an href', () => {
  const harness = sfxHarness();
  harness.sfx.setSources([openverseSource({ register_url: 'javascript:alert(1)' })]);
  harness.sfx.render();
  const html = harness.nodes.get('sfx-sources').innerHTML;
  assert.doesNotMatch(html, /javascript:/);
  assert.match(html, /Source documentation/, 'the https link beside it still renders');
});

test('quota says "not read yet" rather than zero before any search', () => {
  const harness = sfxHarness();
  harness.sfx.setSources([openverseSource()]);
  harness.sfx.render();
  let html = harness.nodes.get('sfx-sources').innerHTML;
  assert.match(html, /not read yet/);
  assert.doesNotMatch(html, /0 left/, 'an unread quota must not be drawn as zero');

  harness.sfx.setSources([openverseSource({
    quota: { scopes: { anon_burst: { limit: '20/min', available: '19' } }, last_seen_at: 1793000000 },
  })]);
  harness.sfx.render();
  html = harness.nodes.get('sfx-sources').innerHTML;
  assert.match(html, /19 left of 20\/min/);
  assert.doesNotMatch(html, /not read yet/);
});

// ---------------------------------------------------------------------------
// Saving
// ---------------------------------------------------------------------------
test('saving sends only the fields that were typed into', async () => {
  // The provider form posts every input, empty ones included, and an empty value means "clear
  // this field" on the server — so saving untouched would delete a stored key.
  const saved = { source: openverseSource({ registered: false }) };
  const harness = sfxHarness([saved]);
  harness.sfx.setSources([openverseSource()]);
  harness.inputs.push(
    Object.assign(element(''), { dataset: { field: 'access_token' } }),   // untouched
    Object.assign(element('cid-9'), { dataset: { field: 'client_id' } }),
  );
  harness.sfx.render();

  await harness.sfx.save('openverse', element());

  const call = harness.calls[0];
  assert.equal(call.page, '/sounds/sources/openverse/credentials');
  assert.equal(call.options.method, 'POST');
  assert.deepEqual(JSON.parse(call.options.body), { values: { client_id: 'cid-9' } },
    'an untouched field leaked into the body, which clears it');
});

test('a save with nothing typed sends nothing and says so', async () => {
  const harness = sfxHarness();
  harness.sfx.setSources([openverseSource()]);
  harness.inputs.push(Object.assign(element(''), { dataset: { field: 'access_token' } }));
  harness.sfx.render();

  await harness.sfx.save('openverse', element());

  assert.equal(harness.calls.length, 0, 'an empty form must not post');
  assert.match(harness.nodes.get('sfx-msg-openverse').textContent, /nothing to save/);
});

test('the card is redrawn from the answer before the result is reported', async () => {
  const harness = sfxHarness([{ source: openverseSource({ registered: true }) }]);
  harness.sfx.setSources([openverseSource({ registered: false })]);
  harness.inputs.push(Object.assign(element(SECRET), { dataset: { field: 'access_token' } }));
  harness.sfx.render();

  await harness.sfx.save('openverse', element());

  assert.match(harness.nodes.get('sfx-msg-openverse').textContent, /searches now use the access token/);
  assert.match(harness.nodes.get('sfx-sources').innerHTML, /key set/,
    'the card must be drawn from the server\'s answer, not the click');
  assert.doesNotMatch(harness.nodes.get('sfx-sources').innerHTML, new RegExp(SECRET.slice(2, 10)),
    'the typed secret must not survive the redraw');
});

test('clearing re-reads the list instead of guessing what is left', async () => {
  const harness = sfxHarness([{}, { sources: [openverseSource({ registered: false })] }]);
  harness.sfx.setSources([openverseSource()]);
  await harness.sfx.clear('openverse', element());

  assert.equal(harness.calls[0].page, '/sounds/sources/openverse/credentials');
  assert.equal(harness.calls[0].options.method, 'DELETE');
  assert.equal(harness.calls[1].page, '/sounds/sources');
  assert.match(harness.nodes.get('sfx-msg-openverse').textContent, /anonymous quota/);
  assert.match(harness.nodes.get('sfx-sources').innerHTML, /anonymous quota/,
    'the card must show the state the server answered with');
});

test('a failed save reports the refusal and keeps the card', async () => {
  const harness = sfxHarness([new Error('unknown field(s): api_key')]);
  harness.sfx.setSources([openverseSource()]);
  harness.inputs.push(Object.assign(element(SECRET), { dataset: { field: 'access_token' } }));
  await harness.sfx.save('openverse', element());

  assert.equal(harness.nodes.get('sfx-msg-openverse').className, 'msg bad');
  assert.match(harness.nodes.get('sfx-msg-openverse').textContent, /unknown field/);
});

// ---------------------------------------------------------------------------
// The Providers page's headings
// ---------------------------------------------------------------------------
test('providers are drawn under the headings the server names, in its order', () => {
  const context = vm.createContext({
    esc, state: {
      providerGroups: [
        { key: 'chat', label: 'Text & chat', ids: ['alpha'] },
        { key: 'tts', label: 'Voice & TTS', ids: ['beta'] },
      ],
    },
    providerCard: p => `<card id="${p.id}">`,
  });
  vm.runInContext(between('function providerGroupsHtml', 'function rateLimitLine'), context);
  const html = context.providerGroupsHtml([{ id: 'alpha' }, { id: 'beta' }]);

  assert.match(html, /Text &amp; chat/);
  assert.ok(html.indexOf('Text &amp; chat') < html.indexOf('Voice &amp; TTS'), 'headings are re-ordered');
  assert.match(html, /<card id="alpha">/);
  assert.match(html, /<card id="beta">/);
  assert.equal((html.match(/<h3/g) || []).length, 2, 'one heading per group that has members');
});

test('a provider no heading names is still drawn, never dropped', () => {
  const context = vm.createContext({
    esc, state: { providerGroups: [{ key: 'chat', label: 'Text & chat', ids: ['alpha'] }] },
    providerCard: p => `<card id="${p.id}">`,
  });
  vm.runInContext(between('function providerGroupsHtml', 'function rateLimitLine'), context);
  const html = context.providerGroupsHtml([{ id: 'alpha' }, { id: 'lonely' }]);
  assert.match(html, /<card id="lonely">/);
  assert.match(html, /Other/);
});

test('an empty group is not drawn, and no grouping still draws the flat list', () => {
  const context = vm.createContext({
    esc, state: { providerGroups: [
      { key: 'chat', label: 'Text & chat', ids: ['alpha'] },
      { key: 'music', label: 'Music', ids: [] },
    ] },
    providerCard: p => `<card id="${p.id}">`,
  });
  vm.runInContext(between('function providerGroupsHtml', 'function rateLimitLine'), context);
  assert.doesNotMatch(context.providerGroupsHtml([{ id: 'alpha' }]), /Music/);

  const flat = vm.createContext({
    esc, state: {}, providerCard: p => `<card id="${p.id}">`,
  });
  vm.runInContext(between('function providerGroupsHtml', 'function rateLimitLine'), flat);
  const html = flat.providerGroupsHtml([{ id: 'alpha' }, { id: 'beta' }]);
  assert.doesNotMatch(html, /<h3/, 'an ungrouped response must not invent a heading');
  assert.match(html, /<card id="alpha"><card id="beta">/);
});

test('the provider filter can search by a lane, not only by name', () => {
  const render = between('function renderProviders', 'function providerGroupsHtml');
  assert.match(render, /modalities/,
    'a page grouped by first lane must still be searchable by the others');
});
