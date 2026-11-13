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
    test_kinds: ['reference_counts'], fixture_sets: [{id:'fixtures', sha256:'digest', reference_order: ['ref-1']}],
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
  // The door is the sidebar row, drawn by the nav template with the row's own view key —
  // a second literal button in Fleet was a duplicate door and is gone.
  assert.match(html,/onclick="openNav\('\$\{esc\(value\)\}'\)"/);
  assert.match(html,/function openNav\(value\)/);
});
test('evidence uses text rendering and has no polling or automatic retry loop', () => {
  assert.doesNotMatch(script,/\.innerHTML\s*=/); assert.doesNotMatch(script,/setInterval|setTimeout/);
  assert.match(script,/Review evidence/); assert.match(script,/Approve and publish measured limits/);
  assert.match(script,/Restore previous evidence/);
});
test('terminal runs guard against cancel action', () => {
  assert.match(script, /!terminal/);
});
test('review evidence includes close review action', () => {
  assert.match(script, /Close review/);
});
test('matchingTargets trims filter whitespace and handles null labels safely', () => {
  const targets = [
    {node_id:'mac-one', label:null, chip:'Apple M2', ram_bytes:16*1024**3},
    {node_id:'mac-two', label:'Worker Studio', chip:'Apple M2 Pro', ram_bytes:32*1024**3},
  ];
  assert.equal(context.window.DeveloperTestsUI.matchingTargets(targets, {filter:'  mac-one  '}).length, 1);
  assert.equal(context.window.DeveloperTestsUI.matchingTargets(targets, {filter:'worker'}).length, 1);
  assert.equal(context.window.DeveloperTestsUI.matchingTargets(targets, {filter:'nonexistent'}).length, 0);
});

// --- executed behaviour: the page wrapper and the Run form, driven as events ---

function domEl(tag = 'div') {
  const listeners = {};
  return {
    tagName: tag, textContent: '', value: '', checked: false, disabled: false,
    className: '', hidden: false, type: '', name: '', id: '', min: '', max: '',
    step: '', required: false, children: [], options: [], attributes: {}, style: {},
    get childNodes() { return this.children; },
    append(...items) {
      for (const item of items) {
        this.children.push(item);
        if (this.tagName === 'select' && item && item.tagName === 'option') this.options.push(item);
      }
    },
    replaceChildren(...items) { this.children = items; if (this.tagName === 'select') this.options = []; },
    setAttribute(k, v) { this.attributes[k] = v; },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    async dispatch(type, event) {
      const results = [];
      for (const fn of listeners[type] || []) results.push(fn(event));
      return Promise.all(results);
    },
    reportValidity: () => true,
  };
}

function pageDocument(nodes) {
  return {
    getElementById: id => nodes[id] ||= domEl(),
    createElement: tag => domEl(tag),
    createTextNode: text => ({textContent: text}),
    head: {append() {}},
  };
}

const indexPayload = () => ({
  fresh: true, inventory_at: 1000, reader_byte_ceiling: 8388608,
  targets: [{controller_id: 'site', node_id: 'mac-one', label: 'Fleet Mac',
             chip: 'Apple M2 Pro', ram_bytes: 32 * 1024 ** 3, online: true, paused: false}],
  runs: [],
});

test('page load executes the developer loader exactly once, never twice', async () => {
  let loads = 0;
  let indexReads = 0;
  const nodes = {};
  const sandbox = {
    window: {},
    api: async path => { if (path === '/developer-tests') indexReads += 1; return path === '/developer-tests' ? indexPayload() : {store_revision: 'r1', rows: []}; },
    crypto: {randomUUID: () => '00000000-0000-4000-8000-000000000001'},
    fetch: async () => ({ok: true, text: async () => script}),
    adminHeaders: () => ({}),
    document: pageDocument(nodes),
  };
  sandbox.document.head.append = node => {
    vm.runInContext(node.textContent, sandboxContext);
    const inner = sandbox.window.loadDeveloperTests;
    sandbox.window.loadDeveloperTests = () => { loads += 1; return inner(); };
  };
  const sandboxContext = vm.createContext(sandbox);
  vm.runInContext(between(html, 'let developerPageLoad;', 'const NAV = ['), sandboxContext);
  await vm.runInContext('loadDeveloperTests()', sandboxContext);
  assert.equal(loads, 1, 'the wrapper must invoke the page loader exactly once');
  assert.equal(indexReads, 1, 'the initial load must read the ledger exactly once');
});

function between(source, start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + 1);
  if (first < 0 || last < 0) throw new Error(`admin_ui.html moved: ${start}`);
  return source.slice(first, last);
}

async function runFormHarness() {
  const nodes = {};
  let mode = 'ok';
  let postAttempts = 0;
  const sandbox = {
    window: {},
    document: pageDocument(nodes),
    crypto: {randomUUID: () => '00000000-0000-4000-8000-000000000002'},
    api: async (path, opts) => {
      if (opts && opts.method === 'POST') {
        if (path === '/developer-tests/runs') {
          postAttempts += 1;
          throw new Error('connection reset');
        }
        return {};
      }
      if (path === '/developer-tests') return mode === 'stale' ? {...indexPayload(), fresh: false} : indexPayload();
      if (path === '/developer-tests/measurements') return {store_revision: 'r1', rows: []};
      if (path.startsWith('/developer-tests/targets/')) return {capabilities: cap()};
      return {};
    },
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(script, ctx);
  await sandbox.window.loadDeveloperTests();
  const row = nodes['developer-targets'].children[0];
  const checkbox = row.children[0].children[0];
  checkbox.checked = true;
  await checkbox.dispatch('change', {});
  const form = nodes['developer-vector'].children.at(-1);
  assert.equal(form.tagName, 'form');
  const run = form.children.at(-1);
  assert.equal(run.tagName, 'button');
  return {
    run, form, nodes,
    setMode: value => { mode = value; },
    postAttempts: () => postAttempts,
    // A disabled submit control cannot submit its form, so the harness mirrors the browser.
    submit: () => run.disabled ? Promise.resolve() : form.dispatch('submit', {preventDefault() {}}),
    message: () => nodes['developer-message'].textContent,
  };
}

test('a preflight failure with no submission re-enables Run and says nothing was sent', async () => {
  const h = await runFormHarness();
  h.setMode('stale');
  await h.submit();
  assert.equal(h.postAttempts(), 0, 'a stale inventory must not reach POST /runs');
  assert.equal(h.run.disabled, false, 'no submission means the owner may correct and Run again');
  assert.match(h.message(), /Nothing was submitted/);
});

test('an attempted submission with an uncertain outcome keeps Run locked', async () => {
  const h = await runFormHarness();
  h.setMode('ok');
  await h.submit();
  assert.equal(h.postAttempts(), 1);
  assert.equal(h.run.disabled, true, 'an unknown POST outcome must stay locked for recovery');
  assert.match(h.message(), /attempted submission may need recovery/);
  await h.submit();
  assert.equal(h.postAttempts(), 1, 'a locked Run button must not fire a second submission');
});
