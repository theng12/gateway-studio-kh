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
  const start = source.indexOf('const CONTENT_PACK_AUTHORING_BRIEF =');
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
  return {context, node, state: () => vm.runInContext('shelfState', context), authorState: () => vm.runInContext('authorState', context)};
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

test('a failed shelf refresh clears the last ready state before publication controls update', async () => {
  let fail = false;
  let publishCalls = 0;
  const h = harness(async (pathName) => {
    if (pathName === '/content-shelf' && fail) throw Error('shelf status unavailable');
    if (pathName === '/content-shelf/publish') { publishCalls += 1; return {}; }
    return READY_SHELF;
  });

  await h.context.loadContentShelf();
  assert.equal(h.state().data.configured, true);
  fail = true;
  await h.context.loadContentShelf();

  assert.equal(h.state().data, null);
  assert.match(h.node('shelf-message').textContent, /status unavailable/);
  const button = element();
  h.node('shelf-authorship-ack').checked = true;
  await h.context.publishPreservedArchive('d'.repeat(64), button);
  assert.equal(publishCalls, 0);
  assert.match(h.node('shelf-message').textContent, /Set up an available private shelf/);
});

test('a refused draft publication refreshes a sealed candidate without overwriting open editor text', async () => {
  const draft = {
    id: 'b'.repeat(64),
    pack_id: 'com.fixture.pack',
    version: '1.0.0',
    revision: 3,
    document_text: '{"name":"Saved"}',
    sealed: false,
  };
  let currentDraft = draft;
  let rejectPublish;
  const h = harness(async (pathName, options) => {
    if (pathName === '/content-shelf') return READY_SHELF;
    if (pathName.endsWith('/publish')) return new Promise((_, reject) => { rejectPublish = reject; });
    if (pathName === '/pack-authoring') return {available:true, reservations:[]};
    if (pathName === '/pack-authoring/drafts') return {drafts: [currentDraft]};
    return currentDraft;
  });
  await h.context.loadContentShelf();
  h.node('shelf-authorship-ack').checked = true;
  h.context.updateShelfControls();
  h.authorState().draft = {...draft};
  h.authorState().savedText = draft.document_text;
  h.node('author-document').value = draft.document_text;
  const button = element();

  const pending = h.context.publishAuthorDraft(draft.id, draft.revision, button);
  await Promise.resolve();
  h.node('author-document').value = '{"name":"Unsaved while shelving"}';
  h.context.authorEdited();
  currentDraft = {...draft, sealed:true};
  rejectPublish(Error('Registry receipt is not eligible for this shelf.'));
  await pending;

  assert.equal(button.disabled, false);
  assert.equal(h.authorState().draft.id, draft.id);
  assert.equal(h.authorState().draft.sealed, true);
  assert.equal(h.node('author-document').value, '{"name":"Unsaved while shelving"}');
  assert.equal(h.node('author-document').readOnly, true);
  assert.match(h.node('author-message').textContent, /immutable export candidate/i);
  assert.match(h.node('author-message').textContent, /sealed/i);
  assert.doesNotMatch(h.node('author-message').textContent, /remains intact and editable/i);
});

test('repeated archive shelving clicks share one pending request and restore the control', async () => {
  let finish;
  let publishCalls = 0;
  const digest = 'c'.repeat(64);
  const archive = {pack_id: 'com.fixture.pack', version: '1.0.0', archive_sha256: digest, integrity: 'verified', item_kinds: ['topic-pack']};
  const h = harness(async (pathName, options) => {
    if (pathName === '/content-shelf') return READY_SHELF;
    if (pathName === '/content-library') return {archives: [archive]};
    if (pathName === '/content-shelf/publish') {
      publishCalls += 1;
      return new Promise(resolve => { finish = resolve; });
    }
    return {};
  });
  await h.context.loadContentShelf();
  await h.context.loadPacks();
  h.node('shelf-authorship-ack').checked = true;
  h.context.updateShelfControls();
  const button = element();

  const pending = h.context.publishPreservedArchive(digest, button);
  await Promise.resolve();
  const repeated = await h.context.publishPreservedArchive(digest, button);
  assert.equal(repeated, undefined);
  assert.equal(publishCalls, 1);
  assert.equal(button.disabled, true);
  assert.match(button.textContent, /Shelving/);

  finish({release: READY_SHELF.releases[0] || {}, replayed: false});
  await pending;
  assert.equal(button.disabled, false);
  assert.match(h.node('shelf-message').textContent, /shelf/i);
});

test('copying the authoring brief produces a credential-free local handoff', async () => {
  let copied = '';
  const h = harness(async () => READY_SHELF, {
    navigator: {clipboard: {writeText: async value => { copied = value; }}},
  });

  await h.context.copyAuthoringBrief();
  assert.match(copied, /local private destination/i);
  assert.match(copied, /standalone .*content pack skill/i);
  assert.match(copied, /series formula/i);
  assert.match(copied, /visual style/i);
  assert.match(copied, /thumbnail recipe/i);
  assert.doesNotMatch(copied, /Bearer\s|sk-[A-Za-z0-9]|-----BEGIN|token:|password:/i);
  assert.match(h.node('brief-message').textContent, /copied/i);
});
