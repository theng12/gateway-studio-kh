// The Create page draws each model's OWN options — ratios, sizes, video variants,
// instrumental, and the one required-field sentence — and nothing it was not told.
//
// The owner's rule (2026-11-13 completeness gates): every capability a model declares has a
// control, a control the model does not declare is absent, and an unsupported pair cannot be
// chosen. These drive the page's own functions in a VM with a hand-built DOM.

const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
const START = source.indexOf('const CREATE_TABS = [');
const END = source.indexOf('async function renderCreateRecent()');
if (START < 0 || END < 0) throw new Error('Create section boundary changed in admin_ui.html');
const SECTION = source.slice(START, END);

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function element(extra = {}) {
  return { value: '', innerHTML: '', textContent: '', className: '', style: {}, dataset: {},
    disabled: false, focus() {}, ...extra };
}

function harness(nodes = {}) {
  const map = new Map(Object.entries(nodes));
  const context = vm.createContext({
    document: { getElementById: id => map.get(id) || null },
    esc,
    api: async () => ({ voices: [] }),
    priceInfo: () => ({ text: 'Not published' }),
    console,
  });
  vm.runInContext(`globalThis.uiConfirm = async () => true;
globalThis.uiAlert = async () => {};
globalThis.uiPrompt = async (t, v) => v;`, context);
  vm.runInContext(SECTION, context);
  return { context, map };
}

const IMAGE_ROW = {
  id: 'studio-hub::flux', display_name: 'Flux', provider_name: 'Hub Studio KH (local)',
  modality: 'image', free_lane: true, default_aspect_ratio: '16:9',
  output_options: [
    { resolution: '1K', aspect_ratio: '16:9', width: 1280, height: 720, is_default: true, credits_per_image: 0 },
    { resolution: '1K', aspect_ratio: '16:9', width: 1024, height: 576, is_default: false, credits_per_image: 0 },
    { resolution: '1K', aspect_ratio: '1:1', width: 1024, height: 1024, is_default: false, credits_per_image: 0 },
  ],
};

test('the image form draws only the ratios and sizes the model published', () => {
  const nodes = {
    'cf-model': element({ value: 'studio-hub::flux' }),
    'cf-image-options': element({ style: { display: 'none' } }),
    'cf-aspect_ratio': element(),
    'cf-resolution': element(),
    'cf-size-price': element(),
  };
  const { context } = harness(nodes);
  vm.runInContext(`createTab = 'image'; createCatalogue = ${JSON.stringify({ by_modality: { image: [IMAGE_ROW] } })};`, context);

  vm.runInContext('syncCreateImageOptions();', context);
  assert.equal(nodes['cf-aspect_ratio'].innerHTML,
    '<option value="16:9">16:9 · recommended</option><option value="1:1">1:1</option>',
    'one option per published ratio, the model\'s default named as recommended');
  assert.match(nodes['cf-resolution'].innerHTML, /1K · 1280×720/);
  assert.match(nodes['cf-resolution'].innerHTML, /1K · 1024×576/);
  assert.doesNotMatch(nodes['cf-resolution'].innerHTML, /4:3/, 'an unpublished ratio is not offered');
  assert.equal(nodes['cf-image-options'].style.display, '');

  const params = JSON.parse(JSON.stringify(vm.runInContext('createImageParams()', context)));
  assert.deepEqual(params, { aspect_ratio: '16:9', width: 1280, height: 720, resolution: '1K' },
    'the default option travels as the request');
  assert.match(nodes['cf-size-price'].textContent, /Free \(your Macs\)/,
    'the size shows its live price — a free lane says so');

  // Changing the ratio re-draws that ratio's sizes and keeps the request in step.
  nodes['cf-aspect_ratio'].value = '1:1';
  vm.runInContext('syncCreateImageSizes();', context);
  assert.equal(nodes['cf-resolution'].innerHTML, '<option value="0">1K · 1024×1024</option>');
  assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext('createImageParams()', context))),
    { aspect_ratio: '1:1', width: 1024, height: 1024, resolution: '1K' });
});

test('a model with no published options draws no ratio control', () => {
  const nodes = {
    'cf-model': element({ value: 'cloud::model' }),
    'cf-image-options': element({ style: { display: '' } }),
    'cf-aspect_ratio': element(),
    'cf-resolution': element(),
    'cf-size-price': element(),
  };
  const { context } = harness(nodes);
  vm.runInContext(`createTab = 'image'; createCatalogue = ${JSON.stringify({ by_modality: { image: [{
    id: 'cloud::model', display_name: 'Cloud', modality: 'image', free_lane: false,
  }] } })};`, context);
  vm.runInContext('syncCreateImageOptions();', context);
  assert.equal(nodes['cf-image-options'].style.display, 'none',
    'nothing is invented for a model that states no geometry');
  assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext('createImageParams()', context))), {});
});

