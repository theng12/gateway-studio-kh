// The local video models were retired (2026-10-01), so a Studio Hub location publishes none and
// the Video Create page can have no model at all -- or only a cloud one. Both must render cleanly:
// no picker with zero options, a plain message instead, and a cloud model still selectable.
// Drives the page's own functions in a VM with a hand-built DOM: no app, no gateway, no network.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
const START = source.indexOf('const CREATE_TABS = [');
const END = source.indexOf('function cfValue(');
if (START < 0 || END < 0) throw new Error('Create section boundary changed in admin_ui.html');

function videoForm(videoRows) {
  const form = { innerHTML: '' };
  const context = vm.createContext({
    document: { getElementById: id => (id === 'create-form' ? form : { innerHTML: '' }) },
    esc: s => String(s ?? '').replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    api: async () => ({}),
  });
  vm.runInContext(source.slice(START, END), context);
  vm.runInContext(
    `createTab = 'video'; createCatalogue = { by_modality: { video: ${JSON.stringify(videoRows)} } };
     renderCreateForm();`, context);
  return form.innerHTML;
}

test('no video model at all: a plain message, no empty picker', () => {
  const html = videoForm([]);
  assert.ok(html.includes('No model is switched on for this lane'), html);
  assert.ok(!html.includes('id="cf-model"'), 'an empty <select> must not be rendered');
  assert.ok(html.includes('id="cf-prompt"'), 'the prompt box still renders');
});

test('a cloud video model alone still fills the picker', () => {
  const html = videoForm([{ id: 'fal::fal-ai/veo3.1', provider_name: 'fal', display_name: 'Veo 3.1' }]);
  assert.ok(html.includes('id="cf-model"'), html);
  assert.ok(html.includes('fal — Veo 3.1'), html);
  assert.ok(!html.includes('No model is switched on'), html);
});
