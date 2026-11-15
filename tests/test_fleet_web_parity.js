/**
 * Action parity: every gateway route the retired native Fleet views call has a caller on the web
 * Fleet page.
 *
 * The owner, 2026-10-30: the Mac app's Fleet row now loads `gateway/admin_ui.html`, so an action
 * that lives only in `menubar/Fleet*.swift` is gone from the app. This suite reads the Swift files
 * themselves — not a hand-kept list — collects every `/admin/api…` route they call, and fails when
 * the Fleet page's own markup and script name no route matching it.
 *
 * **A route a web page truly cannot call is allow-listed below, with its reason.** An allow list
 * entry that no longer matches a Swift call fails too, so the list cannot quietly grow stale.
 *
 * Two reads need care, and both are handled here rather than by a reviewer's eye:
 *
 * 1. **Routes built from a helper prefix.** `FleetModelPreset` and `FleetModelInstall` call
 *    `"\(base)/model-presets"`, where `base` is a location+machine path built in the same file.
 *    The interpolation is normalised to a wildcard over any path, so the route still has to be
 *    called somewhere on the page.
 * 2. **Routes assembled from a helper argument.** `FleetTrash.path(_, tail)` ends its literal with
 *    `trash\(tail)` and passes `""`, `"/move-to-macos-trash"` and `"/delete"` as the tail, so no
 *    single literal contains those routes. The suffix interpolation is dropped from the literal
 *    (giving `/trash`), and the tails are named in ASSEMBLED below, each with the Swift call that
 *    builds it.
 */
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const page = fs.readFileSync(path.join(root, 'gateway/admin_ui.html'), 'utf8');

// The Fleet page's own code: its markup and its script. `api()` prepends `/admin/api`, so the
// routes compared here are everything after that prefix.
const fleetHtml = page.slice(page.indexOf('<!-- FLEET'), page.indexOf('<!-- JOBS (browser only) -->'));
const fleetScript = page.slice(page.indexOf("let fleetFilter = 'all';"), page.indexOf('async function loadJobs()'));
if (!fleetHtml.length || !fleetScript.length) throw new Error('admin_ui.html moved: the Fleet page is not where this suite looks');

// Routes a web page cannot call. Each names the reason, and the test fails if the reason's route is
// no longer called by any Fleet view.
const ALLOW = new Map([
  ['/fleet/controllers/*/machines/*/trash/delete',
    "the route demands the Gateway app's own per-launch owner-console secret (require_owner_console, "
    + "X-Gateway-Owner-Console), which only the native client holds. A web page cannot present it. The "
    + "recoverable move to macOS Trash is on the page; the permanent delete is in the app, at "
    + "View → Fleet Trash… (AppDelegate.openFleetTrash), which opens the kept native FleetTrash sheet."],
]);

// Routes assembled from a helper argument, so no single Swift literal contains them. The prefix is
// in FleetTrash.path's own literal above; these are the tails its callers pass.
const ASSEMBLED = [
  ['/fleet/controllers/*/machines/*/trash/move-to-macos-trash', 'FleetTrash.move — tail "/move-to-macos-trash"'],
  ['/fleet/controllers/*/machines/*/trash/delete', 'FleetTrash.confirmDelete — tail "/delete"'],
];

// Replace a Swift `\(…)` or a JS `${…}` with one marker, consuming balanced brackets so an
// expression containing calls (`row.id.addingPercentEncoding(…) ?? row.id`) stays one marker.
function markers(text, opener) {
  const open = opener === '${' ? '{' : '(';
  const close = opener === '${' ? '}' : ')';
  let out = '';
  let i = 0;
  while (i < text.length) {
    if (text.startsWith(opener, i)) {
      let depth = 0;
      let j = i + 1;
      for (; j < text.length; j += 1) {
        if (text[j] === open) depth += 1;
        else if (text[j] === close) { depth -= 1; if (depth === 0) break; }
      }
      i = j + 1;
      out += '\u0000';
    } else {
      out += text[i];
      i += 1;
    }
  }
  return out;
}

// `/admin/api/fleet/controllers/\(place)/machines/\(node)/trash\(tail)` and
// `/fleet/controllers/${id}/startup` both become `/fleet/controllers/*/…`:
//   * a marker that stands for a whole path segment is a wildcard;
//   * a marker appended to a segment (`trash\(tail)`) is a suffix argument and is dropped.
function normalise(literal) {
  let value = String(literal).replace(/^\/admin\/api/, '').split('?')[0];
  value = markers(value, '\\(');
  value = markers(value, '${');
  value = value.replace(/([^/\u0000])\u0000/g, '$1');
  value = value.replace(/\u0000/g, '*');
  return value.replace(/\/+$/, '');
}