test('the video form offers the model\'s published variants only', () => {
  const row = {
    id: 'studio-hub::ltx', display_name: 'LTX', provider_name: 'Hub Studio KH (local)',
    modality: 'video', free_lane: true,
    variants: [
      { operation: 'text-to-video', duration_seconds: 5, resolution: '720p', aspect_ratio: '16:9' },
      { operation: 'image-to-video', duration_seconds: 10, resolution: '720p', aspect_ratio: '16:9' },
    ],
  };
  const nodes = {
    'cf-model': element({ value: 'studio-hub::ltx' }),
    'cf-video-options': element({ style: { display: 'none' } }),
    'cf-video-variant': element(),
  };
  const { context } = harness(nodes);
  vm.runInContext(`createTab = 'video'; createCatalogue = ${JSON.stringify({ by_modality: { video: [row] } })};`, context);
  vm.runInContext('syncCreateVideoOptions();', context);
  assert.equal(nodes['cf-video-options'].style.display, '');
  assert.match(nodes['cf-video-variant'].innerHTML, /5s · 720p · 16:9 · text to video/);
  assert.match(nodes['cf-video-variant'].innerHTML, /10s · 720p · 16:9 · image to video/);
  assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext('createVideoParams()', context))),
    { duration_seconds: 5, resolution: '720p', aspect_ratio: '16:9' });
});

test('instrumental appears only where the model supports it, and drops the lyrics', () => {
  const nodes = {
    'cf-model': element({ value: 'studio-hub::yue' }),
    'cf-instrumental-box': element({ style: { display: 'none' } }),
    'cf-instrumental': element({ checked: true }),
    'cf-lyrics': element({ value: 'a line' }),
    'cf-lyrics-box': element({ style: { display: '' } }),
    'cf-lyrics-count': element(),
    'cf-gate': element(),
    'cf-go': element(),
  };
  const { context } = harness(nodes);
  vm.runInContext(`createTab = 'music'; createCatalogue = ${JSON.stringify({ by_modality: { music: [{
    id: 'studio-hub::yue', display_name: 'YuE', modality: 'music',
    supports_instrumental: true, requires_lyrics: true, supports_vocals: true,
  }] } })};`, context);
  vm.runInContext('updateCreateGate();', context);
  assert.equal(nodes['cf-instrumental-box'].style.display, '', 'the toggle is offered');
  assert.equal(nodes['cf-lyrics-box'].style.display, 'none', 'instrumental means no lyrics field');
  assert.equal(nodes['cf-gate'].textContent, '', 'and no missing-lyrics refusal');
  assert.equal(nodes['cf-go'].disabled, false);

  // The lyrics field is cleared by the page when the box hides; simulate the same state.
  nodes['cf-lyrics'].value = '';
  vm.runInContext('createCatalogue.by_modality.music[0].supports_instrumental = false; updateCreateGate();', context);
  assert.equal(nodes['cf-instrumental-box'].style.display, 'none',
    'a model that does not declare instrumental never shows the toggle');
  assert.equal(nodes['cf-instrumental'].checked, false, 'and the choice is cleared');
  assert.equal(nodes['cf-lyrics-box'].style.display, '');
  assert.match(nodes['cf-gate'].textContent, /needs lyrics/);
  assert.equal(nodes['cf-go'].disabled, true, 'Generate is disabled with the reason');
});

test('a missing voice disables Generate and names what is missing', () => {
  const nodes = {
    'cf-model': element({ value: 'studio-hub::tts' }),
    'cf-voice_id': element({ value: '' }),
    'cf-voice_id_manual': element({ value: '' }),
    'cf-gate': element(),
    'cf-go': element(),
  };
  const { context } = harness(nodes);
  vm.runInContext(`createTab = 'audio'; createCatalogue = ${JSON.stringify({ by_modality: { tts: [{
    id: 'studio-hub::tts', display_name: 'TTS', modality: 'tts',
  }] } })};`, context);
  vm.runInContext('updateCreateGate();', context);
  assert.equal(nodes['cf-go'].disabled, true);
  assert.match(nodes['cf-gate'].textContent, /Choose a saved voice, or a provider voice ID/);

  nodes['cf-voice_id'].value = 'studio-hub-local::voice-1';
  vm.runInContext('updateCreateGate();', context);
  assert.equal(nodes['cf-go'].disabled, false);
  assert.equal(nodes['cf-gate'].textContent, '');
});

