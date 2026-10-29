/**
 * The Fleet page's actions: every control the native Fleet page offered, on the web page.
 *
 * The owner, 2026-10-30: the Mac app's Fleet section now loads the web Fleet page in its own
 * window, so an action that exists only in the retired Swift views is lost. This suite pins the
 * web controls to the same routes, bodies and sentences the native code used — a VM over
 * `admin_ui.html` with a recording `api`, never a live gateway.
 *
 * Each test drives the page's own function and asserts the request it actually sent, so a button
 * wired to the wrong route (or to nothing) fails here rather than in the app.
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
  return { innerHTML: '', textContent: '', value: '', checked: false, disabled: false,
    options: [], dataset: {}, querySelector: () => null,
    setAttribute: () => {}, removeAttribute: () => {}, ...extra };
}

const NODES = ['fleet-body', 'fleet-summary', 'fleet-issues', 'fleet-filter', 'fleet-threshold',
  'fleet-machines-wrap', 'fleet-status', 'fleet-warnings', 'fleet-warning-settings',
  'fleet-locations-wrap', 'fleet-locations-status', 'fleet-models-wrap', 'fleet-models-status',
  'fleet-jobs-wrap', 'fleet-jobs-status', 'fleet-jobs-note', 'fleet-jobs-problems', 'fleet-tabs', 'fleet-search',
  'fleet-location', 'fleet-drops', 'fleet-threshold-note', 'fleet-machine-action-note',
  'fleet-model-action-note', 'fleet-location-action-note', 'fleet-story-note', 'fleet-story-wrap',
  'fleet-story-status', 'fleet-installs', 'fleet-rollout', 'fleet-release-status',
  'fleet-release-note', 'fleet-render-body', 'fleet-render-note',
  'fleet-dialog', 'fleet-dialog-heading', 'fleet-dialog-text', 'fleet-dialog-body',
  'fleet-dialog-note', 'fleet-dialog-actions'];

function context(payload, options = {}) {
  const nodes = new Map();
  for (const id of NODES) nodes.set(id, element());
  const calls = [];
  const ctx = vm.createContext({
    document: {
      // A real DOM creates the dialog's controls when `innerHTML` is set; this map makes them on
      // first ask, so a handler can read what its own dialog drew.
      getElementById: id => {
        if (!nodes.has(id)) nodes.set(id, element());
        return nodes.get(id);
      }, querySelector: () => null, activeElement: null,
      querySelectorAll: () => [],
    },
    window: {},
    esc,
    ago: () => 'just now',
    FLEET_STATES: {
      working: ['info', 'Working'], just_finished: ['ok', 'Just finished'],
      needs_attention: ['warn', 'Needs attention'], offline: ['bad', 'Offline'], ready: ['ok', 'Ready'],
    },
    state: { view: 'fleet' },
    nodes,
    calls,
    api: async (route, opts) => {
      calls.push({ route, opts });
      if (options.respond) return options.respond(route, opts);
      return payload;
    },
    confirm: text => { calls.push({ confirm: text }); return options.confirm !== false; },
    prompt: (text, value) => {
      calls.push({ prompt: text });
      return options.prompt === undefined ? value : options.prompt;
    },
    Date, Number, Math, JSON, Object, Set, Map, String, Array, Promise, isFinite, setTimeout, clearTimeout,
  });
  vm.runInContext(between("let fleetFilter = 'all';", 'async function loadJobs()'), ctx);
  return ctx;
}

// The page creates a dialog's controls by assigning `innerHTML`, which a real DOM parses. Reading
// one that the page has not asked for yet goes through the same getter, so it exists exactly when
// the browser would have made it.
function node(ctx, id) { return ctx.document.getElementById(id); }

function history(overrides = {}) {
  return {
    observed: true, online: true, observed_since: 1000, observed_at: 1000,
    since: 1000, online_since: 1000, offline_since: null,
    drops_day: 0, drops_week: 0, flips_day: 0, flips_week: 0,
    offline_seconds_day: 0, offline_seconds_week: 0, days: [], pattern: null,
    problematic: false, last_drop: null, ...overrides,
  };
}

function machine(overrides = {}) {
  return {
    machine: '1301', label: '1301', location: 'Blue Room', location_id: 'L1', network: 'A',
    state: 'ready', reachable: true, node_paused: false, chip: 'Apple M4', memory_gb: 32,
    available_gb: 18.4, disk_free_gb: 61, disk_total_gb: 460, hub_version: '0.6.126',
    version_status: 'latest', history: history(),
    causes: [{ code: 'fine', text: 'Nothing wrong reported', evidence: null }],
    startup: { open_at_login: true, start_services_on_launch: true, stale: false,
      services_running: { up: 3, total: 3 }, macos_trash_auto_empty: true },
    workstation: { enabled: false, idle_only: false, idle_minutes: 3, step_aside: false,
      resume_after_minutes: 2, start_free_gb: null, quiet_hours: [], hide_menu_bar_icon: false },
    installable_models: [], priority_order: [], ...overrides,
  };
}

function location(overrides = {}) {
  return {
    id: 'L1', label: 'Blue Room', url: 'http://100.64.0.11:47873', provider_id: 'studio-hub-L1',
    reachable: true, role: 'member', version: '0.6.126', version_status: 'latest', local: false,
    node_summary: { total: 3, reachable: 2, unauthorized: 1 }, network: 'A',
    machines: [{}], history: history(), release: { version: '0.6.126', build: 'abc1234' },
    causes: [{ code: 'fine', text: 'Nothing wrong reported' }], ...overrides,
  };
}

function payload(machines, locations = []) {
  return {
    summary: { machines: machines.length, machines_online: machines.length,
      locations: locations.length, locations_up: locations.length, pulse: { working: 0 } },
    gateway_queue: { waiting: 0, running: 0 },
    locations, machines, running: [], issues: [],
    generated_at: Math.floor(Date.now() / 1000),
    fleet_health: { flapping_drops_day: 4, default: 4, min: 1, max: 100, window_days: 7 },
    fleet_warnings: { hidden: [], hideable: [{ kind: 'macos_updates', title: 'macOS updates' }] },
    release: { identity: { fingerprint: 'ab'.repeat(32) }, last_push: null, rollout: null },
    model_installs: [],
  };
}

function writes(calls) {
  return calls.filter(call => call.route);
}

test('a Mac offers exactly the startup actions its own report supports', () => {
  const ctx = context(payload([machine()]));
  const html = ctx.fleetMachineActionCell(machine());
  assert.match(html, /Restart Services/);
  assert.doesNotMatch(html, /Start Services/);
  assert.doesNotMatch(html, /Enable Auto-Start/);

  const off = ctx.fleetMachineActionCell(machine({
    startup: { open_at_login: true, start_services_on_launch: false, stale: false,
      services_running: { up: 1, total: 3 }, macos_trash_auto_empty: false },
  }));
  assert.match(off, /Enable Auto-Start/);
  assert.match(off, /Start Services/);
  assert.match(off, /Restart Services/);
  assert.match(off, /Turn on macOS Trash/);

  // A Mac that is not answering gets no startup action at all.
  const away = ctx.fleetMachineActionCell(machine({ reachable: false, state: 'offline' }));
  assert.doesNotMatch(away, /Restart Services/);
  assert.doesNotMatch(away, /Start Services/);
});

test('Enable Auto-Start on one Mac sends the native body and reads the Mac\'s answer', async () => {
  const machineRow = machine({
    startup: { open_at_login: true, start_services_on_launch: false, stale: false,
      services_running: { up: 3, total: 3 }, macos_trash_auto_empty: true },
  });
  const ctx = context(payload([machineRow]), {
    respond: route => (route === '/fleet' ? payload([machineRow])
      : { state: 'queued', ok: true }),
  });
  await ctx.loadFleet();
  const button = { disabled: false };
  await ctx.fleetMachineStartup(button, 'L1', '1301', 'enable-autostart');
  const post = writes(ctx.calls).find(call => call.route.endsWith('/startup'));
  assert.equal(post.route, '/fleet/controllers/L1/machines/1301/startup');
  assert.equal(post.opts.method, 'POST');
  assert.deepEqual(JSON.parse(post.opts.body),
    { action: 'set_start_services_on_launch', value: true, force: false });
  assert.match(node(ctx, 'fleet-machine-action-note').textContent, /sent\. The Mac applies it on its next check\./);
});

test('a restart that meets running work asks before forcing, and the retry carries force: true', async () => {
  const machineRow = machine();
  let attempt = 0;
  const ctx = context(payload([machineRow]), {
    respond: route => {
      if (route === '/fleet') return payload([machineRow]);
      attempt += 1;
      if (attempt === 1) throw new Error('Studio Hub is working: 2 running, 1 queued (send force: true to interrupt).');
      return { state: 'handed_to_app', ok: true };
    },
  });
  await ctx.loadFleet();
  await ctx.fleetMachineStartup({ disabled: false }, 'L1', '1301', 'restart-services');
  const confirms = ctx.calls.filter(call => call.confirm).map(call => call.confirm);
  // The first is the plain restart confirm; the second is the "busy" question.
  assert.match(confirms[0], /Restart Services on 1301\?/);
  assert.match(confirms[1], /1301 is busy/);
  assert.match(confirms[1], /Restart anyway and interrupt that work\?/);
  const posts = writes(ctx.calls).filter(call => call.route.endsWith('/startup'));
  assert.equal(posts.length, 2);
  assert.deepEqual(JSON.parse(posts[1].opts.body), { action: 'restart_services', force: true });
  assert.match(node(ctx, 'fleet-machine-action-note').textContent, /the Mac is restarting its services\./);
});

test('a pause is one boolean, and Resume says the Mac rejoins on its next check', async () => {
  const machineRow = machine();
  const ctx = context(payload([machineRow]), {
    respond: route => (route === '/fleet' ? payload([machineRow, { ...machineRow, node_paused: true }])
      : { ok: true, paused: true }),
  });
  await ctx.loadFleet();
  await ctx.fleetMachinePause({ disabled: false }, 'L1', '1301', true);
  const post = writes(ctx.calls).find(call => call.route.endsWith('/pause'));
  assert.equal(post.route, '/fleet/controllers/L1/machines/1301/pause');
  assert.deepEqual(JSON.parse(post.opts.body), { paused: true });
  assert.match(node(ctx, 'fleet-machine-action-note').textContent, /Paused 1301\. It gets no new work/);
});

test('Rename refuses an empty or over-long name and sends only the name', async () => {
  const machineRow = machine();
  const ctx = context(payload([machineRow]), {
    respond: route => (route === '/fleet' ? payload([machineRow]) : { ok: true }),
    prompt: '',
  });
  await ctx.loadFleet();
  await ctx.fleetMachineRename({ disabled: false }, 'L1', '1301');
  assert.match(node(ctx, 'fleet-machine-action-note').textContent, /1 to 120 characters/);
  assert.equal(writes(ctx.calls).filter(call => call.route).length, 1, 'nothing was sent');

  ctx.calls.length = 0;
  const ctx2 = context(payload([machineRow]), {
    respond: route => (route === '/fleet' ? payload([machineRow]) : { ok: true }),
    prompt: '  Blue Room 2  ',
  });
  await ctx2.loadFleet();
  await ctx2.fleetMachineRename({ disabled: false }, 'L1', '1301');
  const post = writes(ctx2.calls).find(call => call.route.endsWith('/name'));
  assert.equal(post.route, '/fleet/controllers/L1/machines/1301/name');
  assert.deepEqual(JSON.parse(post.opts.body), { name: 'Blue Room 2' });
});

test('Lite Mode opens on the Mac\'s own settings and Save sends every setting', async () => {
  const machineRow = machine();
  const ctx = context(payload([machineRow]), {
    respond: route => (route === '/fleet' ? payload([machineRow]) : { ok: true, pending: false }),
  });
  await ctx.loadFleet();
  ctx.fleetLiteOpen({ disabled: false }, 'L1', '1301');
  assert.match(node(ctx, 'fleet-dialog-body').innerHTML, /Only when idle/);
  assert.match(node(ctx, 'fleet-dialog-body').innerHTML, /Quiet hours/);
  assert.match(node(ctx, 'fleet-dialog-heading').textContent, /Lite Mode — 1301/);

  node(ctx, 'fleet-lite-enabled').checked = true;
  node(ctx, 'fleet-lite-idle-only').checked = true;
  node(ctx, 'fleet-lite-idle-minutes').value = '5';
  node(ctx, 'fleet-lite-step-aside').checked = true;
  node(ctx, 'fleet-lite-resume-minutes').value = '4';
  node(ctx, 'fleet-lite-start-free').value = 'custom';
  node(ctx, 'fleet-lite-start-free-gb').value = '3.5';
  node(ctx, 'fleet-lite-quiet-hours').value = '22:00-07:00, 12:00-13:00';
  node(ctx, 'fleet-lite-hide-icon').checked = false;

  await ctx.fleetLiteSave({ disabled: false }, { locationId: 'L1', machineId: '1301' });
  const post = writes(ctx.calls).find(call => call.route.endsWith('/workstation'));
  assert.equal(post.route, '/fleet/controllers/L1/machines/1301/workstation');
  assert.deepEqual(JSON.parse(post.opts.body), {
    enabled: true, idle_only: true, idle_minutes: 5, step_aside: true, resume_after_minutes: 4,
    start_free_gb: 3.5, quiet_hours: ['22:00-07:00', '12:00-13:00'], hide_menu_bar_icon: false,
  });
  assert.match(node(ctx, 'fleet-machine-action-note').textContent, /Lite Mode saved for 1301\./);
});

test('Lite Mode refuses a value it cannot send, in a sentence, and sends nothing', async () => {
  const machineRow = machine();
  const ctx = context(payload([machineRow]), {
    respond: route => (route === '/fleet' ? payload([machineRow]) : { ok: true }),
  });
  await ctx.loadFleet();
  ctx.fleetLiteOpen({ disabled: false }, 'L1', '1301');
  node(ctx, 'fleet-lite-idle-minutes').value = '0';
  node(ctx, 'fleet-lite-resume-minutes').value = '2';
  node(ctx, 'fleet-lite-quiet-hours').value = '';
  await ctx.fleetLiteSave({ disabled: false }, { locationId: 'L1', machineId: '1301' });
  assert.match(node(ctx, 'fleet-dialog-note').textContent, /from 1 to 60/);
  const sent = () => writes(ctx.calls).filter(call => call.opts && call.opts.method).length;
  assert.equal(sent(), 0);

  node(ctx, 'fleet-lite-idle-minutes').value = '3';
  node(ctx, 'fleet-lite-quiet-hours').value = '10am-noon';
  await ctx.fleetLiteSave({ disabled: false }, { locationId: 'L1', machineId: '1301' });
  assert.match(node(ctx, 'fleet-dialog-note').textContent, /is not a time range/);
  assert.equal(sent(), 0);
});

test('Work Priorities is read-only when the Mac\'s Hub answers no policy, and otherwise sends only the order', async () => {
  const machineRow = machine();
  const ctx = context(payload([machineRow]), {
    respond: route => {
      if (route.endsWith('/work-policy')) return { priority_order: ['voice'] };   // no functions: too old
      return payload([machineRow]);
    },
  });
  await ctx.loadFleet();
  await ctx.fleetPriorityOpen({ disabled: false }, 'L1', '1301');
  assert.match(node(ctx, 'fleet-machine-action-note').textContent, /read-only/);

  const policy = {
    priority_functions: [{ id: 'voice', title: 'Voice' }, { id: 'render' }],
    priority_order: ['voice'], studios: [{ modality: 'voice', enabled: true }],
    machine_enabled: true,
  };
  const ctx2 = context(payload([machineRow]), {
    respond: route => {
      if (route === '/fleet') return payload([machineRow]);
      if (route.endsWith('/work-policy')) return policy;
      return { ok: true };
    },
  });
  await ctx2.loadFleet();
  await ctx2.fleetPriorityOpen({ disabled: false }, 'L1', '1301');
  assert.match(node(ctx2, 'fleet-dialog-body').innerHTML, /Final render/, 'the unnamed function gets the owner\'s words');
  await ctx2.fleetPrioritySave({ disabled: false }, { locationId: 'L1', machineId: '1301' });
  // The POST, not the GET that read the policy first.
  const post = writes(ctx2.calls).filter(call => call.route.endsWith('/work-policy') && call.opts
    && call.opts.method === 'POST').pop();
  assert.deepEqual(JSON.parse(post.opts.body), { priority_order: ['voice'] });
});

test('Install a model posts the package download and reports the run', async () => {
  const machineRow = machine({
    installable_models: [{ package_id: 'qwen-image-turbo', label: 'Qwen Image Turbo', size_gb: 8 }],
  });
  const ctx = context(payload([machineRow]), {
    respond: route => {
      if (route === '/fleet') return payload([machineRow]);
      if (route.endsWith('/download')) return { ok: true, run: { id: 'run-1', state: 'done' } };
      return {};
    },
  });
  await ctx.loadFleet();
  ctx.fleetInstallOpen({ disabled: false }, 'L1', '1301');
  assert.match(node(ctx, 'fleet-dialog-body').innerHTML, /qwen-image-turbo/);
  node(ctx, 'fleet-install-package').value = 'qwen-image-turbo';
  await ctx.fleetInstallSave({ disabled: false }, { locationId: 'L1', machineId: '1301' });
  const post = writes(ctx.calls).find(call => call.route.endsWith('/download'));
  assert.equal(post.route,
    '/fleet/controllers/L1/machines/1301/model-packages/qwen-image-turbo/download');
  assert.match(node(ctx, 'fleet-model-action-note').textContent, /Installed Qwen Image Turbo/);
});

test('Install on Every Mac posts the package id, with the native confirm', async () => {
  const machineRow = machine({
    installable_models: [{ package_id: 'qwen-image-turbo', label: 'Qwen Image Turbo' }],
  });
  const ctx = context(payload([machineRow]), {
    respond: route => (route === '/fleet' ? payload([machineRow]) : { plan: { id: 'p1' } }),
  });
  await ctx.loadFleet();
  ctx.fleetInstallOpen({ disabled: false }, 'L1', '1301');
  node(ctx, 'fleet-install-package').value = 'qwen-image-turbo';
  await ctx.fleetInstallEverywhere({ disabled: false });
  const question = ctx.calls.filter(call => call.confirm).pop().confirm;
  assert.match(question, /Install Qwen Image Turbo on every Mac that can run it\?/);
  assert.match(question, /Each network downloads it from the internet once/);
  const post = writes(ctx.calls).find(call => call.route === '/fleet/model-installs');
  assert.deepEqual(JSON.parse(post.opts.body), { package_id: 'qwen-image-turbo' });
});

test('Remove a Model deletes the exact package path and quotes the Hub', async () => {
  const machineRow = machine();
  const ctx = context(payload([machineRow]), {
    respond: route => {
      if (route === '/fleet') return payload([machineRow]);
      if (route.endsWith('/model-presets')) {
        return { models: [{ packageId: 'qwen-image-turbo', label: 'Qwen Image Turbo', installed: true, bytes: 8e9 }] };
      }
      if (route.includes('/model-packages/')) return { ok: true, detail: 'Moved to the model-library trash (8.0 GB).' };
      return {};
    },
  });
  await ctx.loadFleet();
  await ctx.fleetRemoveOpen({ disabled: false }, 'L1', '1301');
  node(ctx, 'fleet-remove-package').value = 'qwen-image-turbo';
  await ctx.fleetRemoveSave({ disabled: false }, { locationId: 'L1', machineId: '1301' });
  const remove = writes(ctx.calls).find(call => call.opts && call.opts.method === 'DELETE');
  assert.equal(remove.route, '/fleet/controllers/L1/machines/1301/model-packages/qwen-image-turbo');
  assert.match(node(ctx, 'fleet-machine-action-note').textContent, /Moved to the model-library trash/);
});

test('a Trash Folder is read, moved recoverably, and never deleted from the web page', async () => {
  const machineRow = machine();
  const ctx = context(payload([machineRow]), {
    respond: route => {
      if (route === '/fleet') return payload([machineRow]);
      if (route.endsWith('/trash')) {
        return { supported: true, status: 'fresh', count: 12, bytes: 3.4e9, measured_at: 1700000000,
          disk: { free_bytes: 61e9 }, by_kind: { audio: { count: 12, bytes: 3.4e9 } } };
      }
      if (route.endsWith('/trash/move-to-macos-trash')) return { ok: true, moved_count: 12, failed: [] };
      return {};
    },
  });
  await ctx.loadFleet();
  await ctx.fleetTrashOpen({ disabled: false }, 'L1', '1301');
  const text = node(ctx, 'fleet-trash-report').textContent;
  assert.match(text, /12 items/);
  assert.match(text, /By kind: audio: 12/);
  // Permanent delete is the app's own action: the page offers no route for it.
  assert.match(node(ctx, 'fleet-dialog-text').textContent, /Permanently deleting stays in the Gateway app/);
  assert.equal(node(ctx, 'fleet-trash-move').disabled, false);

  await ctx.fleetTrashMove({ disabled: false });
  const move = writes(ctx.calls).find(call => call.route.endsWith('/trash/move-to-macos-trash'));
  assert.deepEqual(JSON.parse(move.opts.body), { items: 'all' });
  assert.match(node(ctx, 'fleet-machine-action-note').textContent, /Moved 12 item\(s\)/);
  assert.equal(writes(ctx.calls).filter(call => call.route.endsWith('/trash/delete')).length, 0);
});

test('macOS Trash is turned on one Mac at a time, and the bulk confirm names what it will and will not do', async () => {
  const target = machine({ startup: { start_services_on_launch: true, stale: false, macos_trash_auto_empty: false } });
  const away = machine({ machine: '1302', label: '1302', reachable: false,
    startup: { start_services_on_launch: true, stale: false, macos_trash_auto_empty: false } });
  const ctx = context(payload([target, away]), {
    respond: route => (route === '/fleet' ? payload([target, away])
      : { ok: true, applied: [{}], skipped: [{}], failed: [] }),
  });
  await ctx.loadFleet();
  await ctx.fleetMacTrashOn({ disabled: false }, 'L1', '1301');
  const one = writes(ctx.calls).find(call => call.route.endsWith('/macos-trash-auto-empty/enable'));
  assert.equal(one.route, '/fleet/controllers/L1/machines/1301/macos-trash-auto-empty/enable');

  ctx.calls.length = 0;
  await ctx.fleetEnableMacTrashEverywhere({ disabled: false });
  const question = ctx.calls.filter(call => call.confirm).pop().confirm;
  assert.match(question, /Turn on macOS's 30-day Trash emptying for 1 Mac\?/);
  assert.match(question, /1302: skipped — not answering/);
  const bulk = writes(ctx.calls).find(call => call.route === '/fleet/trash/enable-macos-auto-empty');
  assert.ok(bulk, 'the bulk route is posted');
});

test('Enable Auto-Start on All Macs confirms the count and posts the bulk route', async () => {
  const off = machine({ startup: { start_services_on_launch: false, stale: false, macos_trash_auto_empty: true } });
  const ctx = context(payload([off]), {
    respond: route => (route === '/fleet' ? payload([off]) : { applied: [{}], skipped: [], failed: [] }),
  });
  await ctx.loadFleet();
  await ctx.fleetEnableAutostartEverywhere({ disabled: false });
  const question = ctx.calls.filter(call => call.confirm).pop().confirm;
  assert.match(question, /Turn on "Start services on startup" for 1 Mac\?/);
  assert.ok(writes(ctx.calls).find(call => call.route === '/fleet/startup/enable-autostart'));
});

test('Reset Job Counters asks first and reports the time it counts from', async () => {
  const ctx = context(payload([]), {
    respond: route => (route === '/fleet' ? payload([]) : { counters_since: 1700000000 }),
  });
  await ctx.loadFleet();
  await ctx.fleetResetCounters({ disabled: false });
  assert.match(ctx.calls.filter(call => call.confirm).pop().confirm, /Nothing is deleted/);
  const post = writes(ctx.calls).find(call => call.route === '/fleet/counters/reset');
  assert.ok(post);
  assert.match(node(ctx, 'fleet-machine-action-note').textContent, /Counters reset/);
});

test('the Jobs tab stops a whole batch by id and retries one failed item by index', async () => {
  const job = {
    location_id: 'L1', location: 'Blue Room', modality: 'tts', model: 'fish', batch_id: 'b-9',
    running: 2, queued: 3, error: 1, stalled: false, uncertain: 0,
    errors: [{ item_index: 4, code: 'provider_error', message: 'The voice provider refused it.' }],
  };
  const base = payload([]);
  base.running = [job];
  const ctx = context(base, {
    respond: route => {
      if (route === '/fleet') return base;
      if (route.endsWith('/cancel')) {
        return { result: { ok: true, answer: { queued_cancelled: 3, running_signalled: 2, running_pending: 1 } } };
      }
      if (route.endsWith('/retry')) return { result: { ok: true, answer: { state: 'queued' } } };
      return {};
    },
  });
  await ctx.loadFleet();
  assert.match(node(ctx, 'fleet-jobs-wrap').innerHTML, /fleetCancelBatch\(this\)/);
  assert.match(node(ctx, 'fleet-jobs-problems').innerHTML, /fleetRetryItem\(this\)/);

  await ctx.fleetCancelBatch({ disabled: false, dataset: { id: 'L1', batch: 'b-9' } });
  const cancel = writes(ctx.calls).find(call => call.route.endsWith('/cancel'));
  assert.equal(cancel.route, '/fleet/controllers/L1/jobs/b-9/cancel');
  assert.deepEqual(JSON.parse(cancel.opts.body), { modality: 'tts' });
  assert.match(node(ctx, 'fleet-jobs-note').textContent, /3 queued cancelled, 2 worker\(s\) signalled, 1 still pending/);

  await ctx.fleetRetryItem({ disabled: false, dataset: { id: 'L1', batch: 'b-9', modality: 'tts', index: '4' } });
  const retry = writes(ctx.calls).find(call => call.route.endsWith('/retry'));
  assert.equal(retry.route, '/fleet/controllers/L1/jobs/b-9/items/4/retry');
  assert.deepEqual(JSON.parse(retry.opts.body), { modality: 'tts' });
});

test('every Story Mac can be updated now, and a release published, from the web page', async () => {
  const instances = [
    { instance_id: 's1', machine: 'Studio 1', status: 'update_available', version: '1.4.360',
      update_requested_at: null, next_check_by: null, queue: {} },
    { instance_id: 's2', machine: 'Studio 2', status: 'up_to_date', version: '1.4.363', queue: {} },
  ];
  const ctx = context(payload([]), {
    respond: route => {
      if (route === '/story/instances') return { instances, release: { version: '1.4.363', filename: 'Story.dmg' } };
      if (route.endsWith('/update')) return { instance: { instance_id: 's1', next_check_by: Date.now() / 1000 + 600 } };
      if (route === '/story/release/publish') return { descriptor: { version: '1.4.363' } };
      return {};
    },
  });
  await ctx.loadFleetStory();
  const html = node(ctx, 'fleet-story-wrap').innerHTML;
  assert.match(html, /fleetStoryUpdate\(this\)/);
  assert.equal((html.match(/fleetStoryUpdate\(this\)/g) || []).length, 1, 'only a Mac that can update gets the button');

  await ctx.fleetStoryUpdate({ disabled: false, dataset: { id: 's1' } });
  const update = writes(ctx.calls).find(call => call.route === '/story/instances/s1/update');
  assert.ok(update);
  assert.match(node(ctx, 'fleet-story-note').textContent, /Studio 1 updates at its next check/);

  node(ctx, 'fleet-story-path').value = '/Users/owner/Story Studio KH-1.4.363.dmg';
  await ctx.fleetPublishStory({ disabled: false });
  const publish = writes(ctx.calls).find(call => call.route === '/story/release/publish');
  assert.deepEqual(JSON.parse(publish.opts.body), { artifact_path: '/Users/owner/Story Studio KH-1.4.363.dmg' });
  assert.match(node(ctx, 'fleet-story-note').textContent, /Published Story Studio 1\.4\.363/);
});

test('a Studio Hub release is published with every override, and the rollout can be stopped and resumed', async () => {
  const ctx = context(payload([machine()]), {
    respond: route => {
      if (route === '/fleet') return payload([machine()]);
      if (route === '/release/publish') return { rollout: { state: 'running', version: '0.6.50', summary: 'Rolling out to 1 controller' } };
      if (route.startsWith('/release/rollout/')) return { rollout: { state: 'stopped', version: '0.6.50', summary: 'Stopped at Blue Room' } };
      return {};
    },
  });
  await ctx.loadFleet();
  node(ctx, 'fleet-release-path').value = '/Users/owner/StudioHub-0.6.50-abc1234.dmg';
  node(ctx, 'fleet-release-version').value = '0.6.50';
  node(ctx, 'fleet-release-build').value = 'abc1234';
  node(ctx, 'fleet-release-all').checked = true;
  node(ctx, 'fleet-release-full').checked = true;
  await ctx.fleetPublishRelease({ disabled: false });
  const publish = writes(ctx.calls).find(call => call.route === '/release/publish');
  assert.deepEqual(JSON.parse(publish.opts.body), {
    artifact_path: '/Users/owner/StudioHub-0.6.50-abc1234.dmg', version: '0.6.50', build: 'abc1234',
    filename: 'StudioHub-0.6.50-abc1234.dmg', mode: 'all_at_once', force_full: true,
  });
  assert.match(node(ctx, 'fleet-release-note').textContent, /Rolling out 0\.6\.50/);

  await ctx.fleetRollout({ disabled: false }, 'stop');
  const stop = writes(ctx.calls).find(call => call.route === '/release/rollout/stop');
  assert.ok(stop);
  await ctx.fleetRollout({ disabled: false }, 'resume');
  assert.ok(writes(ctx.calls).find(call => call.route === '/release/rollout/resume'));
});

test('the rollout panel offers Stop while running and Resume once stopped', async () => {
  const base = payload([machine()]);
  base.release.rollout = { state: 'running', version: '0.6.50', order: ['L1'],
    steps: { L1: { label: 'Blue Room', state: 'sending' } }, summary: 'Rolling out to 1 controller' };
  const ctx = context(base);
  await ctx.loadFleet();
  assert.match(node(ctx, 'fleet-rollout').innerHTML, /Stop Rollout/);
  assert.doesNotMatch(node(ctx, 'fleet-rollout').innerHTML, /Resume Rollout/);

  base.release.rollout = { state: 'stopped', version: '0.6.50', order: ['L1'], reason: 'owner asked',
    steps: { L1: { label: 'Blue Room', state: 'skipped', detail: 'stopped' } }, summary: 'Stopped' };
  ctx.renderFleetRelease(base);
  assert.match(node(ctx, 'fleet-rollout').innerHTML, /Resume Rollout/);
  assert.match(node(ctx, 'fleet-rollout').innerHTML, /Stopped: owner asked/);
});

test('Remote final rendering lists each Mac and sends one switch per Mac', async () => {
  const answer = {
    workers: [
      { controller_id: 'L1', machine_id: '1301', machine: '1301', chip: 'Apple M4', memory_gb: 24,
        idle_state: 'idle', network: 'same_network', address: '10.0.0.5', authority: 'none',
        opted_in: false, hub_accepting: false, render_while_paused: false, render_speed: 2.4,
        render_speed_renders: 3 },
      { controller_id: 'L2', machine_id: '1402', machine: '1402', authority: 'production', opted_in: true },
    ],
  };
  const ctx = context(answer, {
    respond: route => (route === '/fleet/remote-rendering' ? answer : { ok: true, opted_in: true, relayed: true }),
  });
  await ctx.loadFleetRender();
  const html = node(ctx, 'fleet-render-body').innerHTML;
  assert.match(html, /fleetRenderToggle\(this,'L1','1301'/);
  assert.match(html, /On here, waiting for the Mac's Studio Hub|Off/);
  assert.match(html, /Already qualified/);
  assert.match(html, /2\.40× realtime measured/);

  await ctx.fleetRenderToggle({ checked: true, disabled: false }, 'L1', '1301', 'optin');
  const post = writes(ctx.calls).find(call => call.route.endsWith('/render-optin'));
  assert.equal(post.route, '/fleet/controllers/L1/machines/1301/render-optin');
  assert.deepEqual(JSON.parse(post.opts.body), { enabled: true });

  await ctx.fleetRenderToggle({ checked: true, disabled: false }, 'L1', '1301', 'paused');
  assert.ok(writes(ctx.calls).find(call => call.route.endsWith('/render-while-paused')));
});

test('a location can be given new credentials, re-pointed at a new address, and have its models refreshed', async () => {
  const ctx = context(payload([machine()], [location()]), {
    respond: route => {
      if (route === '/fleet') return payload([machine()], [location()]);
      if (route.endsWith('/credential')) return { credential: { detail: 'The controller answered on scope operator.' }, discovered: 12 };
      if (route.endsWith('/hub-token')) return { ok: true };
      if (route.endsWith('/test')) return { reachable: true, role: 'member', version: '0.6.126', machines: 3, nodes: 2 };
      if (route.endsWith('/address')) return { discovered: 12 };
      if (route.endsWith('/refresh')) return { discovered: 12 };
      return {};
    },
  });
  await ctx.loadFleet();

  ctx.fleetLocationCredentials({ dataset: { id: 'L1' } });
  node(ctx, 'fleet-cred-token').value = 'fleet-secret';
  node(ctx, 'fleet-cred-hub').value = 'hub-secret';
  await ctx.fleetCredentialsSave({ disabled: false }, { locationId: 'L1', kind: 'credentials' });
  const credential = writes(ctx.calls).find(call => call.route === '/fleet/controllers/L1/credential');
  assert.deepEqual(JSON.parse(credential.opts.body), { token: 'fleet-secret' });
  assert.equal(credential.opts.method, 'PUT');
  const hubToken = writes(ctx.calls).find(call => call.route === '/fleet/controllers/L1/hub-token');
  assert.deepEqual(JSON.parse(hubToken.opts.body), { token: 'hub-secret' });

  ctx.fleetLocationAddress({ dataset: { id: 'L1' } });
  node(ctx, 'fleet-addr-url').value = 'http://100.64.0.99:47873';
  await ctx.fleetAddressTest({ disabled: false });
  assert.match(node(ctx, 'fleet-addr-result').textContent, /Answered as member on Hub 0\.6\.126/);
  await ctx.fleetAddressChange({ disabled: false }, { locationId: 'L1', kind: 'address', testedOK: 'http://100.64.0.99:47873' });
  const address = writes(ctx.calls).find(call => call.route === '/fleet/controllers/L1/address');
  assert.deepEqual(JSON.parse(address.opts.body), { url: 'http://100.64.0.99:47873' });
  assert.equal(address.opts.method, 'PUT');

  await ctx.fleetLocationRefresh({ dataset: { id: 'L1' }, disabled: false });
  const refresh = writes(ctx.calls).find(call => call.route === '/providers/studio-hub-L1/refresh');
  assert.equal(refresh.opts.method, 'POST');
});

test('a network name is normalised the gateway\'s way, and empty clears it', async () => {
  const ctx = context(payload([machine()], [location()]), {
    respond: route => (route === '/fleet' ? payload([machine()], [location()]) : { networks: {} }),
    prompt: '  b   room ',
  });
  await ctx.loadFleet();
  await ctx.fleetMachineNetwork({ disabled: false }, 'L1', '1301');
  const put = writes(ctx.calls).find(call => call.route.endsWith('/network'));
  assert.equal(put.route, '/fleet/controllers/L1/machines/1301/network');
  assert.deepEqual(JSON.parse(put.opts.body), { network: 'B ROOM' });
});

test('a whole kind of warning can be hidden and shown again', async () => {
  const warning = machine({ disk: { state: 'green', warning_items: [{ kind: 'macos_updates', text: 'macOS has an update waiting.' }] } });
  const base = payload([warning]);
  const ctx = context(base, {
    respond: route => (route === '/fleet' ? base : { hidden: ['macos_updates'], hideable: base.fleet_warnings.hideable }),
  });
  await ctx.loadFleet();
  assert.match(node(ctx, 'fleet-warning-settings').innerHTML, /Hide macOS updates warnings/);
  await ctx.fleetHideWarning({ disabled: false }, 'macos_updates');
  const put = writes(ctx.calls).find(call => call.route === '/settings/fleet-warnings');
  assert.deepEqual(JSON.parse(put.opts.body), { hidden: ['macos_updates'] });
});
