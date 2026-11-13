/**
 * Tests for Developer Gate inventory display in the Gateway Fleet machine view.
 *
 * Verifies that the existing web UI (admin_ui.html) correctly displays:
 * 1. Actual toolchain versions, pins, and availability status without synthetic guesses.
 * 2. Developer gate space usage and limits (workspace, spool, shared cache).
 * 3. Cleanup state, reason code/detail, and observation timestamp.
 * 4. Honest null representation ('—') vs numeric 0.
 * 5. Physical drive mapping and backing relationships in tooltips.
 * 6. Search matching across tool versions, cleanup states, and gate space facts.
 * 7. Zero regression when developer_gate_inventory is absent.
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

function element(extra = {}) {
  return {
    innerHTML: '', textContent: '', value: '', options: [], dataset: {},
    querySelector: () => null, ...extra,
  };
}

function context(payload = null) {
  const nodes = new Map();
  for (const id of [
    'fleet-body', 'fleet-summary', 'fleet-issues', 'fleet-filter', 'fleet-threshold',
    'fleet-machines-wrap', 'fleet-status', 'fleet-warnings', 'fleet-locations-wrap',
    'fleet-locations-status', 'fleet-models-wrap', 'fleet-models-status', 'fleet-jobs-wrap',
    'fleet-jobs-status', 'fleet-jobs-problems', 'fleet-tabs', 'fleet-search', 'fleet-location',
    'fleet-drops', 'fleet-threshold-note',
  ]) {
    nodes.set(id, element());
  }
  const ctx = vm.createContext({
    document: {
      getElementById: id => nodes.get(id),
      querySelector: () => null,
      activeElement: null,
      querySelectorAll: () => [],
    },
    window: {},
    esc,
    ago: () => 'just now',
    FLEET_STATES: {
      working: ['info', 'Working'],
      just_finished: ['ok', 'Just finished'],
      needs_attention: ['warn', 'Needs attention'],
      offline: ['bad', 'Offline'],
      ready: ['ok', 'Ready'],
    },
    state: { view: 'fleet' },
    nodes,
    api: async () => payload,
    Date,
    Number,
    Math,
    isFinite,
    setTimeout,
    clearTimeout,
  });
// Every question in the page is now an in-app dialog; the tests' own confirm/alert/prompt
// stubs are where each answer comes from.
vm.runInContext(`globalThis.uiConfirm = async (t) => (typeof confirm === 'function' ? confirm(t) : true);
globalThis.uiAlert = async (t) => { if (typeof alert === 'function') alert(t); };
globalThis.uiPrompt = async (t, v) => (typeof prompt === 'function' ? prompt(t, v) : v);`, ctx);

  vm.runInContext(between("let fleetFilter = 'all';", 'async function loadJobs()'), ctx);
  return ctx;
}

function sampleGateInventory(overrides = {}) {
  return {
    format: 'studio.developer-gate.inventory.v1',
    node_id: 'node-1301',
    controller_id: 'ctrl-main',
    observed_at: '2026-11-08T06:00:00Z',
    internal_physical_drive: {
      device_id: 'disk0',
      internal: true,
      physical: true,
      size_bytes: 512000000000,
      source: 'diskutil-info-plist',
      workspace_backing_device_id: 'disk0s2',
      backing_source: 'diskutil-apfs-list-plist',
      observed_at: '2026-11-08T06:00:00Z',
      reason: null,
    },
    free_bytes: 50000000000,
    toolchains: [
      {
        id: 'node',
        pin: {
          id: 'node',
          version: '26.8.2',
          manifest_sha256: 'b'.repeat(64),
        },
        available: true,
        observed_at: '2026-11-08T06:00:00Z',
        installed_bytes: 104857600,
        provenance: 'owned-toolchain-readback',
        reason: null,
      },
      {
        id: 'python',
        pin: {
          id: 'python',
          version: '3.15.0',
          manifest_sha256: 'c'.repeat(64),
        },
        available: true,
        observed_at: '2026-11-08T06:00:00Z',
        installed_bytes: 209715200,
        provenance: 'owned-toolchain-readback',
        reason: null,
      },
      {
        id: 'rust',
        pin: null,
        available: false,
        observed_at: '2026-11-08T06:00:00Z',
        installed_bytes: null,
        provenance: 'owned-toolchain-readback',
        reason: {
          code: 'not_installed',
          detail: 'cargo binary not found on PATH',
        },
      },
    ],
    developer_gate_space: {
      observed_at: '2026-11-08T06:00:00Z',
      workspace_bytes: 1048576,
      result_spool_bytes: 2097152,
      shared_cache_bytes: 4194304,
      workspace_limit_bytes: 10737418240,
      result_spool_limit_bytes: 5368709120,
      shared_cache_limit_bytes: 21474836480,
      cleanup_state: 'awaiting_ack',
      reason: {
        code: 'gate_passed',
        detail: 'awaiting caller ACK before disposable scratch cleanup',
      },
    },
    ...overrides,
  };
}

function sampleMachine(overrides = {}) {
  return {
    machine: '1301',
    label: '1301',
    location: 'Blue Room',
    location_id: 'L1',
    network: 'A',
    state: 'ready',
    reachable: true,
    chip: 'Apple M4',
    memory_gb: 32,
    available_gb: 18.4,
    disk_free_gb: 61,
    disk_total_gb: 460,
    hub_version: '0.6.126',
    version_status: 'latest',
    history: {
      observed: true, online: true, drops_day: 0, drops_week: 0,
      offline_seconds_day: 0, offline_seconds_week: 0, days: [],
    },
    causes: [{ code: 'fine', text: 'Nothing wrong reported' }],
    developer_gate_inventory: null,
    ...overrides,
  };
}

function makePayload(machines = []) {
  return {
    summary: {
      machines: machines.length,
      machines_online: machines.filter(m => m.reachable !== false).length,
      locations: 1,
      locations_up: 1,
      pulse: { working: 0 },
    },
    gateway_queue: { waiting: 0, running: 0 },
    locations: [{
      id: 'L1', label: 'Blue Room', reachable: true, role: 'member',
      version: '0.6.126', version_status: 'latest',
      node_summary: { total: machines.length, reachable: machines.length },
      models_publishable: 3, models_discovered: 4,
    }],
    machines,
    running: [],
    issues: [],
    generated_at: Math.floor(Date.now() / 1000),
    fleet_health: { flapping_drops_day: 4, default: 4, min: 1, max: 100, window_days: 7 },
  };
}

test('fleetGateBytes distinguishes null/undefined from numeric 0', () => {
  const ctx = context();
  assert.equal(ctx.fleetGateBytes(null), '—');
  assert.equal(ctx.fleetGateBytes(undefined), '—');
  assert.equal(ctx.fleetGateBytes(NaN), '—');
  assert.equal(ctx.fleetGateBytes(Infinity), '—');
  assert.equal(ctx.fleetGateBytes(0), '0 bytes');
  assert.equal(ctx.fleetGateBytes(1048576), '1.0 MB');
  assert.equal(ctx.fleetGateBytes(10737418240), '10.7 GB');
});

test('fleetGateToolsSummary renders actual versions and unavailable flags', () => {
  const ctx = context();
  const inv = sampleGateInventory();
  const summary = ctx.fleetGateToolsSummary(inv);
  assert.equal(summary, 'node 26.8.2 · python 3.15.0 · rust (unavailable)');

  const tooltip = ctx.fleetGateToolsTooltip(inv);
  assert.match(tooltip, /• node \(v26\.8\.2, available, 104\.9 MB\)/);
  assert.match(tooltip, /• python \(v3\.15\.0, available, 209\.7 MB\)/);
  assert.match(tooltip, /• rust \(unversioned, unavailable\) — not_installed: cargo binary not found on PATH/);
});

test('fleetGateToolsSummary returns empty string when toolchains absent', () => {
  const ctx = context();
  assert.equal(ctx.fleetGateToolsSummary(null), '');
  assert.equal(ctx.fleetGateToolsSummary({}), '');
  assert.equal(ctx.fleetGateToolsSummary({ toolchains: [] }), '');
  assert.equal(ctx.fleetGateToolsTooltip(null), '');
});

test('fleetGateSpaceSummary renders workspace, spool, and cache with caps', () => {
  const ctx = context();
  const inv = sampleGateInventory();
  const summary = ctx.fleetGateSpaceSummary(inv);
  assert.equal(summary, 'Workspace: 1.0 MB / 10.7 GB · Spool: 2.1 MB / 5.4 GB · Cache: 4.2 MB / 21.5 GB');

  const tooltip = ctx.fleetGateStorageTooltip(inv);
  assert.match(tooltip, /Physical drive: disk0 \(internal, physical, 512\.0 GB via diskutil-info-plist\)/);
  assert.match(tooltip, /Workspace backing device: disk0s2 via diskutil-apfs-list-plist/);
  assert.match(tooltip, /Gate filesystem free: 50\.0 GB/);
  assert.match(tooltip, /Workspace space: 1\.0 MB \(limit 10\.7 GB\)/);
  assert.match(tooltip, /Result spool: 2\.1 MB \(limit 5\.4 GB\)/);
  assert.match(tooltip, /Shared cache: 4\.2 MB \(limit 21\.5 GB\)/);
  assert.match(tooltip, /Cleanup state: awaiting_ack/);
  assert.match(tooltip, /Cleanup reason: gate_passed — awaiting caller ACK before disposable scratch cleanup/);
});

test('fleetGateCleanupSummary renders cleanup state, reason, and time', () => {
  const ctx = context();
  const inv = sampleGateInventory();
  const summary = ctx.fleetGateCleanupSummary(inv);
  assert.equal(
    summary,
    'Cleanup: awaiting_ack (gate_passed: awaiting caller ACK before disposable scratch cleanup) at 2026-11-08T06:00:00Z',
  );
});

test('fleetMachineRow embeds gate tools under name and gate space/cleanup under disk', async () => {
  const inv = sampleGateInventory();
  const m = sampleMachine({ developer_gate_inventory: inv });
  const ctx = context(makePayload([m]));
  await ctx.loadFleet();

  const html = ctx.nodes.get('fleet-machines-wrap').innerHTML;

  // Machine cell has Gate tools line
  assert.match(html, /Gate tools: node 26\.8\.2 · python 3\.15\.0 · rust \(unavailable\)/);

  // Disk cell has workspace / spool / cache line
  assert.match(html, /Workspace: 1\.0 MB \/ 10\.7 GB · Spool: 2\.1 MB \/ 5\.4 GB · Cache: 4\.2 MB \/ 21\.5 GB/);

  // Disk cell has cleanup line
  assert.match(html, /Cleanup: awaiting_ack \(gate_passed: awaiting caller ACK before disposable scratch cleanup\)/);

  // Drive and toolchain tooltips are in the HTML attributes
  assert.match(html, /Physical drive: disk0/);
  assert.match(html, /Workspace backing device: disk0s2/);
  assert.match(html, /• node \(v26\.8\.2, available/);
});

test('fleetMac detail card displays gate tools, space, and cleanup facts', () => {
  const inv = sampleGateInventory();
  const m = sampleMachine({ developer_gate_inventory: inv });
  const ctx = context(makePayload([m]));
  const cardHtml = ctx.fleetMac(m);

  assert.match(cardHtml, /Gate tools: node 26\.8\.2 · python 3\.15\.0 · rust \(unavailable\)/);
  assert.match(cardHtml, /Workspace: 1\.0 MB \/ 10\.7 GB · Spool: 2\.1 MB \/ 5\.4 GB · Cache: 4\.2 MB \/ 21\.5 GB/);
  assert.match(cardHtml, /Cleanup: awaiting_ack/);
});

test('search matches developer gate toolchain versions and cleanup states', async () => {
  const m1 = sampleMachine({
    machine: '1301',
    label: '1301',
    developer_gate_inventory: sampleGateInventory(),
  });
  const m2 = sampleMachine({
    machine: '1302',
    label: '1302',
    developer_gate_inventory: null,
  });
  const ctx = context(makePayload([m1, m2]));
  await ctx.loadFleet();

  // Exercise the actual Machines tab rather than the default Locations tab.
  vm.runInContext("fleetTab = 'machines';", ctx);
  // Search by toolchain version
  vm.runInContext("fleetQuery = '26.8.2';", ctx);
  ctx.renderFleet(makePayload([m1, m2]));
  let html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.match(html, />1301</);
  assert.doesNotMatch(html, />1302</);

  // Search by cleanup state
  vm.runInContext("fleetQuery = 'awaiting_ack';", ctx);
  ctx.renderFleet(makePayload([m1, m2]));
  html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.match(html, />1301</);
  assert.doesNotMatch(html, />1302</);

  // Search by reason code
  vm.runInContext("fleetQuery = 'gate_passed';", ctx);
  ctx.renderFleet(makePayload([m1, m2]));
  html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.match(html, />1301</);
  assert.doesNotMatch(html, />1302</);

  // Clear search shows both
  vm.runInContext("fleetQuery = '';", ctx);
  ctx.renderFleet(makePayload([m1, m2]));
  html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.match(html, />1301</);
  assert.match(html, />1302</);
});

test('clean regression test: machine without developer_gate_inventory renders standard view', async () => {
  const m = sampleMachine({ developer_gate_inventory: null });
  const ctx = context(makePayload([m]));
  await ctx.loadFleet();

  const html = ctx.nodes.get('fleet-machines-wrap').innerHTML;
  assert.doesNotMatch(html, /Gate tools:/);
  assert.doesNotMatch(html, /Workspace:/);
  assert.doesNotMatch(html, /Cleanup:/);
  assert.match(html, /61\.0 GB free/);
});
