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

test('a guest\'s own actions never carry their name into an attribute', async () => {
  // **An apostrophe ended the string.** The buttons used to be
  // `onclick="removeGuestAccount('guest-9','O'Brien')"` — `esc()` writes `&#39;`, the HTML
  // parser hands JavaScript a real `'`, and every button on that row was dead code for
  // anybody called O'Brien. Actions name the id; the name is looked up from the state.
  const calls = [];
  const nodes = { 'guests-body': element(), 'guest-msg': element() };
  const context = vm.createContext({
    document: { getElementById: id => nodes[id] || null },
    esc,
    api: async (path, options) => {
      calls.push([path, options && options.method]);
      if (path === '/guests' && !options) {
        return { guests: [{ id: 'guest-9', name: "O'Brien", paid_allowed: false, disabled: false,
                            removed: false, jobs: 1, unfinished: 0, spend_recorded: false,
                            spend: null }], password_min_length: 8 };
      }
      return {};
    },
    confirm: () => true,
    prompt: () => null,
  });
  vm.runInContext(between('let guestsState', 'async function signOut()'), context);
  await vm.runInContext('loadGuests()', context);          // the page's own render, not a copy
  const html = nodes['guests-body'].innerHTML;
  assert.ok(html.includes('Brien'), 'the name is drawn in its own cell');
  assert.ok(!/onclick="[^"]*Brien/.test(html),
    `the name must not be inside an attribute: ${html}`);
  assert.equal(vm.runInContext('guestById("guest-9").name', context), "O'Brien",
    'the name comes from the state instead');
  assert.equal(vm.runInContext('guestById("nobody").name', context), 'this guest',
    'an unknown id degrades to a sentence, never to undefined');
  calls.length = 0;
  await vm.runInContext('setGuestDisabled("guest-9", true)', context);
  assert.deepEqual(calls[0], ['/guests/guest-9', 'POST'], 'the action posts by id alone');
});

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

// The guest's agent key — one bearer token for an MCP agent, shown exactly once.
//
// The server is the boundary (a guest key is refused everywhere but Create, and every /v1
// route refuses it — walked in test_guest_api_keys.py). What is checked here is that the page
// offers the key, draws its state honestly, and **puts the key in one text box and nowhere
// else**: not in markup, not in an attribute, not in a URL.
function keyHarness(answerFor) {
  const nodes = {
    'guests-body': element(),
    'guest-msg': element(),
    'guest-key-box': { ...element(), style: { display: 'none' } },
    'guest-key-value': { ...element(), focus() { this.focused = true; }, select() { this.selected = true; } },
    'guest-key-what': element(),
  };
  const calls = [];
  const context = vm.createContext({
    document: { getElementById: id => nodes[id] || null },
    esc,
    api: async (path, options) => {
      calls.push([path, options && options.method]);
      return answerFor(path, options);
    },
    confirm: () => true,
    prompt: () => null,
    navigator: {},
  });
  vm.runInContext(between('let guestsState', 'async function signOut()'), context);
  return { context, nodes, calls };
}

function guestRow(over) {
  return { id: 'guest-1', name: 'Maria', paid_allowed: false, disabled: false, removed: false,
           jobs: 0, unfinished: 0, spend_recorded: false, spend: null, api_key: null, ...over };
}

test('a guest with no key offers one, and a removed guest offers nothing', async () => {
  const { context, nodes } = keyHarness(() => ({ guests: [
    guestRow(),
    guestRow({ id: 'guest-2', name: 'Sam', removed: true,
               api_key: { fingerprint: 'abcd1234', created_at: 1, last_used_at: null, revoked: true } }),
  ], password_min_length: 8 }));
  await vm.runInContext('loadGuests()', context);
  const html = nodes['guests-body'].innerHTML;
  assert.ok(html.includes("issueGuestKey('guest-1')"), html);
  assert.ok(html.includes('No key'), html);
  assert.ok(!html.includes("issueGuestKey('guest-2')"), 'a retired guest is history');
});

test('an active key shows its state and can be revoked', async () => {
  const { context, nodes } = keyHarness(() => ({ guests: [guestRow({
    api_key: { fingerprint: 'deadbeef', created_at: 1, last_used_at: 2, revoked: false } })] }));
  await vm.runInContext('loadGuests()', context);
  const html = nodes['guests-body'].innerHTML;
  assert.ok(html.includes('deadbeef'), 'the fingerprint identifies the key without being it');
  assert.ok(html.includes('Replace key'));
  assert.ok(html.includes("revokeGuestKey('guest-1')"));
  assert.ok(html.includes('used '), 'last use is drawn when the server knows it');
});

test('the key is shown once, in one text box, and never in the markup', async () => {
  const secret = 'gwk_abcdefghijklmnop';
  const { context, nodes, calls } = keyHarness((path, options) => {
    if (path === '/guests') return { guests: [guestRow()] };
    if (path === '/guests/guest-1/key' && options && options.method === 'POST') {
      return { guest: guestRow(), key: secret, guests: [guestRow()] };
    }
    return {};
  });
  await vm.runInContext('loadGuests()', context);
  await vm.runInContext("issueGuestKey('guest-1')", context);
  assert.equal(nodes['guest-key-value'].value, secret, 'the key is in the box, for copying');
  assert.equal(nodes['guest-key-box'].style.display, '', 'and the box is shown');
  assert.ok(!nodes['guests-body'].innerHTML.includes(secret), 'never drawn into the table');
  assert.ok(!JSON.stringify(calls).includes(secret), 'never put in a URL or a call');
  await vm.runInContext('hideGuestKey()', context);
  assert.equal(nodes['guest-key-value'].value, '');
  assert.equal(nodes['guest-key-box'].style.display, 'none');
});
