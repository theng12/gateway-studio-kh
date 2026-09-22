const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');

const READY_SHELF = {
  configured: true,
  available: true,
  detail: 'Private shelf is ready.',
  authority: {
    account_id: 'account.fixture',
    key_id: 'key.fixture',
    public_key_fingerprint: 'fingerprint.fixture',
    created_at: '2026-09-21T00:00:00Z',
    baseline_revision: 42,
  },
  supported_kinds: ['series-formula', 'topic-pack', 'visual-style', 'thumbnail-recipe'],
  releases: [],
};

function element(value = '') {
  return {
    value,
    textContent: '',
    innerHTML: '',
    className: '',
    disabled: false,
    checked: false,
    hidden: false,
    readOnly: false,
    dataset: {},
    style: {},
    select() {},
  };
}

function harness(api, overrides = {}) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  };
  const start = source.indexOf('const SHELF_KIND_LABELS = {');
  const end = source.indexOf('async function loadStatus()', start);
  assert.ok(start >= 0 && end > start, 'content shelf controller must exist');
  const context = vm.createContext({
    document: {
      getElementById: node,
      querySelectorAll: () => [],
      execCommand: () => true,
    },
    api,
    esc: value => String(value ?? '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c])),
    adminHeaders: extra => extra || {},
    confirm: () => true,
    JSON,
    Promise,
    Set,
    console,
    navigator: {clipboard: {writeText: async () => {}}},
    window: {addEventListener() {}},
    ...overrides,
  });
  vm.runInContext(source.slice(start, end), context);
  return {context, node, state: () => vm.runInContext('shelfState', context)};
}

test('unconfigured setup stays gated until the owner acknowledges it', async () => {
  const calls = [];
  const unconfigured = {
    configured: false,
    available: true,
    detail: 'Ready to initialize a private authority after checking the Gateway key and Story tools.',
    authority: null,
    supported_kinds: READY_SHELF.supported_kinds,
    releases: [],
  };
  const harnessValue = harness(async (pathName, options) => {
    calls.push([pathName, options]);
    if (options?.method === 'POST') return READY_SHELF;
    return unconfigured;
  });

  await harnessValue.context.loadContentShelf();
  assert.match(harnessValue.node('shelf-detail').textContent, /Ready to initialize/);
  assert.equal(harnessValue.node('shelf-setup-ack').checked, false);
  assert.equal(harnessValue.node('shelf-setup').disabled, true);

  harnessValue.node('shelf-setup-ack').checked = true;
  harnessValue.context.updateShelfControls();
  assert.equal(harnessValue.node('shelf-setup').disabled, false);

  await harnessValue.context.setupContentShelf();
  const setupCall = calls.find(([, options]) => options?.method === 'POST');
  assert.equal(setupCall[0], '/content-shelf/setup');
  assert.deepEqual(JSON.parse(setupCall[1].body), {acknowledge: true});
});

test('an uncertain setup failure asks for refresh without claiming durable state was unchanged', async () => {
  const unconfigured = {
    configured: false,
    available: true,
    detail: 'Ready to initialize a private authority.',
    authority: null,
    supported_kinds: READY_SHELF.supported_kinds,
    releases: [],
  };
  const h = harness(async (pathName, options) => {
    if (options?.method === 'POST') throw Error('connection lost after dispatch');
    return unconfigured;
  });

  await h.context.loadContentShelf();
  h.node('shelf-setup-ack').checked = true;
  h.context.updateShelfControls();
  await h.context.setupContentShelf();

  assert.match(h.node('shelf-message').textContent, /Setup did not complete/);
  assert.match(h.node('shelf-message').textContent, /Refresh to inspect its state/);
  assert.match(h.node('shelf-message').textContent, /nothing was published or installed/);
  assert.doesNotMatch(h.node('shelf-message').textContent, /No authority was changed/);
  assert.equal(h.state().data, null);
});

test('shelf refresh renders all supported kinds and escapes release metadata', async () => {
  const shelf = {
    ...READY_SHELF,
    releases: [{
      release_id: 'release.fixture',
      pack_id: '<img src=x onerror=alert(1)>',
      version: '1.0.0',
      archive_sha256: 'a'.repeat(64),
      bytes: 1200,
      title: '<script>bad</script>',
      kind: 'topic-pack',
      current: true,
      available: true,
      published_at: '2026-09-21T00:00:00Z',
    }],
  };
  const h = harness(async () => shelf);

  await h.context.loadContentShelf();
  const html = h.node('shelf-releases').innerHTML;
  assert.match(h.node('shelf-supported').textContent, /series-formula/);
  assert.match(h.node('shelf-supported').textContent, /topic-pack/);
  assert.match(h.node('shelf-supported').textContent, /visual-style/);
  assert.match(h.node('shelf-supported').textContent, /thumbnail-recipe/);
  assert.match(html, /&lt;script&gt;bad&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>bad<\/script>/);
  assert.doesNotMatch(html, /onerror=alert/);
  assert.match(html, /Current release/);
  assert.match(html, /not installed/i);
});

