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
// Every question in the page is now an in-app dialog; the tests' own confirm/alert/prompt
// stubs are where each answer comes from.
vm.runInContext(`globalThis.uiConfirm = async (t) => (typeof confirm === 'function' ? confirm(t) : true);
globalThis.uiAlert = async (t) => { if (typeof alert === 'function') alert(t); };
globalThis.uiPrompt = async (t, v) => (typeof prompt === 'function' ? prompt(t, v) : v);`, context);

  vm.runInContext(source.slice(source.indexOf('const CREATE_TABS = ['), source.indexOf('function pollCreate(id)')), context);
  context.row = row;
  vm.runInContext("createTab = 'music'; createCatalogue = { by_modality: { music: [row] } };", context);
  return { nodes, calls, context };
}

test('lyrics remain required while undeclared language options stay hidden', () => {
  const { context, nodes } = workspace({ id: 'studio-hub::song', supports_vocals: true, requires_lyrics: true });
  vm.runInContext('renderCreateForm()', context);
  const html = nodes.get('create-form').innerHTML;
  assert.match(html, /id="cf-lyrics"/);
  assert.equal(nodes.get('cf-lyrics-box').style.display, '');
  assert.equal(nodes.get('cf-lyrics').required, true);
  assert.equal(nodes.get('cf-vocal_language-box').style.display, 'none');
  assert.equal(nodes.get('cf-vocal_language').disabled, true);
  nodes.get('cf-lyrics').value = 'A song';
  vm.runInContext("createCatalogue.by_modality.music[0] = { id: 'studio-hub::song', supports_vocals: false }; applyCreateModelControls();", context);
  assert.equal(nodes.get('cf-lyrics-box').style.display, 'none');
  assert.equal(nodes.get('cf-lyrics').value, '');
  assert.equal(nodes.get('cf-vocal_language').value, '');
});

test('required lyrics fail locally; lyrics submit without an undeclared language', async () => {
  const { context, nodes, calls } = workspace({ id: 'studio-hub::song', requires_lyrics: true });
  const button = { disabled: false, textContent: 'Generate' };
  await context.submitCreate(button);
  assert.match(nodes.get('cf-msg').textContent, /needs lyrics/);
  assert.equal(calls.length, 0);
  nodes.get('cf-lyrics').value = '[Verse]\nពន្លឺថ្ងៃ\n[Chorus]\nពន្លឺ';
  // Typing fires the page's gate; a direct value assignment must do the same for the button
  // to be pressable again after the first refusal disabled it.
  vm.runInContext('updateCreateGate()', context);
  await context.submitCreate(button);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.params.lyrics, nodes.get('cf-lyrics').value);
  assert.equal(Object.hasOwn(calls[0].body.params, 'vocal_language'), false);
});

test('published language options control the list, default and outgoing value', async () => {
  const { context, nodes, calls } = workspace({ id: 'studio-hub::song', requires_lyrics: true,
    controls: [{ name: 'vocal_language', enum: ['fr', 'en', 'de'], default: 'en' }] });
  vm.runInContext('renderCreateForm()', context);
  const field = nodes.get('cf-vocal_language');
  assert.equal(nodes.get('cf-vocal_language-box').style.display, '');
  assert.equal(field.value, 'en');
  assert.match(field.innerHTML, /value="fr"/);
  assert.doesNotMatch(field.innerHTML, /value="zh"/);
  nodes.get('cf-lyrics').value = '[Verse]\nHello';
  field.value = 'de';
  await context.submitCreate({ disabled: false, textContent: 'Generate' });
  assert.equal(calls[0].body.params.vocal_language, 'de');
  vm.runInContext("createCatalogue.by_modality.music[0] = { id: 'studio-hub::song', supports_vocals: false, controls: [{ name: 'vocal_language', enum: ['en'] }] }; applyCreateModelControls();", context);
  assert.equal(nodes.get('cf-vocal_language-box').style.display, 'none');
  assert.equal(field.value, '');
  assert.equal(field.disabled, true);
});
