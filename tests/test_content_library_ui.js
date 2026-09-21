const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
const readSource = source.slice(source.indexOf('async function loadPacks()'), source.indexOf('async function importPack('));
// The kind grouping the packs renderer uses lives in the shelf helpers, outside the
// loadPacks..importPack slice, so it is run in the same script scope.
const helperSource = source.slice(source.indexOf('const SHELF_KIND_LABELS = {'), source.indexOf('function renderShelfStatus('));
const importSource = source.slice(source.indexOf('async function importPack('), source.indexOf('async function savePack('));

function harness({ archives = [], importer } = {}) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, { value: '', textContent: '', innerHTML: '', dataset: {}, className: '', disabled: false });
    return nodes.get(id);
  };
  const calls = [];
  const context = vm.createContext({
    document: { getElementById: node },
    esc: value => String(value ?? ''),
    api: async (route, options) => {
      calls.push({ route, options });
      if (route === '/content-library') {
        if (importer) return importer(route, options);
        return { archives };
      }
      return importer ? importer(route, options) : { pack_id: 'x', version: '1.0.0' };
    },
    shelfState: { archives: [] },
    shelfReady: () => false,
    shelfAcknowledged: () => false,
    updateShelfControls: () => {},
    loadContentShelf: async () => {},
  });
  vm.runInContext(`${helperSource}\n${readSource}\n${importSource}`, context);
  return { context, nodes, node, calls };
}

test('library retry clears its old read error and blocks unavailable downloads', async () => {
  let failing = true;
  const { context, node } = harness({
    importer: async route => {
      if (route !== '/content-library') return {};
      if (failing) throw Error('offline');
      return { archives: [{ pack_id: 'test.fixture', version: '1.0.0', archive_sha256: 'a'.repeat(64), integrity: 'unavailable' }] };
    },
  });
  const message = node('pack-message');
  const library = node('pack-library');
  await context.loadPacks();
  assert.match(message.textContent, /Cannot read library/);
  failing = false;
  await context.loadPacks();
  assert.equal(message.textContent, '');
  assert.match(library.innerHTML, /disabled.*onclick="savePack/);
  assert.match(library.innerHTML, /Ownership: unverified/);
  message.textContent = 'Preserved test.fixture; not installed.';
  await context.loadPacks();
  assert.match(message.textContent, /Preserved/);
});

test('the archive list is searchable, sortable and filterable', async () => {
  const { context, node } = harness({
    archives: [
      { pack_id: 'p.zeta', title: 'Zeta Style', version: '0.9.0', item_kinds: ['visual-style'], tags: ['painterly'], integrity: 'verified', archive_sha256: 'b'.repeat(64) },
      { pack_id: 'p.alpha', title: 'Alpha Topic', version: '1.10.0', item_kinds: ['topic-pack'], tags: ['history'], integrity: 'unavailable', archive_sha256: 'c'.repeat(64) },
    ],
  });
  await context.loadPacks();
  const library = node('pack-library');
  assert.match(library.innerHTML, /Alpha Topic/);
  assert.match(library.innerHTML, /Zeta Style/);
  assert.match(node('pack-count').textContent, /2 of 2/);

  // Sort by version, newest first: 1.10.0 must beat 0.9.0 (numeric, not lexical).
  node('pack-sort').value = 'version';
  context.renderPacks();
  assert.ok(library.innerHTML.indexOf('Alpha Topic') < library.innerHTML.indexOf('Zeta Style'),
    'numeric version sort put 1.10.0 after 0.9.0');

  // A tag search finds a row that the name would not.
  node('pack-search').value = 'painterly';
  context.renderPacks();
  assert.match(library.innerHTML, /Zeta Style/);
  assert.doesNotMatch(library.innerHTML, /Alpha Topic/);
  assert.match(node('pack-count').textContent, /1 of 2/);

  // Filter by integrity, and an empty result says so instead of looking broken.
  node('pack-search').value = '';
  node('pack-filter').value = 'verified';
  context.renderPacks();
  assert.match(library.innerHTML, /Zeta Style/);
  assert.doesNotMatch(library.innerHTML, /Alpha Topic/);
  node('pack-filter').value = 'unavailable';
  node('pack-search').value = 'nothing matches this';
  context.renderPacks();
  assert.match(library.innerHTML, /No preserved archive matches/);
});

test('one click imports many archives, keeps going past a failure, and reports it', async () => {
  const { context, node, calls } = harness({
    importer: async (route, options) => {
      if (route !== '/content-library/import') return { archives: [] };
      if (options.body.name === 'broken.sspack') throw Error('not a content pack');
      return { pack_id: `p.${options.body.name}`, version: '1.0.0' };
    },
  });
  const files = [
    { name: 'one.sspack', size: 1024 },
    { name: 'broken.sspack', size: 1024 },
    { name: 'three.sspack', size: 1024 },
    { name: 'notes.txt', size: 10 },
  ];
  node('pack-files').files = files;
  const button = node('pack-import');

  await context.importPack(button);

  const imports = calls.filter(call => call.route === '/content-library/import');
  assert.equal(imports.length, 3, 'the .txt file must be skipped and the batch must not stop at the failure');
  assert.equal(button.disabled, false, 'the button must come back');
  assert.deepEqual(node('pack-files').value, '', 'the picker is cleared so the same files cannot be re-imported by accident');
  assert.match(node('pack-message').textContent, /Preserved 2 of 3/);
  assert.match(node('pack-message').textContent, /broken\.sspack/);
});

test('importing nothing chosen explains what to do', async () => {
  const { context, node, calls } = harness();
  await context.importPack(node('pack-import'));
  assert.equal(calls.filter(call => call.route === '/content-library/import').length, 0);
  assert.match(node('pack-message').textContent, /Choose one or more/);
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
