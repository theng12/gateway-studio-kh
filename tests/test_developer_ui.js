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
test('chip, physical RAM and node filters intersect without guessing unknown hardware', () => {
  const targets = [
    {node_id:'m2-pro-32', label:'Fleet Mac', chip:'Apple M2 Pro', ram_bytes:32*1024**3},
    {node_id:'m2-pro-16', label:'Fleet Mac', chip:'Apple M2 Pro', ram_bytes:16*1024**3},
    {node_id:'m2-32', label:'Fleet Mac', chip:'Apple M2', ram_bytes:32*1024**3},
    {node_id:'unknown', label:'Apple M2 Pro 32 GB', chip:null, ram_bytes:null},
  ];
  const rows = context.window.DeveloperTestsUI.matchingTargets(targets, {chip:'Apple M2 Pro', ram:String(32*1024**3), filter:'m2-pro-32'});
  assert.equal(rows.length,1); assert.equal(rows[0].node_id,'m2-pro-32');
  assert.equal(context.window.DeveloperTestsUI.matchingTargets(targets, {chip:'Apple M2 Pro', ram:String(32*1024**3), filter:'other'}).length,0);
});
test('pre-run inventory refresh retains exact selected identity and never expands targets', () => {
  const target={controller_id:'site',node_id:'chosen',chip:'Apple M2 Pro',ram_bytes:32*1024**3,online:true,paused:false};
  const inventory={fresh:true,inventory_at:456,targets:[target,{...target,node_id:'new-unselected'}]};
  const fresh=context.window.DeveloperTestsUI.freshInventoryAt;
  assert.equal(fresh([target],inventory),456);
  for (const change of [{online:false},{paused:true},{chip:'Apple M4'},{ram_bytes:16*1024**3},{node_id:'replacement'}])
    assert.throws(()=>fresh([target],{...inventory,targets:[{...target,...change}]}));
  assert.throws(()=>fresh([target],{...inventory,fresh:false}));
  assert.throws(()=>fresh([target],{...inventory,targets:[target,target]}));
});
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
  assert.match(script,/Review evidence/); assert.match(script,/Approve and publish measured limits/);
  assert.match(script,/Restore previous evidence/);
});