function swiftRoutes() {
  const dir = path.join(root, 'menubar');
  const files = fs.readdirSync(dir).filter(name => /^Fleet.*\.swift$/.test(name));
  assert.ok(files.length >= 10, `expected the Fleet views, found ${files.length} files`);
  const routes = new Set();
  for (const name of files) {
    const source = fs.readFileSync(path.join(dir, name), 'utf8');
    for (const match of source.matchAll(/"[^"\n]*"/g)) {
      const literal = match[0].slice(1, -1);
      // A whole route, or one built from the `base`/helper prefix the same file defines.
      if (!literal.startsWith('/admin/api') && !literal.startsWith('\\(base)')) continue;
      routes.add(normalise(literal));
    }
  }
  return routes;
}

function pageRoutes() {
  const routes = new Set();
  // Every path literal in the Fleet script — a direct `api('/fleet')`, a template argument, and a
  // route held in a variable (the render switches choose one of two) all count as a caller.
  for (const match of fleetScript.matchAll(/(`[^`\n]*`|'[^'\n]*'|"[^"\n]*")/g)) {
    const literal = match[1];
    if (!/^[`'"](\/|\$\{)/.test(literal)) continue;
    const route = normalise(literal.slice(1, -1));
    if (!/(^|\*)\/(fleet|release|settings|providers)(\/|$)/.test(route)) continue;
    routes.add(route);
  }
  return routes;
}

// `*` means "any characters, across segments" — which is what a `\(base)` interpolation covers.
function matcher(expected) {
  const body = expected.split('*')
    .map(part => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('.*');
  return new RegExp(`^${body}$`);
}

test('every route the native Fleet views call has a caller on the web Fleet page', () => {
  const expected = swiftRoutes();
  for (const [route] of ASSEMBLED) expected.add(route);
  const called = [...pageRoutes()];
  assert.ok(expected.size >= 30, `the Swift route extractor found only ${expected.size} routes`);

  const missing = [];
  for (const route of [...expected].sort()) {
    if (ALLOW.has(route)) continue;
    if (!called.some(candidate => matcher(route).test(candidate) || matcher(normalise(candidate)).test(route))) {
      missing.push(route);
    }
  }
  assert.deepEqual(missing, [], `no web Fleet caller for:\n  ${missing.join('\n  ')}`);
});

test('the allow list holds only routes a web page cannot call, and nothing stale', () => {
  const expected = swiftRoutes();
  for (const [route] of ASSEMBLED) expected.add(route);
  const stale = [...ALLOW.keys()].filter(route => !expected.has(route));
  assert.deepEqual(stale, [], 'an allow-list entry names a route no Fleet view calls any more');
  for (const [route, reason] of ALLOW) {
    assert.ok(reason && reason.length > 40, `${route} needs its reason in a sentence`);
  }
});

test('the allow list is the owner-console delete, and nothing else', () => {
  // A change here is a decision about what the web page may do, so it is spelled out: the only
  // route a browser cannot reach is the one that permanently deletes, because the app alone can
  // prove it is the app.
  assert.deepEqual([...ALLOW.keys()], ['/fleet/controllers/*/machines/*/trash/delete']);
  assert.match(ALLOW.get('/fleet/controllers/*/machines/*/trash/delete'), /owner-console secret/);
});

test('the page really calls the routes this release added', () => {
  const called = [...pageRoutes()];
  const routes = [
    '/fleet/controllers/*/machines/*/startup', '/fleet/controllers/*/machines/*/name',
    '/fleet/controllers/*/machines/*/pause', '/fleet/controllers/*/machines/*/workstation',
    '/fleet/controllers/*/machines/*/network', '/fleet/controllers/*/machines/*/work-policy',
    '/fleet/controllers/*/machines/*/trash', '/fleet/controllers/*/machines/*/trash/move-to-macos-trash',
    '/fleet/controllers/*/machines/*/model-packages/*/download',
    '/fleet/controllers/*/machines/*/model-downloads/*',
    '/fleet/controllers/*/machines/*/model-presets', '/fleet/controllers/*/machines/*/model-preset',
    '/fleet/controllers/*/machines/*/model-packages/*',
    '/fleet/controllers/*/machines/*/render-optin', '/fleet/controllers/*/machines/*/render-while-paused',
    '/fleet/controllers/*/machines/*/macos-trash-auto-empty/enable',
    '/fleet/trash/enable-macos-auto-empty', '/fleet/startup/enable-autostart', '/fleet/counters/reset',
    '/fleet/model-installs', '/fleet/model-installs/*/stop', '/fleet/remote-rendering',
    '/fleet/controllers/*/jobs/*/cancel', '/fleet/controllers/*/jobs/*/items/*/retry',
    '/fleet/controllers/*/credential', '/fleet/controllers/*/hub-token', '/fleet/controllers/*/address',
    '/fleet/controllers/*/network', '/fleet/controllers/*/test', '/fleet/controllers/*/pause',
    '/fleet/controllers/*/resume', '/fleet/controllers/*',
    '/settings/fleet-health', '/settings/fleet-warnings', '/providers/*/refresh',
  ];
  const missing = routes.filter(route => !called.some(candidate => matcher(route).test(candidate)));
  assert.deepEqual(missing, [], `the page has no caller for:\n  ${missing.join('\n  ')}`);
});
