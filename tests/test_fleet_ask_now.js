// "Ask now" on one location: the sentence the owner reads, and the page's own cache after the read.
//
// The route (POST /api/fleet/controllers/<id>/refresh) is covered by tests/test_fleet_attention_current.py.
// This file covers the page side the backlog said was missing: a control that calls it, a sentence that
// says whether the answer is current, and the page's copy of the fleet being replaced for that location
// instead of waiting for the next whole-fleet read.

const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');

function askCode() {
  const first = source.indexOf('// ---- Ask one location now');
  const last = source.indexOf('async function fleetLocationAction(');
  if (first < 0 || last < 0) throw new Error('the Ask now section moved; update this test');
  return source.slice(first, last) + `
    ;globalThis.__ask = { fleetLocationReadNote, fleetAskOneLocation, fleetApplyLocationRead };`;
}

function context(payload, notes) {
  let rendered = 0;
  const ctx = vm.createContext({
    console,
    lastFleetPayload: payload,
    fleetMachineRows: [],
    renderFleetTabs: () => {},
    renderFleet: () => { rendered += 1; },
    fleetLocationNote: text => notes.push(text),
    api: null,
  });
  vm.runInContext(askCode(), ctx);
  return { ctx, rendered: () => rendered };
}

test('a location that answered names how many Macs and when', () => {
  const when = new Date(2026, 10, 22, 9, 5, 0).getTime() / 1000;
  const { ctx } = context(null, []);
  const sentence = ctx.__ask.fleetLocationReadNote({
    id: 'studio-hub-blue', label: 'Blue Room', reachable: true, bound_seconds: 20,
    measured_at: when, machines: [{}, {}],
  });
  assert.match(sentence, /Blue Room answered inside its 20 s bound — 2 Macs measured at/);
});

test('a location that missed its bound says the reason and that its Macs are last-seen', () => {
  const { ctx } = context(null, []);
  const sentence = ctx.__ask.fleetLocationReadNote({
    id: 'shops', reachable: false, bound_seconds: 35,
    error: 'no answer in 35 s', machines: [{}],
  });
  assert.match(sentence, /shops did not answer inside its 35 s bound — no answer in 35 s/);
  assert.match(sentence, /Its Mac is shown from the last read/);
});

test('a silent site with no reason never reads as a fresh fault', () => {
  const { ctx } = context(null, []);
  const sentence = ctx.__ask.fleetLocationReadNote({id: 'shops', reachable: false, bound_seconds: 10, machines: []});
  assert.match(sentence, /no reason was given/);
});

test('the answer replaces that location in the page copy, and only that location', () => {
  const payload = {
    generated_at: 1,
    machines: [
      {location_id: 'shops', machine: 'mac-a', reachable: false, attention_source: 'last_known'},
      {location_id: 'blue', machine: 'mac-b', reachable: true},
    ],
    locations: [
      {id: 'shops', reachable: false, error: 'no answer'},
      {id: 'blue', reachable: true},
    ],
  };
  const carrier = context(payload, []);
  carrier.ctx.__ask.fleetApplyLocationRead('shops', {
    id: 'shops', label: 'Shops', reachable: true, machines: [{location_id: 'shops', machine: 'mac-a', reachable: true}],
  });
  const after = carrier.ctx.lastFleetPayload;
  assert.equal(after.machines.filter(m => m.location_id === 'shops').length, 1);
  assert.equal(after.machines.find(m => m.location_id === 'shops').reachable, true);
  assert.equal(after.machines.find(m => m.location_id === 'blue').machine, 'mac-b', 'another location is left alone');
  assert.equal(after.locations.find(l => l.id === 'shops').reachable, true);
  assert.equal(after.locations.find(l => l.id === 'shops').error, null);
  assert.equal(carrier.rendered(), 1);
});

test('the control is wired on both the location row and the site card', () => {
  const wiring = source.split('fleetAskOneLocation(this)').length - 1;
  assert.ok(wiring >= 2, `Ask now must be reachable from the row and the card (found ${wiring})`);
});
