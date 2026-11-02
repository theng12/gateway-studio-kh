const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');

function workspace(row) {
  const nodes = new Map();
  for (const id of ['cf-model', 'cf-lyrics', 'cf-lyrics-box', 'cf-vocal_language', 'cf-vocal_language-box', 'cf-msg', 'create-form']) {
    nodes.set(id, { value: '', style: {}, innerHTML: '', textContent: '' });
  }
  nodes.get('cf-model').value = row.id;
  const calls = [];
  const context = vm.createContext({
    document: { getElementById: id => nodes.get(id) },
    esc: value => String(value ?? ''),
    crypto: { randomUUID: () => 'music-request' },
    api: async (url, options) => { calls.push({ url, body: JSON.parse(options.body) }); return { id: 'music-job' }; },
    renderCreateResult() {}, pollCreate() {},
  });
  vm.runInContext(source.slice(source.indexOf('const CREATE_TABS = ['), source.indexOf('function pollCreate(id)')), context);
  context.row = row;
  vm.runInContext("createTab = 'music'; createCatalogue = { by_modality: { music: [row] } };", context);
  return { nodes, calls, context };
}

test('vocal model offers lyrics and language; changing to instrumental clears both', () => {
  const { context, nodes } = workspace({ id: 'studio-hub::song', supports_vocals: true, requires_lyrics: true });
  vm.runInContext('renderCreateForm()', context);
  const html = nodes.get('create-form').innerHTML;
  assert.match(html, /id="cf-lyrics"/);
  assert.match(html, /id="cf-vocal_language"/);
  assert.equal(nodes.get('cf-lyrics-box').style.display, '');
  assert.equal(nodes.get('cf-lyrics').required, true);
  nodes.get('cf-lyrics').value = 'A song';
  nodes.get('cf-vocal_language').value = 'km';
  vm.runInContext("createCatalogue.by_modality.music[0] = { id: 'studio-hub::song', supports_vocals: false }; applyCreateModelControls();", context);
  assert.equal(nodes.get('cf-lyrics-box').style.display, 'none');
  assert.equal(nodes.get('cf-lyrics').value, '');
  assert.equal(nodes.get('cf-vocal_language').value, '');
});

test('required lyrics fail locally; supplied lyrics and vocal language reach Create unchanged', async () => {
  const { context, nodes, calls } = workspace({ id: 'studio-hub::song', requires_lyrics: true });
  const button = { disabled: false, textContent: 'Generate' };
  await context.submitCreate(button);
  assert.match(nodes.get('cf-msg').textContent, /needs lyrics/);
  assert.equal(calls.length, 0);
  nodes.get('cf-lyrics').value = '[Verse]\nពន្លឺថ្ងៃ\n[Chorus]\nពន្លឺ';
  nodes.get('cf-vocal_language').value = 'km';
  await context.submitCreate(button);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.params.lyrics, nodes.get('cf-lyrics').value);
  assert.equal(calls[0].body.params.vocal_language, 'km');
});
