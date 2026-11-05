const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../gateway/admin_developer_tests.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
const context = {window: {}};
vm.runInNewContext(script, context);
const {common, unknown} = context.window.DeveloperTestsUI;
function cap() {
  const ranges = Object.fromEntries(['width_px','height_px','steps','seed','reference_count','prompt_characters','guidance'].map(k => [k, {minimum: 0, maximum: 1000}]));
  ranges.width_px.minimum = ranges.height_px.minimum = 64;
  ranges.dimension_multiple = 8;
  return {supported: true, setting: {enabled: true, verified: true}, bounds: {input_refs: 4},
    models: [{model: {id: 'model'}, installable: true, settings_bounds: ranges, memory_modes: ['default', 'bounded-staged']}],
    test_kinds: ['reference_counts'], fixture_sets: [{id:'fixtures', sha256:'digest'}],
    safety_policy: {requested_limits: {max_runtime_seconds: {minimum:1, maximum:60, unit:'seconds'}}}};
}
test('unknown metrics are distinct from a measured zero', () => {
  assert.equal(unknown(null), 'Not recorded'); assert.equal(unknown(0), '0');
});
test('common bounds intersect live minima and maxima', () => {
  const a=cap(), b=cap(); b.models[0].settings_bounds.steps = {minimum:3, maximum:7};
  const r=common([a,b], 'model', 80);
  assert.equal(r.bounds.steps.minimum,3); assert.equal(r.bounds.steps.maximum,7);
  assert.equal(r.bounds.prompt_characters.maximum,80); assert.equal(r.bounds.reference_count.maximum,4);
});
test('dimension multiples intersect rather than guessing a canvas', () => {
  const a=cap(), b=cap(); b.models[0].settings_bounds.dimension_multiple=12;
  const r=common([a,b], 'model',1000); assert.equal(r.multiple,24); assert.equal(r.bounds.width_px.minimum,72);
});
test('OFF, unverified and unsupported targets cannot produce a vector', () => {
  for (const change of [c=>c.setting.enabled=false,c=>c.setting.verified=false,c=>c.supported=false]) {
    const c=cap(); change(c); assert.throws(()=>common([c],'model',1000));
  }
});
test('incompatible ranges and absent models refuse', () => {
  const a=cap(), b=cap(); b.models[0].settings_bounds.steps.minimum=1001;
  assert.throws(()=>common([a,b],'model',1000)); assert.throws(()=>common([a],'missing',1000));
});
test('guidance is null only when every target declares it unused', () => {
  const a=cap(), b=cap(); a.models[0].settings_bounds.guidance=null;
  assert.throws(()=>common([a,b],'model',1000)); b.models[0].settings_bounds.guidance=null;
  assert.equal(common([a,b],'model',1000).bounds.guidance,null);
});
test('fixture bytes and memory modes must agree across targets', () => {
  const a=cap(), b=cap(); b.fixture_sets[0].sha256='different';
  assert.throws(()=>common([a,b],'model',1000)); b.fixture_sets[0].sha256='digest';
  b.models[0].memory_modes=['other']; assert.throws(()=>common([a,b],'model',1000));
});
test('shared page has navigation, hash, section, script and app Fleet entry', () => {
  assert.match(html,/view: 'developer-tests', label: 'Run test'/);
  assert.match(html,/id="view-developer-tests"/); assert.match(html,/hash.startsWith\('developer-tests'\)/);
  assert.match(html,/loadDeveloperTests\(\)/); assert.match(html,/fetch\('\/admin\/developer-tests.js', \{headers: adminHeaders\(\)\}\)/);
  assert.match(html,/onclick="openNav\('developer-tests'\)"/);
});
test('evidence uses text rendering and has no polling or automatic retry loop', () => {
  assert.doesNotMatch(script,/\.innerHTML\s*=/); assert.doesNotMatch(script,/setInterval|setTimeout/);
  assert.match(script,/Review evidence/); assert.match(script,/Approve measured evidence/);
  assert.match(script,/Restore previous evidence/);
});
