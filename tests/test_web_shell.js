/**
 * The web console's shell: a sidebar drawn from one list, and no dead links.
 *
 * The owner, 2026-10-30: *"can i view the fleet on the web and have it look like this?"* and
 * *"make sure anything built or updated in the future instantly appears [in both], like a
 * mirror, instead of something we keep building each time."* So the page is an app shell whose
 * navigation is the native window's own grouping, and a page the Mac app still owns is a real
 * note rather than a link to nothing.
 *
 * What this pins, from the page's own source:
 *
 * 1. **The native groups are present**, in the native order: Overview, Create, Library,
 *    Connection, App.
 * 2. **Every row resolves**: to a page section this console has, or to an "it is in the app"
 *    note. No row is a destination with no door.
 * 3. **In the app the web sidebar hides itself** (the window already has one), and a guest is
 *    offered Create only.
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

function nav() {
  const ctx = vm.createContext({});
  vm.runInContext(between('const NAV = [', 'const NAV_PATHS'), ctx);
  vm.runInContext(between('const APP_ONLY = {', 'function navEntries()'), ctx);
  // `const` is lexical, so it is read back by evaluating the name, not from the context object.
  return { NAV: vm.runInContext('NAV', ctx), APP_ONLY: vm.runInContext('APP_ONLY', ctx) };
}

const VIEWS_IN_HTML = new Set(
  [...source.matchAll(/<section id="view-([a-z-]+)"/g)].map(match => match[1]));

test('the sidebar carries the native groups, in the native order', () => {
  const { NAV } = nav();
  // `Array.from` reads the VM's own array into this realm, so the comparison is by value.
  assert.deepEqual(Array.from(NAV, section => section.group).filter(Boolean),
    ['Create', 'Library', 'Connection', 'App']);
  assert.equal(NAV[0].group, undefined, 'Overview is ungrouped, as it is in the app');
  assert.equal(NAV[0].items[0].view, 'overview');
  const labels = index => Array.from(NAV[index].items, item => item.label);
  const views = index => Array.from(NAV[index].items, item => item.view);
  assert.deepEqual(labels(1), ['Audio', 'Subtitle', 'Image', 'Music', 'Video', 'Chat']);
  assert.deepEqual(views(2), ['jobs', 'voices', 'models', 'analytics', 'packs']);
  assert.deepEqual(views(3), ['providers', 'fleet', 'developer-tests']);
  assert.deepEqual(views(4), ['whatsnew', 'guests', 'settings']);
});

test('every row opens a page this console has, or says where the page lives', () => {
  const { NAV, APP_ONLY } = nav();
  const rows = Array.from(NAV).flatMap(section => Array.from(section.items));
  assert.ok(rows.length > 0);
  for (const row of rows) {
    const hasPage = VIEWS_IN_HTML.has(row.view);
    const hasNote = Object.prototype.hasOwnProperty.call(APP_ONLY, row.view);
    assert.ok(hasPage || hasNote, `${row.view} is a dead link`);
    if (hasNote) assert.match(APP_ONLY[row.view], /in the Gateway app\./, `${row.view}'s note says where it is`);
  }
  // The pages this console really carries are all reachable from the sidebar.
  for (const view of ['fleet', 'jobs', 'models', 'providers', 'create', 'packs', 'guests']) {
    assert.ok(rows.some(row => row.view === view), `${view} has no sidebar row`);
  }
});

test('a Create lane keeps its own address, and the page picks that lane', () => {
  const lanes = between('const CREATE_TABS = [', 'const CREATE_FIELDS');
  for (const key of ['audio', 'subtitle', 'image', 'music', 'video', 'chat']) {
    assert.match(lanes, new RegExp(`key: '${key}'`));
  }
  // `#create:audio` (and a bare `#audio`) is read before the page draws.
  assert.match(source, /hash\.startsWith\('create:'\)/);
  assert.match(source, /'audio', 'subtitle', 'image', 'music', 'video', 'chat'/);
});

test('the web sidebar hides itself in the app, and a guest is offered one page', () => {
  // In the app the window already has a sidebar, so the page's own one must not draw twice.
  assert.match(source, /if \(IN_APP\) \{[\s\S]*?classList\.add\('in-app'\)/);
  assert.match(source, /if \(nav\) nav\.hidden = false;/);
  assert.match(source, /if \(typeof GUEST !== 'undefined' && GUEST\)[\s\S]*?Signed in as/);
  // A guest's row list and picker are Create alone — the server refuses everything else.
  assert.match(source, /guest \? '<option value="create">Create<\/option>'/);
});

test('the shell is a left sidebar beside one page, and a top bar at phone width', () => {
  assert.match(source, /\.shell \{ display: grid; grid-template-columns: 220px minmax\(0, 1fr\)/);
  assert.match(source, /\.shell\.in-app \{ display: block; \}/);
  assert.match(source, /@media \(max-width: 760px\) \{[\s\S]*?\.nav-list, \.nav-title, \.nav-foot \{ display: none; \}/);
  // The Machines table becomes labelled rows rather than scrolling sideways.
  assert.match(source, /\.ftable td::before \{ content: attr\(data-label\)/);
});
