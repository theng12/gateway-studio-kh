// The Footage library card: one row per clip, measured states, and a folder **picker**.
//
// The owner's rule (0.2.257): no typed paths anywhere. The root is chosen by browsing
// directories the Gateway offers, so this pins that no text field takes a path and that
// the page draws what the library actually measured.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');

function section(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + 1);
  if (first < 0 || last < 0) throw new Error(`admin_ui.html moved: ${start}`);
  return source.slice(first, last);
}

function element(extra = {}) {
  return { value: '', innerHTML: '', textContent: '', className: '', style: {}, dataset: {},
    disabled: false, ...extra };
}

function harness(nodes, answers) {
  const map = new Map(Object.entries(nodes));
  const calls = [];
  const context = vm.createContext({
    document: { getElementById: id => map.get(id) || null,
      querySelectorAll: () => [] },
    esc: value => String(value ?? '').replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    api: async (pathName) => { calls.push(pathName); return answers[pathName] ?? {}; },
    console,
  });
  vm.runInContext(section('/* ------------------------------ footage library', '/* ---------------------------------- models'),
    context);
  return { context, calls };
}

const CLIP = {
  sha256: 'a'.repeat(64), title: 'City timelapse', duration_seconds: 12, width: 1920,
  height: 1080, fps: 25, filesize_bytes: 2048, licence: 'cc0', source: 'wikimedia',
  ai_generated: false,
};

test('one row per clip, with measured length, size, licence and source', () => {
  const nodes = {
    'footage-quota': element(),
    'footage-root': element(),
    'footage-rows': element(),
    'footage-sources': element(),
    'footage-msg': element(),
  };
  const { context } = harness(nodes);
  vm.runInContext(`renderFootageRows(${JSON.stringify([CLIP])});`, context);
  const html = nodes['footage-rows'].innerHTML;
  assert.match(html, /City timelapse/);
  assert.match(html, /0:00:12/, 'the length is a clock, not a raw number');
  assert.match(html, /2 KB/);
  assert.match(html, /cc0/);
  assert.match(html, /wikimedia/);
  assert.match(html, new RegExp(`/admin/api/footage/file/${'a'.repeat(64)}`),
    'the console plays the clip through its own authenticated route');
});

test('an unmeasured clip says so, never zero', () => {
  const nodes = { 'footage-rows': element() };
  const { context } = harness(nodes);
  vm.runInContext(`renderFootageRows(${JSON.stringify([{ sha256: 'b'.repeat(64), title: 'Raw' }])});`, context);
  const html = nodes['footage-rows'].innerHTML;
  assert.match(html, /length not recorded/);
  assert.match(html, /picture size not recorded/);
  assert.doesNotMatch(html, /0 B/, 'a missing byte count is not a measured zero');
});

test('the folder is chosen with a picker: no text field takes a path', () => {
  const pack = section('<section id="view-packs"', '<section id="view-guests"');
  assert.match(source, /id="footage-root-dialog"/);
  assert.match(source, /onclick="openFootageRoot\(\)"/);
  assert.doesNotMatch(pack, /<input[^>]+id="footage-root[^"]*"[^>]*type="text"/,
    'the root is never a typed path');
  assert.match(source, /async function browseFootage\(/, 'the picker lists directories');
  assert.match(source, /\/footage\/browse/, 'and reads them from the Gateway');
});

test('a source shows its cost before a fetch, and a keyed one asks for the key', () => {
  const nodes = { 'footage-sources': element() };
  const { context } = harness(nodes);
  const sources = [
    { id: 'wikimedia', label: 'Wikimedia Commons', free: true, enabled: true,
      configured: true, needs_key: false, key_field: 'api_key', cost: 'Free · check the file licence' },
    { id: 'pexels', label: 'Pexels', free: true, enabled: false, configured: false,
      needs_key: true, key_field: 'api_key', key_label: 'API key', cost: 'Free with a key' },
  ];
  vm.runInContext(`renderFootageSources(${JSON.stringify(sources)});`, context);
  const html = nodes['footage-sources'].innerHTML;
  assert.match(html, /check the file licence/, 'the cost line is shown before anything is fetched');
  assert.match(html, /key needed/);
  assert.match(html, /Save key/);
  assert.match(html, /Turn on/);
});

test('the library overview fills quota and the chosen folder', async () => {
  const nodes = {
    'footage-quota': element(), 'footage-root': element(), 'footage-rows': element(),
    'footage-sources': element(), 'footage-msg': element(),
  };
  const { context, calls } = harness(nodes, {
    '/footage': { clips: [CLIP], quota: { files: 1, bytes: 2048, free_bytes: 1048576 },
      sources: [], root: '/Volumes/NAS/footage', using_default: false, default_root: '/tmp/x' },
  });
  await context.loadFootage();
  assert.deepEqual(calls, ['/footage']);
  assert.match(nodes['footage-quota'].textContent, /1 clips · 2 KB/);
  assert.match(nodes['footage-root'].textContent, /\/Volumes\/NAS\/footage/);
  assert.match(nodes['footage-rows'].innerHTML, /City timelapse/);
});
