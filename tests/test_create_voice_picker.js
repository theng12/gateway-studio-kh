// The Create page's voice picker — driven in a VM over the page's own source.
//
// The owner's report (2026-10-28): the Voice field showed `studio-hub-local::voice-ba75c0a0aa4f`,
// not the name the catalogue publishes. Three separate things had to be true for that to happen,
// and each is pinned here:
//
//   1. **The published name is read.** `/admin/api/voices` answers `display_name`; the page read
//      `x.name`, which is not a field, so every row fell back to its id. A `<datalist>` then shows
//      the *value* anyway, so even a correct label would have been hidden behind an id.
//   2. **Only the model's voices are offered, and the list is asked again when the model changes.**
//      The page drew the whole library for every lane, and drew it once for the life of the tab.
//   3. **A voice with no published name says so** — "Unnamed voice · ba75c0" — rather than
//      printing the raw id as though it were a person's name.
//
// No app, no gateway, no network: the page's functions run against a hand-built DOM.

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
  return { value: '', innerHTML: '', textContent: '', className: '', style: {}, ...extra };
}

function harness(nodes, answers = {}) {
  const calls = [];
  const context = vm.createContext({
    document: { getElementById: id => nodes[id] || null },
    esc,
    api: async (path) => {
      calls.push(path);
      if (!(path in answers)) throw new Error(`unexpected request ${path}`);
      return answers[path];
    },
  });
  vm.runInContext(SECTION, context);
  return { context, calls, nodes };
}

function withVoices(modelId = 'studio-hub::mock-hub/clone-model') {
  const nodes = {
    'cf-model': element({ value: modelId }),
    'cf-voice_id': element(),
    'cf-voice_id_manual': element({ style: { display: 'none' } }),
    'cf-voice-hint': element(),
  };
  const rows = [
    { id: 'studio-hub-local::voice-0616aec33b41', display_name: 'Christian',
      provider: 'Gateway voice library' },
    { id: 'studio-hub-local::voice-ba75c0a0aa4f', display_name: 'voice-ba75c0a0aa4f',
      provider: 'Gateway voice library' },
  ];
  const answer = { voices: rows };
  const built = harness(nodes, { [`/voices?model_id=${encodeURIComponent(modelId)}`]: answer });
  return { ...built, rows, modelId };
}

test('the published name is what is read, and an id is never printed as a name', () => {
  const { context } = harness({});
  const name = vm.runInContext('createVoiceName', context);
  assert.equal(name({ id: 'studio-hub-local::voice-0616aec33b41', display_name: 'Christian' }),
    'Christian');
  // The catalogue's own fallback for an entry with nothing published is the raw id in the
  // name field. Printing that is the defect; saying it has no name is the fix.
  assert.equal(name({ id: 'studio-hub-local::voice-ba75c0a0aa4f',
    display_name: 'voice-ba75c0a0aa4f' }), 'Unnamed voice · ba75c0');
  assert.equal(name({ id: 'studio-hub-local::voice-ba75c0a0aa4f' }), 'Unnamed voice · ba75c0');
});

test('the provider is shown when the gateway publishes one', () => {
  const { context } = harness({});
  const label = vm.runInContext('createVoiceLabel', context);
  assert.equal(label({ id: 'a::b', display_name: 'Christian', provider: 'Gateway voice library' }),
    'Christian · Gateway voice library');
  assert.equal(label({ id: 'a::b', display_name: 'Christian' }), 'Christian');
  // Never "Christian · Christian".
  assert.equal(label({ id: 'a::b', display_name: 'Christian', provider: 'Christian' }), 'Christian');
});

