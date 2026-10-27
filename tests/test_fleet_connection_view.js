/**
 * The Fleet page's network link: which Macs are on a thin Ethernet cable.
 *
 * The owner, 2026-10-28: Mac 1301 was linked at `100baseTX` on an old cable — `10baseT` before
 * that — and the Fleet page could not show it, because a Mac on a thin link is `reachable`,
 * `ready` and takes work. So the Hub publishes its own reading and this page draws it.
 *
 * What this file pins, in the page's own code:
 *
 * 1. **A slow Ethernet link is called out** as "slow link — check cable/port", with the speed.
 * 2. **Wi-Fi is named as Wi-Fi**, never as a slow cable — the fix is different.
 * 3. **A Mac whose Hub does not publish the block draws "—"**, never a claim that it is fine.
 * 4. **The fleet summary counts the slow Macs**, so the figure and the tiles cannot disagree.
 *
 * Nothing here touches the app or the running gateway: it is a VM over the page's own source.
 */
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

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function element() {
  return {
    innerHTML: '', textContent: '', className: '', value: '', disabled: false,
    querySelector: () => null, scrollIntoView() {},
  };
}

const FLEET_STATES = {
  ready: ['ok', 'Ready'], working: ['warn', 'Working'], offline: ['bad', 'Offline'],
};

function context(payload = null) {
  const nodes = new Map([
    ['fleet-body', element()], ['fleet-summary', element()], ['fleet-issues', element()],
  ]);
  const ctx = vm.createContext({
    document: { getElementById: id => nodes.get(id) },
    esc,
    ago: () => 'just now',
    FLEET_STATES,
    state: { view: 'fleet' },
    nodes,
    // `loadFleet` reads `/fleet` through the page's own `api()`; the stub stands in for the gateway.
    api: async () => payload,
  });
  vm.runInContext(between('function fleetMac(m)', 'async function loadJobs()'), ctx);
  return ctx;
}

function connection(overrides = {}) {
  return {
    status: 'measured', reason: null, interface: 'en0', kind: 'ethernet',
    speed_mbps: 1000, duplex: 'full', media: 'autoselect (1000baseT <full-duplex>)',
    link_up: true, wifi_up: false, wifi_rate_mbps: null, slow: false,
    text: 'Ethernet 1000 Mb/s full', ...overrides,
  };
}

function machine(overrides = {}) {
  return {
    machine: '1301', label: '1301', state: 'ready', reachable: true,
    hub_version: '0.6.206', completed: 4, failed: 0, connection: connection(), ...overrides,
  };
}

test('a slow Ethernet link is called out by name with its speed', () => {
  const ctx = context();
  const html = ctx.fleetMac(machine({ connection: connection({
    speed_mbps: 100, duplex: 'full', text: 'Ethernet 100 Mb/s full', slow: true,
  }) }));
  assert.match(html, /Slow link — check cable\/port/);
  assert.match(html, /100 Mb\/s/);
  // The reading itself is on the facts line, so the tile says what it is as well as that it is wrong.
  assert.match(html, /Network: Ethernet 100 Mb\/s full/);
});

test('a gigabit link is shown and not called slow', () => {
  const ctx = context();
  const html = ctx.fleetMac(machine());
  assert.doesNotMatch(html, /Slow link/);
  assert.match(html, /Network: Ethernet 1000 Mb\/s full/);
});

test('Wi-Fi is named as Wi-Fi and never as a slow cable', () => {
  const ctx = context();
  const html = ctx.fleetMac(machine({ connection: connection({
    interface: 'en1', kind: 'wifi', speed_mbps: null, duplex: null,
    wifi_up: true, wifi_rate_mbps: 432, slow: false, text: 'Wi-Fi 432 Mb/s',
  }) }));
  assert.match(html, /Network: Wi-Fi 432 Mb\/s/);
  assert.doesNotMatch(html, /Slow link/);
});

test('a Mac whose Hub does not publish its link draws an em dash, not a healthy link', () => {
  const ctx = context();
  const html = ctx.fleetMac({ ...machine(), connection: undefined });
  assert.match(html, /Network: —/);
  assert.doesNotMatch(html, /Ethernet/);
});

test('an unmeasured block draws an em dash and keeps the reason in the tooltip', () => {
  const ctx = context();
  const html = ctx.fleetMac(machine({ connection: connection({
    status: 'unmeasured', reason: "This Mac's routing table could not be read.",
    interface: null, kind: null, speed_mbps: null, duplex: null, text: '—', slow: false,
  }) }));
  assert.match(html, /Network: —/);
  assert.match(html, /routing table could not be read/);
  assert.doesNotMatch(html, /Slow link/);
});

test('the fleet summary counts and names the Macs on slow Ethernet', async () => {
  const ctx = context({
    summary: {
      machines: 3, machines_online: 3, locations: 1, locations_up: 1,
      connections: { slow_ethernet: 2, slow_machines: ['1301', '1304'], wifi: 0, ethernet: 3,
        unmeasured: 0, total: 3 },
    },
    gateway_queue: { waiting: 0, running: 0 },
    locations: [],
    machines: [],
    issues: [],
    generated_at: Math.floor(Date.now() / 1000),
  });
  await ctx.loadFleet();
  const html = ctx.nodes.get('fleet-summary').innerHTML;
  assert.match(html, /2 on slow Ethernet/);
  // The Macs are named in the tooltip: the point of the figure is which cable to replace.
  assert.match(html, /title="1301, 1304"/);
});

test('a fleet with no slow link draws no slow-Ethernet figure at all', async () => {
  const ctx = context({
    summary: {
      machines: 1, machines_online: 1, locations: 1, locations_up: 1,
      connections: { slow_ethernet: 0, slow_machines: [], wifi: 1, ethernet: 0,
        unmeasured: 0, total: 1 },
    },
    gateway_queue: { waiting: 0, running: 0 },
    locations: [], machines: [], issues: [],
    generated_at: Math.floor(Date.now() / 1000),
  });
  await ctx.loadFleet();
  assert.doesNotMatch(ctx.nodes.get('fleet-summary').innerHTML, /slow Ethernet/);
});
