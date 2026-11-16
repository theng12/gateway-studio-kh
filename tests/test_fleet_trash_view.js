/**
 * Fleet → Trash: every Mac's trash on one page, and emptying it from here (0.2.264).
 *
 * **Why.** The owner asked to delete a Mac's trash "through the gateway or the hub", then for "one
 * button to delete across all the linking sites and nodes and controllers … what's in the trash will
 * go". The page must say honestly what each Mac holds, keep the figures that are not reported
 * separate from a real zero, say plainly that emptying is permanent, and leave a skipped Mac's
 * reason **on its row** — not only in a log.
 *
 * What this pins, in the page's own code:
 *
 * 1. There is a Trash tab, its pane, and one primary "Empty all trash" action.
 * 2. The permanence sentence is on the page and in the button's own words.
 * 3. A size a Mac does not report reads "not reported", never 0; a stated 0 reads "empty".
 * 4. A macOS Trash the Hub cannot read names the reason (a permission), not a size.
 * 5. Each Mac's outcome is drawn on that Mac's row, with its state, code/reason and freed bytes.
 * 6. Both actions go to the one route (`/fleet/trash/empty`), and count and bytes are never
 *    invented for a Mac that did not state them.
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

/** The trash helpers, in the page's own words, with only `esc` standing in for the page. */
function trashHelpers() {
  const code = [
    between('let trashResults = {};', 'function trashKey('),
    between('function trashKey(', 'function renderFleetTrash'),
  ].join('\n');
  const context = {
    esc: value => String(value ?? ''),
    document: { getElementById: () => null },
    lastFleetPayload: null,
  };
  vm.createContext(context);
  vm.runInContext(`${code}
    globalThis.__t = { trashSize, trashMacText, trashFolderText, trashGB, trashKey };`, context);
  return context.__t;
}

const t = trashHelpers();
// The helpers run in their own vm realm, so their plain objects are compared as plain data.
const plain = value => JSON.parse(JSON.stringify(value));

test('the Trash tab exists, with one primary empty action', () => {
  assert.match(source, /\['trash', 'Trash'\]/, 'the tab is in FLEET_TABS');
  assert.match(source, /id="fleet-pane-trash"/, 'and has a pane');
  assert.match(source, /id="fleet-trash-empty-all"[^>]*>\s*Empty all trash…/, 'one all-Macs action');
  assert.match(source, /fleet-pane-trash[\s\S]*?id="fleet-trash-wrap"/, 'and a table under it');
});

test('emptying is permanent, in the page and in the button', () => {
  assert.match(source, /<strong>Emptying is permanent\.<\/strong>/, 'the page says so');
  assert.match(source, /Empty permanently…<\/button>/, 'each Mac\'s button says so');
  const dialog = between('async function emptyTrashOne(', 'async function loadFleet(');
  assert.match(dialog, /This is permanent\./, 'and the question says so in its title');
  assert.match(dialog, /confirmLabel: 'Empty permanently'/, 'and on its confirm button');
  assert.match(dialog, /uiConfirm\(/, 'an in-app dialog, never a native confirm');
  assert.doesNotMatch(source, /\bwindow\.confirm|\bwindow\.alert|\bwindow\.prompt/, 'never native');
});

test('a figure nobody reported is not reported, never zero', () => {
  assert.equal(t.trashSize(null), null, 'no size is null, not "0 B"');
  assert.equal(t.trashSize(0), 'empty', 'a stated 0 is a real reading');
  assert.equal(t.trashSize(2_400_000_000), '2.4 GB');
  assert.deepEqual(plain(t.trashFolderText({})), { text: 'not reported', bad: true });
  assert.deepEqual(plain(t.trashFolderText({ hub_trash: { count: 0, bytes: 0 } })), { text: 'empty', bad: false });
  assert.deepEqual(plain(t.trashFolderText({ hub_trash: { count: 14, bytes: 2_400_000_000 } })),
                   { text: '14 items, 2.4 GB', bad: false });
  assert.deepEqual(plain(t.trashMacText({})), { text: 'not reported', bad: true });
  assert.equal(t.trashGB(null), 'not reported', 'free disk and memory too');
  assert.equal(t.trashGB(0), '0.0 GB free', 'and a stated 0 is drawn as 0');
});

test('a macOS Trash the Hub cannot read names the reason, not a size', () => {
  assert.deepEqual(
    plain(t.trashMacText({ macos_trash: { count: null, bytes: null, code: 'FULL_DISK_ACCESS_NEEDED' } })),
    { text: 'needs Full Disk Access', bad: true });
  assert.deepEqual(plain(t.trashMacText({ macos_trash: { count: null, bytes: null, code: 'TRASH_UNREADABLE' } })),
                   { text: 'cannot be read', bad: true });
  assert.deepEqual(plain(t.trashMacText({ macos_trash: { count: 3, bytes: 900_000_000 } })),
                   { text: '3 items, 900 MB', bad: false });
});

test('each Mac\'s outcome is drawn on that Mac\'s own row', () => {
  const render = between('function renderFleetTrash(', 'async function loadTrash(');
  assert.match(render, /trashResults\[key\]/, 'the row reads its own Mac\'s result');
  assert.match(render, /Last empty/, 'and the column says what it is');
  assert.match(render, /result\.reason/, 'a skipped or failed Mac says why on the row');
  assert.match(render, /result\.left_bytes/, 'and what is left when it knows');
  assert.match(render, /nothing to empty/, 'a Mac with both parts stated empty is not offered');
  const answer = between('function trashAnswer(', 'async function emptyTrashEverywhere(');
  assert.match(answer, /row\.location_id/, 'the answer is keyed the way the rows are');
});

test('both actions go to the one route, and none invents a figure', () => {
  for (const fn of ['async function emptyTrashEverywhere(', 'async function emptyTrashOne(']) {
    const body = between(fn, fn.includes('Everywhere') ? 'async function emptyTrashOne('
                                                       : 'async function loadFleet(');
    assert.match(body, /api\('\/fleet\/trash\/empty'/, `${fn} calls the route`);
  }
  const payload = between('function trashPayload(', 'function trashAnswer(');
  assert.match(payload, /scope: 'all'/, 'the whole fleet is one scope');
  assert.match(payload, /location_id: m\.location_id, machine: m\.machine/, 'one Mac is its two ids');
  assert.doesNotMatch(payload, /expected_count|expected_bytes/, 'nothing measured is guessed at');
});