test('a declared temperature control appears with the model\'s own limits', () => {
  const nodes = {
    'cf-model': element({ value: 'cloud::tts' }),
    'cf-temperature-box': element({ style: { display: 'none' } }),
    'cf-temperature': element(),
    'cf-temperature-help': element(),
    'cf-delivery_instruction-box': element({ style: { display: 'none' } }),
    'cf-delivery_instruction': element(),
  };
  const { context } = harness(nodes);
  vm.runInContext(`createTab = 'audio'; createCatalogue = ${JSON.stringify({ by_modality: { tts: [{
    id: 'cloud::tts', display_name: 'TTS', modality: 'tts',
    controls: [{ name: 'temperature', type: 'number', minimum: 0.1, maximum: 2,
                 default: 0.7, step: 0.05 }],
  }] } })};`, context);
  vm.runInContext('applyCreateModelControls();', context);
  assert.equal(nodes['cf-temperature-box'].style.display, '',
    'a declared capability has a control');
  assert.equal(nodes['cf-temperature'].min, 0.1);
  assert.equal(nodes['cf-temperature'].max, 2);
  assert.match(nodes['cf-temperature-help'].textContent, /0\.1–2/);
  assert.match(nodes['cf-temperature-help'].textContent, /default 0\.7/);

  vm.runInContext('createCatalogue.by_modality.tts[0].controls = []; applyCreateModelControls();', context);
  assert.equal(nodes['cf-temperature-box'].style.display, 'none',
    'a model that declares none keeps no temperature field');
});

test('the subtitle form refuses to generate without a recording', () => {
  const nodes = {
    'cf-model': element({ value: 'cloud::whisper' }),
    'cf-file': element({ files: [] }),
    'cf-gate': element(),
    'cf-go': element(),
  };
  const { context } = harness(nodes);
  vm.runInContext("createTab = 'subtitle'; updateCreateGate();", context);
  assert.equal(nodes['cf-go'].disabled, true);
  assert.match(nodes['cf-gate'].textContent, /Choose a recording first/);
  nodes['cf-file'].files = [{}];
  vm.runInContext('updateCreateGate();', context);
  assert.equal(nodes['cf-go'].disabled, false);
});

test('a model switched on in Models reaches Create, and no model disables Generate', () => {
  // The Create catalogue was cached for the page's life, so a model ticked in Models left
  // "No model is switched on" on screen (0.2.257 critic round 4).
  assert.match(SECTION, /createCatalogue = await api\('\/create\/models'\)/);
  assert.match(SECTION, /createCatalogue = null;/);
  assert.match(SECTION, /No model is ready for this lane/);
  assert.match(SECTION, /wireTabKeys\('create-tabs'\)/, 'arrow keys move within the Create tabs');
  assert.match(source, /wireTabKeys\('m-mode-chips'\)/, 'and within the Models mode chips');
});

test('no selectable model is itself a required-field reason', () => {
  const nodes = { 'cf-gate': element(), 'cf-go': element() };
  const { context } = harness(nodes);
  vm.runInContext("createTab = 'music'; updateCreateGate();", context);
  assert.equal(nodes['cf-go'].disabled, true);
  assert.match(nodes['cf-gate'].textContent, /No model is ready for this lane/);
});

test('the image seed field is optional and the empty lane names a missing key', () => {
  assert.match(SECTION, /k: 'seed'/);
  assert.match(SECTION, /Optional\. Repeat the same seed/);
  assert.match(SECTION, /provider has no key yet/,
    'an enabled model on an unconfigured provider explains itself instead of a dead end');
});

test('the Saved voice / Provider voice ID decision is one exclusive control', () => {
  const nodes = {
    'cf-voice-mode-saved': element({ attrs: {} }),
    'cf-voice-mode-manual': element({ attrs: {} }),
    'cf-voice_id': element({ value: 'studio-hub-local::voice-1' }),
    'cf-voice_id_manual': element({ value: 'typed', style: { display: 'none' } }),
    'cf-gate': element(),
    'cf-go': element(),
  };
  nodes['cf-voice-mode-saved'].setAttribute = function (name, value) { this.attrs[name] = value; };
  nodes['cf-voice-mode-manual'].setAttribute = function (name, value) { this.attrs[name] = value; };
  const { context } = harness(nodes);
  vm.runInContext("createTab = 'audio'; setVoiceMode('manual');", context);
  assert.equal(nodes['cf-voice-mode-manual'].attrs['aria-pressed'], 'true');
  assert.equal(nodes['cf-voice_id_manual'].style.display, '');
  assert.equal(vm.runInContext('createVoiceValue()', context), 'typed');
  vm.runInContext("setVoiceMode('saved');", context);
  assert.equal(nodes['cf-voice-mode-saved'].attrs['aria-pressed'], 'true');
  assert.equal(nodes['cf-voice_id_manual'].style.display, 'none');
  assert.equal(nodes['cf-voice_id_manual'].value, '', 'the other answer never travels too');
});
