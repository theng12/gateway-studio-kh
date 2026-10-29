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
    open: false,
    readOnly: false,
    dataset: {},
    attrs: {},
    style: {},
    select() {},
    setAttribute(name, value) {
      this.attrs[name] = String(value);
      if (name === 'open') this.open = true;
    },
    removeAttribute(name) {
      delete this.attrs[name];
      if (name === 'open') this.open = false;
    },
    showModal() { this.open = true; this.attrs.open = ''; },
    close() { this.open = false; delete this.attrs.open; },
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

test('the archive bank is outside the shelf panels that setup toggles', () => {
  const span = (marker) => {
    const at = source.indexOf(marker);
    assert.ok(at > 0, `${marker} must exist`);
    const open = source.lastIndexOf('<div', at);
    const tags = /<div\b|<\/div>/g;
    tags.lastIndex = open;
    let depth = 0;
    let match;
    while ((match = tags.exec(source))) {
      depth += match[0] === '</div>' ? -1 : 1;
      if (depth === 0) return [open, tags.lastIndex];
    }
    throw new Error(`unclosed panel: ${marker}`);
  };
  const bank = source.indexOf('id="archive-bank-section"');
  assert.ok(bank > 0, 'the archive bank section must exist');
  for (const marker of ['id="packs-panel-setup"', 'id="packs-panel-shelved"']) {
    const [start, end] = span(marker);
    assert.ok(bank < start || bank > end, `archive bank must not be hidden inside ${marker}`);
  }
});

test('the archive bank stays available with no shelf configured', async () => {
  const h = harness(async pathName => pathName === '/content-shelf'
    ? {...READY_SHELF, configured: false, available: true, releases: []}
    : {archives: []});
  await h.context.loadContentShelf();

  assert.equal(h.node('packs-panel-setup').hidden, false);
  assert.equal(h.node('archive-bank-section').hidden, false);
  assert.equal(h.node('archive-preserve-add').disabled, false);
});

test('preserving an archive posts only to the archive bank and never sets up or publishes the shelf', async () => {
  const calls = [];
  const h = harness(async (pathName, options) => {
    calls.push([pathName, (options && options.method) || 'GET']);
    if (pathName === '/content-library') return {archives: []};
    if (pathName === '/content-library/import') {
      return {archive_sha256: 'a'.repeat(64), bytes: 1024, title: 'Portable Pack'};
    }
    return {};
  });
  const input = h.node('archive-preserve-files');
  input.files = [{name: 'portable.sspack', size: 1024}];
  const button = element();
  button.textContent = 'Preserve Archives';

  await h.context.preservePackArchives(button);

  assert.deepEqual(calls.filter(([, method]) => method === 'POST'),
    [['/content-library/import', 'POST']]);
  assert.equal(calls.some(([pathName]) => /setup|import-and-publish/.test(pathName)), false);
  assert.equal(button.disabled, false);
  assert.equal(input.value, '');
  assert.match(h.node('archive-preserve-message').textContent, /1 archive preserved/);
  assert.match(h.node('archive-preserve-message').textContent, /Nothing was added to the shelf or published/);
});

test('a pack over the archive ceiling is refused before it is posted', async () => {
  const calls = [];
  const h = harness(async (pathName, options) => {
    calls.push([pathName, (options && options.method) || 'GET']);
    return pathName === '/content-library' ? {archives: []} : {};
  });
  const input = h.node('archive-preserve-files');
  input.files = [{name: 'large.sspack', size: 50 * 1024 * 1024 + 1}];
  const button = element();

  await h.context.preservePackArchives(button);

  assert.equal(calls.some(([pathName, method]) => pathName === '/content-library/import' && method === 'POST'), false);
  assert.equal(button.disabled, false);
  assert.match(h.node('archive-preserve-message').textContent, /larger than 50 MiB/);
});