test('the picker offers only this model\'s voices, by id, and asks again per model', async () => {
  const { context, calls, nodes, rows, modelId } = withVoices();
  await vm.runInContext('loadCreateVoices()', context);
  assert.equal(calls.length, 1);
  assert.equal(calls[0], `/voices?model_id=${encodeURIComponent(modelId)}`, 'the model is sent');
  const html = nodes['cf-voice_id'].innerHTML;
  for (const row of rows) {
    assert.ok(html.includes(`<option value="${esc(row.id)}"`), `the id is the option value: ${row.id}`);
  }
  assert.ok(html.includes('Christian · Gateway voice library'), html);
  assert.ok(html.includes('Unnamed voice · ba75c0'), html);
  assert.ok(!html.includes('<option value="studio-hub-local::voice-0616aec33b41">studio-hub-local'),
    'the id must never be the visible label');
  assert.ok(html.includes('<option value="__manual__">Enter a voice id…</option>'),
    'typing an id stays possible');
  assert.equal(nodes['cf-voice-hint'].textContent, '2 voices this model can use.');
  assert.equal(nodes['cf-voice_id_manual'].style.display, 'none');

  // The same model again is served from the answer already held…
  await vm.runInContext('loadCreateVoices()', context);
  assert.equal(calls.length, 1, 'a re-render must not re-ask for the same model');

  // …and a different model asks again, because the answer belongs to the model.
  const otherModel = 'ui-fixture::mock-tts-1';
  nodes['cf-model'].value = otherModel;
  const second = harness(nodes, {
    [`/voices?model_id=${encodeURIComponent(otherModel)}`]: { voices: [] },
  });
  await vm.runInContext('loadCreateVoices()', second.context);
  assert.deepEqual(second.calls, [`/voices?model_id=${encodeURIComponent(otherModel)}`],
    'a new model is a new question');
});

test('an empty answer and a failed answer are different sentences', async () => {
  const nodes = {
    'cf-model': element({ value: 'ui-fixture::mock-tts-1' }),
    'cf-voice_id': element(),
    'cf-voice_id_manual': element({ style: { display: 'none' } }),
    'cf-voice-hint': element(),
  };
  const empty = harness(nodes, { '/voices?model_id=ui-fixture%3A%3Amock-tts-1': { voices: [] } });
  await vm.runInContext('loadCreateVoices()', empty.context);
  assert.equal(nodes['cf-voice_id'].innerHTML, '<option value="__manual__">Enter a voice id…</option>');
  assert.equal(nodes['cf-voice_id_manual'].style.display, '', 'typing an id is offered instead');
  assert.match(nodes['cf-voice-hint'].textContent, /No voice in the library can be used with this model/);
  assert.equal(nodes['cf-voice-hint'].className, 'hint');

  const failing = vm.createContext({
    document: { getElementById: id => nodes[id] || null },
    esc,
    api: async () => { throw new Error('HTTP 500'); },
  });
  vm.runInContext(SECTION, failing);
  await vm.runInContext('loadCreateVoices()', failing);
  assert.match(nodes['cf-voice-hint'].textContent, /Could not read this model's voices: HTTP 500/);
  assert.equal(nodes['cf-voice-hint'].className, 'hint bad', 'a failure is loud, not a friendly empty');
  assert.equal(nodes['cf-voice_id'].innerHTML, '<option value="__manual__">Enter a voice id…</option>');
});

test('the id is what is submitted, whether picked or typed', () => {
  const nodes = {
    'cf-voice_id': element({ value: 'studio-hub-local::voice-0616aec33b41' }),
    'cf-voice_id_manual': element({ value: 'typed-by-hand' }),
  };
  const { context } = harness(nodes);
  assert.equal(vm.runInContext('createVoiceValue()', context),
    'studio-hub-local::voice-0616aec33b41');

  nodes['cf-voice_id'].value = '__manual__';
  assert.equal(vm.runInContext('createVoiceValue()', context), 'typed-by-hand');

  nodes['cf-voice_id_manual'].value = '  studio-hub-local::voice-x  ';
  assert.equal(vm.runInContext('createVoiceValue()', context), 'studio-hub-local::voice-x');
});

test('a stated gender is shown, and an unstated one prints nothing', () => {
  // The owner asked for `gender` (m/f/n) on a local voice (2026-10-29). The picker prints it
  // beside the name; **an unstated voice prints nothing**, because "neutral" is a real answer
  // and must not be the default for one nobody stated.
  const { context } = harness({});
  const label = vm.runInContext('createVoiceLabel', context);
  assert.equal(label({ id: 'a::b', display_name: 'Christian', gender: 'm',
    provider: 'Gateway voice library' }), 'Christian · male · Gateway voice library');
  assert.equal(label({ id: 'a::b', display_name: 'Christian', gender: 'f' }), 'Christian · female');
  assert.equal(label({ id: 'a::b', display_name: 'Christian', gender: 'n' }), 'Christian · neutral');
  assert.equal(label({ id: 'a::b', display_name: 'Christian', gender: '',
    provider: 'P' }), 'Christian · P');
  assert.equal(label({ id: 'a::b', display_name: 'Christian', provider: 'P' }), 'Christian · P');
  assert.equal(label({ id: 'a::b', display_name: 'Christian', gender: 'robot' }), 'Christian',
    'an unrecognised value prints nothing rather than the raw word');
});

test('changing the model re-reads the voices and calls nothing that is not there', async () => {
  // **A regression this file was blind to.** Splitting the deferred delivery feature onto its
  // own branch deleted `applyCreateModelControls` and left `createModelChanged` calling it —
  // a ReferenceError on every model change of the Create page. No suite drove that function.
  const nodes = {
    'cf-model': element({ value: 'a::b' }),
    'cf-voice_id': element(),
    'cf-voice_id_manual': element({ style: { display: 'none' } }),
    'cf-voice-hint': element(),
  };
  const { context, calls } = harness(nodes, { '/voices?model_id=a%3A%3Ab': { voices: [] } });
  vm.runInContext('createTab = "audio";', context);
  assert.doesNotThrow(() => vm.runInContext('createModelChanged()', context),
    'a model change must not throw');
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls, ['/voices?model_id=a%3A%3Ab'], 'and re-asks for that model\'s voices');
});

