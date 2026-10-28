// The guest view of the console, and "Requested by" — the page's own source, run in a VM.
//
// The server is the boundary: every admin route refuses a guest except Create (`require_admin`
// plus the `/admin` middleware, walked route by route in `test_admin_guests.py`). What is
// checked here is what the page *tells* a guest and the owner — two things that are cheap to
// get wrong and impossible to see in a passing API test:
//
//   1. **A guest lands on Create and nowhere else**, whatever hash the browser carries.
//   2. **"Requested by" is never blank and never guessed.** A job recorded before the field
//      existed reads "not recorded"; it does not silently become "owner".
//   3. **Spend says "not recorded"**, not 0 — this Gateway keeps no per-job money record, and a
//      zero there would read as "this guest cost nothing".

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

function element(value = '') {
  return { value, innerHTML: '', textContent: '', className: '', disabled: false, style: {} };
}

function viewHarness(guest) {
  const context = vm.createContext({ GUEST: guest || null, location: { hash: '' } });
  vm.runInContext(between('function viewFromHash()', "window.addEventListener('hashchange'"),
    context);
  return context;
}

test('a guest has one page, whatever the hash says', () => {
  const guest = viewHarness({ name: 'Maria' });
  for (const hash of ['#models', '#fleet', '#providers', '#jobs', '#packs', '']) {
    guest.location.hash = hash;
    assert.equal(vm.runInContext('viewFromHash()', guest), 'create', hash);
  }
});

test('without a guest the hash still decides the page', () => {
  const owner = viewHarness(null);
  const cases = { '#models': 'models', '#fleet': 'fleet', '#packs': 'packs', '#jobs': 'jobs' };
  for (const [hash, view] of Object.entries(cases)) {
    owner.location.hash = hash;
    assert.equal(vm.runInContext('viewFromHash()', owner), view, hash);
  }
});

test('"Requested by" is a name, or an honest "not recorded"', () => {
  const context = vm.createContext({});
  vm.runInContext(between('function requesterLabel(job)', '// One state -> colour mapping'),
    context);
  const label = job => vm.runInContext(`requesterLabel(${JSON.stringify(job)})`, context);
  assert.equal(label({ requested_by_label: 'Maria', requested_by: { role: 'guest', label: 'Maria' } }),
    'Maria');
  assert.equal(label({ requested_by_label: 'Story Studio', requested_by: null }), 'Story Studio');
  // A job from before the field existed: never blank, never guessed as the owner.
  assert.equal(label({ requested_by: null, requested_by_label: 'not recorded' }), 'not recorded');
  assert.equal(label({}), 'not recorded');
});

function guestPageHarness(answer) {
  const nodes = { 'guests-body': element(), 'guest-msg': element() };
  const context = vm.createContext({
    document: { getElementById: id => nodes[id] || null },
    esc,
    api: async () => answer,
    confirm: () => true,
    prompt: () => null,
  });
  vm.runInContext(between('let guestsState', 'async function signOut()'), context);
  return { context, nodes };
}

test('the Guests page draws what the server said, including what it does not know', async () => {
  const { context, nodes } = guestPageHarness({ guests: [
    { id: 'guest-1', name: 'Maria', paid_allowed: false, disabled: false, removed: false,
      jobs: 12, unfinished: 1, spend_recorded: false, spend: null },
    { id: 'guest-2', name: 'Sam', paid_allowed: true, disabled: true, removed: false,
      jobs: 0, unfinished: 0, spend_recorded: false, spend: null },
    { id: 'guest-3', name: 'Alex', paid_allowed: false, disabled: true, removed: true,
      jobs: 3, unfinished: 0, spend_recorded: false, spend: null },
  ], password_min_length: 8 });
  await vm.runInContext('loadGuests()', context);
  const html = nodes['guests-body'].innerHTML;
  assert.ok(html.includes('Maria') && html.includes('Sam') && html.includes('Alex'), html);
  assert.ok(html.includes('active') && html.includes('disabled') && html.includes('removed'), html);
  assert.ok(html.includes('>12<'), 'a real job count is drawn');
  assert.ok(!html.includes('$0') && html.includes('not recorded'),
    'spend is "not recorded", never a zero that reads as free');
  // A retired guest is history: its toggle and its buttons are gone, its count stays.
  const alex = html.slice(html.indexOf('Alex'));
  assert.ok(!alex.slice(0, 400).includes('Reset password'), alex.slice(0, 400));
  assert.ok(html.includes('onchange="setGuestPaid(\'guest-1\''), 'the owner switch is there');
  assert.ok(!html.includes('setGuestPaid(\'guest-3\''), 'a removed guest has no switch');
});
