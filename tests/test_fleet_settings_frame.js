// Fleet and Settings, in the same frame as the Providers pass: one status line, one primary
// action, filters in an overflow with counts, card detail on demand, and no app-only dead page.
// Headless: the page source and the real functions, no window.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');

function between(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  if (first < 0 || last < 0) throw new Error(`boundary changed: ${start}`);
  return source.slice(first, last);
}

function element(value = '') {
  return { value, innerHTML: '', textContent: '', hidden: false, className: '', style: {}, files: [] };
}

test('Fleet has one primary action and no duplicate Run test door', () => {
  const fleet = between('<section id="view-fleet"', '<!-- Groups: the one list');
  assert.match(fleet, /class="primary sm" onclick="fleetRefresh\(this\)">Refresh/);
  assert.doesNotMatch(fleet, /openNav\('developer-tests'\)/, 'Run test is a sidebar row; a second door in Fleet is a duplicate');
  assert.match(source, /\{ view: 'developer-tests', label: 'Run test'/);
});

test('the Fleet toolbar is one search plus a Filters popover with a count', () => {
  const controls = between('<div class="row" id="fleet-controls"', '<svg');
  assert.match(controls, /id="fleet-search"[^>]*type="search"|type="search"[^>]*id="fleet-search"/);
  assert.match(controls, /id="fleet-filters-btn"/);
  const popover = controls.slice(controls.indexOf('id="fleet-filters"'));
  for (const id of ['fleet-filter', 'fleet-sort', 'fleet-location']) {
    assert.match(popover, new RegExp(`id="${id}"`), `${id} belongs in the Filters popover`);
  }
  // Counts: defaults show nothing; two changed filters show "2".
  const nodes = new Map([['fleet-filter-count', element()]]);
  const ctx = vm.createContext({
    document: { getElementById: id => nodes.get(id) || null },
    FLEET_FILTERS: [['all', 'All machines'], ['offline', 'Offline now']],
    FLEET_SORTS: [['issues', 'Issues (worst first)'], ['drops', 'Most drops']],
  });
  vm.runInContext('let fleetFilter = "all"; let fleetSort = "issues"; let fleetLocation = "all";', ctx);
  vm.runInContext(between('function updateFleetFilterCount()', 'function fleetSetFilter('), ctx);
  vm.runInContext('updateFleetFilterCount()', ctx);
  assert.equal(nodes.get('fleet-filter-count').hidden, true);
  vm.runInContext('fleetFilter = "offline"; fleetLocation = "ctrl-1"; updateFleetFilterCount()', ctx);
  assert.equal(nodes.get('fleet-filter-count').hidden, false);
  assert.equal(nodes.get('fleet-filter-count').textContent, '2');
});

test('a Fleet card hides its history and device facts behind Details, kept across redraws', () => {
  const card = between('const fleetOpenDetails = new Set();', 'function fleetCard(item)');
  assert.match(card, /data-fleet-key="\$\{esc\(detailKey\)\}"/);
  assert.match(card, /fleetOpenDetails\.has\(detailKey\)/);
  assert.match(card, /class="fleet-mac-detail"/);
  assert.match(card, /class="row hint" style="gap:8px;align-items:center">\$\{fleetStrip\(history\)\}/,
    'the 7-day strip and history line moved into the detail');
  // A member and a site both get one; the cause stays on the card.
  assert.equal((card.match(/class="fleet-mac-detail"/g) || []).length, 2);
  assert.match(card, /fleetCause\(\{ kind: 'machine', row: m \}\)/);
  assert.match(card, /fleetCause\(\{ kind: 'site', row: L \}\)/);
});

test('Settings is a real read-only page, not an app-only note', () => {
  assert.doesNotMatch(source, /settings: 'Settings is in the Gateway app\.'/, 'the stub must be gone');
  assert.match(source, /<section id="view-settings"/);
  assert.match(source, /What this Gateway is running\. Appearance, startup, keys and backups are changed in the/);
  assert.match(source, /id="settings-status"/);
  assert.match(source, /id="settings-facts"/);
  assert.match(source, /Copy this gateway's address/);
  assert.match(source, /loadSettingsPage\(\)/);
  assert.match(source, /id="view-settings"/);
  // It is reachable: the section is in the view list and the nav row is not app-only any more.
  assert.match(source, /'whatsnew', 'settings', 'developer-tests'/);
  assert.doesNotMatch(source, /view: 'settings', label: 'Settings', icon: 'settings', app: true/);
});

test('the Settings page answers with the gateway’s own facts, and an error offers Retry', async () => {
  const nodes = new Map([['settings-facts', element()], ['settings-status', element()]]);
  const ctx = vm.createContext({
    document: { getElementById: id => nodes.get(id) || null },
    esc: value => String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;'),
    window: { location: { origin: 'http://127.0.0.1:8787' } },
    api: async () => ({
      version: '0.2.255', computer_name: '1301', uptime_seconds: 7200,
      hub: { running: true, base_url: 'http://127.0.0.1:8787' },
      providers: { enabled: 15, total: 34 }, models: { publishable_models: 458, withheld_models: 4 },
    }),
  });
  vm.runInContext(between('async function loadSettingsPage()', 'async function loadWhatsNew()'), ctx);
  await ctx.loadSettingsPage();
  const facts = nodes.get('settings-facts').innerHTML;
  for (const wanted of ['0.2.255', '1301', 'http://127.0.0.1:8787', 'running at http://127.0.0.1:8787',
    '15 of 34 on', '458 offered · 4 withheld', '2 h 0 min']) {
    assert.match(facts, new RegExp(wanted.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  }
  assert.match(nodes.get('settings-status').innerHTML, /Gateway 0\.2\.255 on 1301 · Studio Hub running/);

  const failing = vm.createContext({
    document: { getElementById: id => nodes.get(id) || null },
    esc: value => String(value ?? ''),
    window: { location: { origin: '' } },
    api: async () => { const error = new Error('offline'); error.status = 0; throw error; },
  });
  vm.runInContext(between('async function loadSettingsPage()', 'async function loadWhatsNew()'), failing);
  await failing.loadSettingsPage();
  assert.match(nodes.get('settings-status').innerHTML, /Could not read this gateway — offline/);
  assert.match(nodes.get('settings-status').innerHTML, /Retry/);
});