test('the Subtitle tab says what this model accepts, read from its row', () => {
  const nodes = {
    'cf-model': element({ value: 'hub::whisper' }),
    'cf-upload-limit': element(),
  };
  const { context } = harness(nodes);
  const catalogue = { by_modality: { transcription: [
    { id: 'hub::whisper', upload_limit_bytes: 100 * 1024 * 1024 },
    { id: 'cloud::whisper', upload_limit_bytes: 25 * 1024 * 1024 },
    { id: 'quiet::whisper' },
  ] } };
  vm.runInContext(`createTab = 'subtitle'; createCatalogue = ${JSON.stringify(catalogue)};`,
    context);
  vm.runInContext('showUploadLimit()', context);
  assert.equal(nodes['cf-upload-limit'].textContent,
    'This model accepts up to 100 MB per recording.');
  nodes['cf-model'].value = 'cloud::whisper';
  vm.runInContext('showUploadLimit()', context);
  assert.equal(nodes['cf-upload-limit'].textContent,
    'This model accepts up to 25 MB per recording.');
  // A row that states no limit says nothing: never a number we invented.
  nodes['cf-model'].value = 'quiet::whisper';
  vm.runInContext('showUploadLimit()', context);
  assert.equal(nodes['cf-upload-limit'].textContent, '');
});

test('the download name is read from the route, not rebuilt in the page', () => {
  const { context } = harness({});
  const filename = vm.runInContext('filenameFromDisposition', context);
  assert.equal(filename('attachment; filename="tts-5417efcd-0.wav"'), 'tts-5417efcd-0.wav');
  assert.equal(filename('attachment; filename*=UTF-8\'\'a%20b.srt'), 'a%20b.srt');
  assert.equal(filename(''), '');
  assert.equal(filename(null), '');
});

test('the Create section keeps no datalist and no invented "name" field', () => {
  assert.ok(!/<datalist/.test(SECTION) && !/list="cf-voices"/.test(SECTION),
    'a datalist shows its value, which here is an id');
  assert.ok(!/\.name \|\||x\.name/.test(SECTION), 'the api answers display_name, not name');
});