test('a failed shelf refresh clears the last ready state', async () => {
  let fail = false;
  const h = harness(async pathName => {
    if (pathName === '/content-shelf' && fail) throw Error('shelf status unavailable');
    return READY_SHELF;
  });

  await h.context.loadContentShelf();
  assert.equal(h.state().data.configured, true);
  fail = true;
  await h.context.loadContentShelf();

  assert.equal(h.state().data, null);
  assert.match(h.node('shelf-message').textContent, /status unavailable/);
});

test('a pack someone sent is added to the shelf in one action', async () => {
  const calls = [];
  const h = harness(async (pathName, options) => {
    calls.push([pathName, (options && options.method) || 'GET']);
    if (pathName === '/content-shelf') return READY_SHELF;
    if (pathName === '/content-library/import-and-publish') {
      return {release: {pack_id: 'com.fixture.pack', title: 'Sent Pack'}, archive: {}};
    }
    return {};
  });
  const input = h.node('received-pack-files');
  input.files = [{name: 'sent-pack.sspack', size: 1024}];
  const button = element();

  await h.context.addReceivedPack(button);

  // One request, and the one that publishes: the person who was sent a pack is
  // not asked to know that preserving and publishing are separate steps.
  assert.deepEqual(calls.filter(([, method]) => method === 'POST'),
    [['/content-library/import-and-publish', 'POST']]);
  assert.equal(button.disabled, false);
  assert.equal(input.value, '');
  assert.match(h.node('received-pack-message').textContent, /available to Story Studio/i);
});

test('adding a pack asks for a pack file rather than posting anything else', async () => {
  const calls = [];
  const h = harness(async pathName => { calls.push(pathName); return {}; });
  const input = h.node('received-pack-files');
  input.files = [{name: 'notes.txt', size: 10}];
  const button = element();

  await h.context.addReceivedPack(button);

  assert.deepEqual(calls, []);
  assert.equal(button.disabled, false);
  assert.match(h.node('received-pack-message').textContent, /Choose a \.sspack file/);
});

test('a pack the shelf refuses is reported and the control comes back', async () => {
  const h = harness(async pathName => {
    if (pathName === '/content-shelf') return READY_SHELF;
    if (pathName === '/content-library/import-and-publish') {
      throw new Error('Historical pack identities cannot enter the private shelf.');
    }
    return {};
  });
  const input = h.node('received-pack-files');
  input.files = [{name: 'old-pack.sspack', size: 1024}];
  const button = element();

  await h.context.addReceivedPack(button);

  assert.equal(button.disabled, false);
  assert.match(h.node('received-pack-message').textContent, /Not added/);
  assert.match(h.node('received-pack-message').textContent, /Historical pack identities/);
});

test('a shelf with no setup yet shows the one setup step and nothing else', async () => {
  const h = harness(async pathName => {
    if (pathName === '/content-shelf') {
      return {...READY_SHELF, configured: false, available: true, releases: []};
    }
    return {};
  });
  await h.context.loadContentShelf();

  assert.equal(h.node('packs-panel-setup').hidden, false);
  assert.equal(h.node('packs-panel-shelved').hidden, true);
});

test('once set up, the only view is the packs and the one add action', async () => {
  const h = harness(async pathName => {
    if (pathName === '/content-shelf') return READY_SHELF;
    return {};
  });
  await h.context.loadContentShelf();

  assert.equal(h.node('packs-panel-shelved').hidden, false);
  assert.equal(h.node('packs-panel-setup').hidden, true);
  assert.match(h.node('packs-intro').textContent, /available to Story Studio/);
});

test('the full console script remains valid JavaScript', () => {
  new vm.Script(source.match(/<script>([\s\S]*)<\/script>/)[1]);
});

test('table row hover follows the appearance instead of a fixed dark literal', () => {
  // A hardcoded dark background outside the dark media query made light-appearance
  // rows black-on-black. Every surface must come from a Canvas/CanvasText token.
  const rule = source.match(/tbody tr:hover\s*\{([^}]*)\}/);
  assert.ok(rule, 'the table hover rule must exist');
  assert.doesNotMatch(rule[1], /#[0-9a-fA-F]{3,8}\b/, 'hover must not use a fixed hex colour');
  assert.match(rule[1], /color-mix\(|var\(--/, 'hover must use a theme token');
});
