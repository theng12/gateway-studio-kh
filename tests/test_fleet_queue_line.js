// The Fleet Jobs line must count the background queue, never print the object.
//
// `/admin/api/fleet` sends `gateway_queue.background` as {waiting, running, waiting_by_modality}
// (store.stats). Interpolating it printed "[object Object]" on every empty fleet; an older
// payload sent a number, which still has to read as a number.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');

function between(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + 1);
  if (first < 0 || last < 0) throw new Error(`admin_ui.html moved: ${start}`);
  return source.slice(first, last);
}

function line(payload) {
  const context = vm.createContext({});
  vm.runInContext(between('function fleetQueueLine(d) {', 'function fleetJobProgress')
    + ';globalThis.__q = fleetQueueLine;', context);
  return context.__q(payload);
}

test('an object background is counted, never printed', () => {
  const text = line({ gateway_queue: { waiting: 2, running: 1,
    background: { waiting: 3, running: 4, waiting_by_modality: { video: 7 } } } });
  assert.equal(text, "This Gateway's own queue: 2 waiting, 1 running (7 background).");
  assert.doesNotMatch(text, /\[object/);
});

test('a numeric background from an older payload still reads, including zero', () => {
  assert.match(line({ gateway_queue: { waiting: 0, running: 0, background: 5 } }), /\(5 background\)/);
  assert.match(line({ gateway_queue: { waiting: 0, running: 0, background: 0 } }), /\(0 background\)/);
});

test('no background figure drops the clause rather than saying zero', () => {
  const text = line({ gateway_queue: { waiting: 0, running: 0 } });
  assert.equal(text, "This Gateway's own queue: 0 waiting, 0 running.");
});
