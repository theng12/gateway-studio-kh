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
// Every question in the page is now an in-app dialog; the tests' own confirm/alert/prompt
// stubs are where each answer comes from.
vm.runInContext(`globalThis.uiConfirm = async (t) => (typeof confirm === 'function' ? confirm(t) : true);
globalThis.uiAlert = async (t) => { if (typeof alert === 'function') alert(t); };
globalThis.uiPrompt = async (t, v) => (typeof prompt === 'function' ? prompt(t, v) : v);`, ctx);

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
  // The browser sidebar has no Overview row: the status page is in the Mac app, and a row that
  // only says "that is in the app" is a dead end (an old #overview link still lands on the note).
  assert.ok(!Array.from(NAV).some(section => Array.from(section.items).some(item => item.view === 'overview')),
    'Overview must not be a sidebar dead end in the browser');
  const labels = index => Array.from(NAV[index].items, item => item.label);
  const views = index => Array.from(NAV[index].items, item => item.view);
  assert.deepEqual(labels(0), ['Audio', 'Subtitle', 'Image', 'Music', 'Video', 'Chat']);
  assert.deepEqual(views(1), ['jobs', 'voices', 'models', 'analytics', 'packs']);
  assert.deepEqual(views(2), ['providers', 'fleet', 'developer-tests']);
  assert.deepEqual(views(3), ['guests', 'settings'],
    "What's New left the sidebar for the standard footer pill (one door, not two)");
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
  // The pages this console really carries are all reachable from the sidebar; What's New is the
  // footer pill's door instead.
  assert.match(source, /class="nav-pill" onclick="openNav\('whatsnew'\)"/);
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

function statusPayload(extra) {
  return Object.assign({
    version: '0.2.236', computer_name: 'terranash-1301',
    models: {by_modality: {}, publishable_models: 1, withheld_models: 0},
    providers: {enabled: 1, total: 2},
    hub: {running: true, base_url: 'http://127.0.0.1:8787'},
  }, extra);
}

function runStatus(payload) {
  const nodes = {};
  const ctx = vm.createContext({
    api: async () => payload,
    document: {getElementById: id => (nodes[id] ||= {textContent: '', innerHTML: '', className: '', hidden: false})},
    esc: value => String(value ?? ''),
  });
// Every question in the page is now an in-app dialog; the tests' own confirm/alert/prompt
// stubs are where each answer comes from.
vm.runInContext(`globalThis.uiConfirm = async (t) => (typeof confirm === 'function' ? confirm(t) : true);
globalThis.uiAlert = async (t) => { if (typeof alert === 'function') alert(t); };
globalThis.uiPrompt = async (t, v) => (typeof prompt === 'function' ? prompt(t, v) : v);`, ctx);

  vm.runInContext(between('async function loadStatus()', 'let voiceSampleAudio'), ctx);
  return {nodes, done: vm.runInContext('loadStatus()', ctx)};
}

test('the version line names the Mac, and the name is text, never markup', async () => {
  // A Computer Name is system text, not trusted markup: it is written with textContent.
  const {nodes, done} = runStatus(statusPayload({computer_name: '<b>not a tag</b>'}));
  await done;
  assert.equal(nodes['app-version'].textContent, '0.2.236', 'the pill carries the version; the Mac name has its own line');
  assert.equal(nodes['app-version'].innerHTML, '', 'the label is text, so no markup is parsed');
  assert.equal(nodes['nav-machine'].textContent, 'on <b>not a tag</b>');
  assert.equal(nodes['nav-machine'].hidden, false);
});

test('an unreadable computer name leaves the version line as it was', async () => {
  const {nodes, done} = runStatus(statusPayload({computer_name: null}));
  await done;
  assert.equal(nodes['app-version'].textContent, '0.2.236');
  assert.equal(nodes['nav-machine'].textContent, '');
  assert.equal(nodes['nav-machine'].hidden, true, 'the sidebar line hides rather than guessing');
});