test('an archive the bank refuses is reported and the control comes back', async () => {
  const h = harness(async pathName => {
    if (pathName === '/content-library') return {archives: []};
    if (pathName === '/content-library/import') throw new Error('Pack archive must be nonempty and at most 50 MiB.');
    return {};
  });
  const input = h.node('archive-preserve-files');
  input.files = [{name: 'broken.sspack', size: 10}];
  const button = element();

  await h.context.preservePackArchives(button);

  assert.equal(button.disabled, false);
  assert.match(h.node('archive-preserve-message').textContent, /Not preserved/);
  assert.match(h.node('archive-preserve-message').textContent, /nonempty and at most 50 MiB/);
});

test('the archive bank refreshes from the existing inventory without reading the shelf', async () => {
  const calls = [];
  const digest = 'c'.repeat(64);
  const h = harness(async pathName => {
    calls.push(pathName);
    return {archives: [
      {
        archive_sha256: digest,
        pack_id: 'com.fixture.pack',
        version: '1.2.3',
        bytes: 2048,
        title: '<img src=x onerror=alert(1)>',
        integrity: 'verified',
      },
      {archive_sha256: '../escape', title: 'bad row'},
    ]};
  });

  await h.context.loadPreservedArchives();

  const html = h.node('archive-bank-list').innerHTML;
  assert.deepEqual(calls, ['/content-library']);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /<img src=x onerror=alert/);
  assert.match(html, new RegExp(`data-archive-digest="${digest}"`));
  assert.match(html, /2\.0 KiB/);
  assert.match(html, /Verified/);
  assert.match(html, /Archive copy/);
  assert.doesNotMatch(html, /Not installed/);
  assert.doesNotMatch(html, /\.\.\/escape/);
});

test('an archive downloads by its exact digest through the authenticated blob door', async () => {
  const digest = 'd'.repeat(64);
  const blobCalls = [];
  const saved = [];
  const revoked = [];
  const h = harness(async () => ({}), {
    apiBlob: async pathName => {
      blobCalls.push(pathName);
      return {size: 1024};
    },
    downloadBlob: (url, filename) => saved.push([url, filename]),
    URL: {
      createObjectURL: () => 'blob:exact-archive',
      revokeObjectURL: url => revoked.push(url),
    },
    setTimeout: callback => callback(),
  });
  const button = element();
  button.textContent = 'Download .sspack';

  await h.context.downloadPreservedArchive(button, digest);

  assert.deepEqual(blobCalls, [`/content-library/${digest}/archive`]);
  assert.deepEqual(saved, [['blob:exact-archive', `${digest}.sspack`]]);
  assert.deepEqual(revoked, ['blob:exact-archive']);
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, 'Download .sspack');
  assert.match(h.node('archive-preserve-message').textContent, /Downloaded/);
});

test('a failed archive download is reported and the control comes back', async () => {
  const h = harness(async () => ({}), {
    apiBlob: async () => { throw new Error('HTTP 410'); },
  });
  const digest = 'e'.repeat(64);
  const button = element();
  button.textContent = 'Download .sspack';

  await h.context.downloadPreservedArchive(button, digest);

  assert.equal(button.disabled, false);
  assert.equal(button.textContent, 'Download .sspack');
  assert.match(h.node('archive-preserve-message').textContent, /Could not download/);
  assert.match(h.node('archive-preserve-message').textContent, /HTTP 410/);
});

test('a preserved archive stays reported when the bank list cannot refresh', async () => {
  const h = harness(async pathName => {
    if (pathName === '/content-library/import') {
      return {archive_sha256: 'a'.repeat(64), bytes: 1024, title: 'Portable Pack'};
    }
    throw new Error('HTTP 500');
  });
  const input = h.node('archive-preserve-files');
  input.files = [{name: 'portable.sspack', size: 1024}];
  const button = element();

  await h.context.preservePackArchives(button);

  const message = h.node('archive-preserve-message').textContent;
  assert.match(message, /1 archive preserved/);
  assert.match(message, /Cannot read preserved archives/);
  assert.match(message, /HTTP 500/);
  assert.equal(button.disabled, false);
});

