const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_image_limits.js'), 'utf8');
function fixture() {
  const target = {innerHTML: ''}, message = {textContent: ''};
  const context = {window: {}, document: {getElementById: id => id === 'image-limits' ? target : message},
    esc: value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))};
  vm.createContext(context); vm.runInContext(source, context);
  return {ui: context.window.GatewayImageLimits, target};
}
function data(change = null) {
  const value = {allowed: true, max_reference_images: 1, swap_allowance_gb: 0, source: 'provisional, measuring'};
  return {swap_allowance_units: 'decimal GB from baseline', rows: [{provider_id: 'studio-hub-fixture', provider_label: 'Fixture', model_id: '<model>', mode: 'edit', reference_count: 1,
    tiers: {'24': {default: value, effective: value, change, detail: 'Saved here; not applied.'}, '48': {default: value, effective: value, change: null}}}]};
}
test('tiers and provenance come from data, and model text is escaped', () => {
  const {ui, target} = fixture(); ui.render(data());
  assert.match(target.innerHTML, /48 GiB/); assert.match(target.innerHTML, /provisional, measuring/);
  assert.match(target.innerHTML, /&lt;model&gt;/); assert.doesNotMatch(target.innerHTML, /<model>/);
  assert.match(target.innerHTML, /swap 0 GB/); assert.match(target.innerHTML, /overflow-x:auto/);
});
test('pending your change never replaces the effective cap or current default', () => {
  const {ui, target} = fixture(); ui.render(data({allowed: true, max_reference_images: 5, swap_allowance_gb: 2}));
  assert.match(target.innerHTML, /your change/); assert.match(target.innerHTML, /On · max 1 · swap 0 GB/);
  assert.match(target.innerHTML, /max 5 · swap 2 GB/); assert.match(target.innerHTML, /Hub default:/);
  assert.match(target.innerHTML, /not applied/);
});
test('save and Reset preserve published model, mode, count and tier identity', () => {
  const {ui} = fixture(); ui.render(data());
  const form = {dataset: {limitRow: '0', limitTier: '24'}, elements: {allowed: {value: 'false'}, maximum: {value: '0'}, swap: {value: '0'}}};
  const saved = JSON.parse(JSON.stringify(ui.payload(form, false)));
  assert.deepEqual(saved, {provider_id: 'studio-hub-fixture', model_id: '<model>', mode: 'edit', reference_count: 1, tier: '24', change: {allowed: false, max_reference_images: 0, swap_allowance_gb: 0}});
  assert.equal(ui.payload(form, true).change, null);
});
test('invalid numbers and missing cells never produce a save payload', () => {
  const {ui} = fixture(); ui.render(data());
  const form = {dataset: {limitRow: '0', limitTier: '24'}, elements: {allowed: {value: 'true'}, maximum: {value: '1'}, swap: {value: 'Infinity'}}};
  assert.throws(() => ui.payload(form, false), /finite/);
  form.elements.swap.value = '0'; form.elements.maximum.value = '';
  assert.throws(() => ui.payload(form, false), /whole/);
  form.dataset.limitTier = 'unknown'; assert.throws(() => ui.payload(form, true), /Refresh/);
});
test('empty tables explain what must be refreshed', () => {
  const {ui, target} = fixture(); ui.render({rows: []});
  assert.match(target.innerHTML, /No Hub has reported/); assert.match(target.innerHTML, /Refresh/);
});
