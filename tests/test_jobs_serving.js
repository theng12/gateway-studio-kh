// Jobs: Location and Serving machine are separate, and the serving Mac is only named when known.
//
// Owner, 2026-11-15: "we don't get to see which actual machine is serving request?" The page
// must never dress the targeted location up as the serving machine, and a queued job must say
// it is waiting rather than showing a Mac.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
const START = source.indexOf('function imageModeLabel');
const END = source.indexOf('async function loadJobs()');
if (START < 0 || END < 0) throw new Error('Jobs section boundary changed');

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
const ctx = vm.createContext({
  esc,
  ago: () => 'just now',
  stateBadgeClass: () => '',
  requesterLabel: () => 'Story Studio',
  Number,
});
vm.runInContext(source.slice(START, END), ctx);
const cell = job => vm.runInContext(`servingMachineCell(${JSON.stringify(job)})`, ctx);

test('a queued job says it is waiting, never the targeted location', () => {
  assert.match(cell({ state: 'accepted', fleet: true, executed_by: null }), /Waiting for execution/);
  assert.match(cell({ state: 'running', fleet: true, executed_by: null }), /Not reported by the Hub yet/);
});

test('the Hub machine is named when it reported one, with the right verb', () => {
  assert.match(cell({ state: 'running', fleet: true, executed_by: 'mac-7' }),
    /mac-7[\s\S]*Accepted & executing here/);
  assert.match(cell({ state: 'succeeded', fleet: true, executed_by: 'mac-7' }),
    /mac-7[\s\S]*Ran here/);
});

test('a cloud job says it has no Mac, and a settled fleet job with no evidence says so', () => {
  assert.match(cell({ state: 'running', fleet: false, executed_by: null }), /Cloud provider — no Mac/);
  assert.match(cell({ state: 'succeeded', fleet: false, executed_by: null }), /Cloud provider — no Mac/);
  assert.match(cell({ state: 'failed', fleet: true, executed_by: null }), /Not recorded/);
});

test('the table separates Location from Serving machine and drops the not-recorded noise', () => {
  const rows = [{ id: 'abcdef123456', short_id: 'abcdef12', modality: 'image',
    image_mode: 'text-to-image', submitted_reference_count: 0, state: 'accepted',
    model_id: 'qwen-image', model_name: 'Qwen Image', provider_name: 'studio-1500',
    fleet: true, executed_by: null, created_at: Date.now() / 1000, caller: 'story' }];
  const html = vm.runInContext(`jobsTable(${JSON.stringify(rows)})`, ctx);
  assert.match(html, /<th>Location<\/th>/);
  assert.match(html, /<th>Serving machine<\/th>/);
  assert.doesNotMatch(html, /<th>Where<\/th>/);
  assert.match(html, /studio-1500/);
  assert.match(html, /Waiting for execution/);
  assert.doesNotMatch(html, /Gateway settled: not recorded/);
  assert.doesNotMatch(html, /Gateway dispatched: not recorded/);
  assert.match(html, /Not dispatched yet/);
});