test('a failed archive bank refresh is not drawn as an empty bank', async () => {
  const h = harness(async () => { throw new Error('HTTP 500'); });

  await h.context.loadPreservedArchives();

  assert.match(h.node('archive-bank-list').innerHTML, /unavailable/i);
  assert.doesNotMatch(h.node('archive-bank-list').innerHTML, /No preserved archives/);
  assert.match(h.node('archive-preserve-message').textContent, /Cannot read preserved archives/);
  assert.match(h.node('archive-preserve-message').textContent, /HTTP 500/);
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

test('shelved releases render Retire / Un-retire / Remove actions and retired status', async () => {
  const shelf = {
    ...READY_SHELF,
    releases: [
      {
        release_id: 'release.active',
        pack_id: 'com.fixture.active',
        version: '1.0.0',
        archive_sha256: 'a'.repeat(64),
        bytes: 1200,
        title: 'Active Pack',
        kind: 'topic-pack',
        current: true,
        available: true,
        retired: false,
        published_at: '2026-09-21T00:00:00Z',
      },
      {
        release_id: 'release.retired',
        pack_id: 'com.fixture.retired',
        version: '1.0.0',
        archive_sha256: 'b'.repeat(64),
        bytes: 1400,
        title: 'Retired Pack',
        kind: 'topic-pack',
        current: false,
        available: true,
        retired: true,
        retired_at: '2026-10-27T00:00:00Z',
        published_at: '2026-09-21T00:00:00Z',
      },
    ],
  };

  const calls = [];
  const confirmations = [];
  const h = harness(async (pathName, options) => {
    calls.push([pathName, options?.method || 'GET', options?.body ? JSON.parse(options.body) : null]);
    return shelf;
  }, {
    confirm: (message) => {
      confirmations.push(message);
      return true;
    },
  });

  await h.context.loadContentShelf();
  const html = h.node('shelf-releases').innerHTML;

  // Active pack has Retire button
  assert.match(html, /data-pack-action="retire"/);
  assert.match(html, /data-release-id="release\.active"/);
  assert.match(html, /Retire…<\/button>/);

  // Retired pack has Retired text and Un-retire + Remove buttons
  assert.match(html, /Retired — hidden from customers/);
  assert.match(html, /data-pack-action="unretire"/);
  assert.match(html, /data-release-id="release\.retired"/);
  assert.match(html, /data-pack-action="remove"/);
  assert.match(html, /Remove…<\/button>/);

  // Retire asks in a native dialog: the text, one checkbox (unticked), Retire / Cancel.
  await h.context.retirePack('release.active');
  assert.equal(calls.filter(([p]) => p === '/content-shelf/retire').length, 0,
    'opening the question must not post anything');
  assert.equal(confirmations.length, 0, 'the question is the dialog, not a confirm()');
  assert.equal(h.node('shelf-retire-dialog').open, true);
  assert.equal(h.node('shelf-retire-remove').checked, false, 'the removal option is unticked by default');
  assert.match(h.node('shelf-retire-text').textContent, /Retire Active Pack v1\.0\.0\?/);
  assert.match(h.node('shelf-retire-text').textContent, /This is the newest version/);
  assert.match(h.node('shelf-retire-text').textContent, /customers will stop seeing it in Studio Library/);

  // Cancel closes and does nothing at all.
  await h.context.cancelShelfRetire();
  assert.equal(h.node('shelf-retire-dialog').open, false);
  assert.equal(calls.filter(([p]) => p === '/content-shelf/retire').length, 0,
    'Cancel must never retire anything');

  // Retire with the box left unticked: a plain retire.
  await h.context.retirePack('release.active');
  assert.equal(h.node('shelf-retire-remove').checked, false, 'it is unticked again on every open');
  await h.context.confirmShelfRetire();
  assert.equal(h.node('shelf-retire-dialog').open, false);
  const retireCall = calls.find(([p]) => p === '/content-shelf/retire');
  assert.deepEqual(retireCall[2], {release_id: 'release.active', confirm_latest: true, remove_from_installs: false});

  // Test clicking unretire on retired pack
  await h.context.unretirePack('release.retired');
  const unretireCall = calls.find(([p]) => p === '/content-shelf/unretire');
  assert.deepEqual(unretireCall[2], {release_id: 'release.retired'});

  // Test clicking remove on retired pack
  await h.context.removePack('release.retired');
  assert.equal(confirmations.length, 1);
  assert.match(confirmations[0], /Remove Retired Pack v1\.0\.0\?/);
  assert.match(confirmations[0], /Its file moves to the Gateway trash/);
  const removeCall = calls.find(([p]) => p === '/content-shelf/remove');
  assert.deepEqual(removeCall[2], {release_id: 'release.retired', confirm: true});
});

test('ticking the box in the dialog is what asks Story apps to remove, and it never sticks', async () => {
  // The option is deliberate and off by default: only a tick IN THE DIALOG asks, and the box is
  // cleared every time the dialog opens so a second retire cannot inherit the first one's answer.
  const shelf = {
    ...READY_SHELF,
    releases: [{
      release_id: 'release.only', pack_id: 'com.fixture.only', version: '1.0.0',
      archive_sha256: 'c'.repeat(64), bytes: 900, title: 'Only Pack', kind: 'topic-pack',
      current: true, available: true, retired: false, published_at: '2026-09-21T00:00:00Z',
    }],
  };
  const calls = [];
  const h = harness(async (pathName, options) => {
    calls.push([pathName, options?.method || 'GET', options?.body ? JSON.parse(options.body) : null]);
    return shelf;
  });

  await h.context.loadContentShelf();

  // Unticked: retire only.
  await h.context.retirePack('release.only');
  await h.context.confirmShelfRetire();
  const plain = calls.find(([p]) => p === '/content-shelf/retire');
  assert.deepEqual(plain[2], {release_id: 'release.only', confirm_latest: true, remove_from_installs: false});

  // Ticked: retire and ask every Story app.
  calls.length = 0;
  await h.context.retirePack('release.only');
  h.node('shelf-retire-remove').checked = true;
  await h.context.confirmShelfRetire();
  const asked = calls.find(([p]) => p === '/content-shelf/retire');
  assert.deepEqual(asked[2], {release_id: 'release.only', confirm_latest: true, remove_from_installs: true});

  // Reopening starts from unticked, never from the previous answer.
  await h.context.retirePack('release.only');
  assert.equal(h.node('shelf-retire-remove').checked, false);
});

test('a WebKit without showModal still shows the same controls, and Cancel still does nothing', async () => {
  // The app's floor is macOS 11, where `<dialog>.showModal` may be missing. The question must still
  // be asked — an option silently dropped is worse than a non-modal one.
  const shelf = {
    ...READY_SHELF,
    releases: [{
      release_id: 'release.old', pack_id: 'com.fixture.old', version: '1.0.0',
      archive_sha256: 'd'.repeat(64), bytes: 900, title: 'Old WebKit Pack', kind: 'topic-pack',
      current: true, available: true, retired: false, published_at: '2026-09-21T00:00:00Z',
    }],
  };
  const calls = [];
  const h = harness(async (pathName, options) => {
    calls.push([pathName, options?.method || 'GET', options?.body ? JSON.parse(options.body) : null]);
    return shelf;
  });

  await h.context.loadContentShelf();
  delete h.node('shelf-retire-dialog').showModal;   // the older WebKit shape
  await h.context.retirePack('release.old');
  assert.equal(h.node('shelf-retire-dialog').attrs.open, '');
  assert.equal(calls.filter(([p]) => p === '/content-shelf/retire').length, 0);
  await h.context.cancelShelfRetire();
  assert.equal(h.node('shelf-retire-dialog').attrs.open, undefined);
  assert.equal(calls.filter(([p]) => p === '/content-shelf/retire').length, 0);
});

test('a retired pack can be asked for Story removal, and the request withdrawn later', async () => {
  const retired = {
    release_id: 'release.retired', pack_id: 'com.fixture.retired', version: '1.0.0',
    archive_sha256: 'd'.repeat(64), bytes: 1000, title: 'Retired Pack', kind: 'topic-pack',
    current: false, available: true, retired: true, retired_at: '2026-10-27T00:00:00Z',
    published_at: '2026-09-21T00:00:00Z', remove_from_installs: false,
  };
  const shelf = {...READY_SHELF, releases: [retired]};
  const calls = [];
  const h = harness(async (pathName, options) => {
    calls.push([pathName, options?.method || 'GET', options?.body ? JSON.parse(options.body) : null]);
    return shelf;
  });

  await h.context.loadContentShelf();
  const html = h.node('shelf-releases').innerHTML;
  assert.match(html, /data-pack-action="remove-from-installs"/);
  assert.match(html, /Also remove from Story apps<\/button>/);
  assert.doesNotMatch(html, /data-pack-action="keep-in-installs"/);

  await h.context.setPackRemoveFromInstalls('release.retired', true);
  const askCall = calls.find(([p]) => p === '/content-shelf/remove-from-installs');
  assert.deepEqual(askCall[2], {release_id: 'release.retired', enabled: true});

  // The flagged row offers the opposite action, and posting it withdraws the ask.
  const flagged = {...READY_SHELF, releases: [{...retired, remove_from_installs: true, remove_from_installs_at: 1793500000}]};
  const h2 = harness(async (pathName, options) => {
    calls.push([pathName, options?.method || 'GET', options?.body ? JSON.parse(options.body) : null]);
    return flagged;
  });
  await h2.context.loadContentShelf();
  const flaggedHtml = h2.node('shelf-releases').innerHTML;
  assert.match(flaggedHtml, /data-pack-action="keep-in-installs"/);
  assert.match(flaggedHtml, /Keep on Story apps<\/button>/);
  await h2.context.setPackRemoveFromInstalls('release.retired', false);
  const clearCall = calls.filter(([p]) => p === '/content-shelf/remove-from-installs').pop();
  assert.deepEqual(clearCall[2], {release_id: 'release.retired', enabled: false});
});

test('the shelf says what each Story Mac reported, and never draws silence as a removal', async () => {
  const base = {
    release_id: 'release.r', pack_id: 'com.fixture.r', version: '1.0.0',
    archive_sha256: 'e'.repeat(64), bytes: 1000, title: 'Asked Pack', kind: 'topic-pack',
    current: false, available: true, retired: true, retired_at: '2026-10-27T00:00:00Z',
    published_at: '2026-09-21T00:00:00Z', remove_from_installs: true, remove_from_installs_at: 1793500000,
  };
  const render = async (row) => {
    const h = harness(async () => ({...READY_SHELF, releases: [row]}));
    await h.context.loadContentShelf();
    return h.node('shelf-releases').innerHTML;
  };

  // No Mac has checked in: unknown, not removed.
  const none = await render({...base, remove_report: null});
  assert.match(none, /Removal requested .* no Story Mac has checked in yet/);

  // Macs have checked in, none has reported since the ask.
  const silent = await render({...base, remove_report: {
    pack_id: 'com.fixture.r', asked_at: 1793500000, total_instances: 2, reported_count: 0,
    removed_count: 0, removed: [], kept_in_use: [], failed: [], not_reported: ['mac-a', 'mac-b'],
  }});
  assert.match(silent, /not reported yet by any of 2 Story Macs/);
  assert.doesNotMatch(silent, /removed on/);

  // A mixed report: the owner's own sentence shape.
  const mixed = await render({...base, remove_report: {
    pack_id: 'com.fixture.r', asked_at: 1793500000, total_instances: 4, reported_count: 2,
    removed_count: 1,
    removed: [{machine: 'mac-a', outcome: 'removed', channels: []}],
    kept_in_use: [{machine: 'macmini-angkor', outcome: 'kept_in_use', channels: ['Night Falls']}],
    failed: [{machine: 'mac-c', outcome: 'failed', detail: 'the pack was in use'}],
    not_reported: ['mac-d'],
  }});
  assert.match(mixed, /removed on 1 of 4 Story Macs/);
  assert.match(mixed, /kept on macmini-angkor: used by channel Night Falls/);
  assert.match(mixed, /failed on mac-c: the pack was in use/);
  assert.match(mixed, /not reported yet: mac-d/);

  // A pack nobody asked to remove draws no report at all.
  const quiet = await render({...base, remove_from_installs: false, remove_from_installs_at: null});
  assert.doesNotMatch(quiet, /Removal requested/);
});
