// The console's one in-app dialog: confirm, alert and prompt without the browser API.
//
// In the packaged webview the native panels are silent no-ops (owner rule, 2026-11-13), so the
// page must own every question. These tests run the page's own helper against a hand-built DOM:
// they pin both answers, Escape, Enter, the queued second question, and that the page contains no
// native call at all.
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

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.listeners = {};
    this.attrs = {};
    this.style = {};
    this.value = '';
    this.textContent = '';
    this.className = '';
  }
  append(...children) { this.children.push(...children); }
  remove() { this.removed = true; }
  setAttribute(name, value) { this.attrs[name] = value; }
  addEventListener(type, handler) { (this.listeners[type] ??= []).push(handler); }
  dispatch(type, event = {}) {
    let prevented = false;
    for (const handler of this.listeners[type] ?? []) handler({ preventDefault: () => { prevented = true; }, ...event });
    return prevented;
  }
  click() { this.dispatch('click'); }
  close() { this.closed = true; }
  showModal() { this.open = true; }
  focus() { this.focused = true; }
  select() { this.selected = true; }
}

function find(element, test) {
  for (const child of element.children) {
    if (test(child)) return child;
    const found = find(child, test);
    if (found) return found;
  }
  return null;
}

function harness() {
  const body = new FakeElement('body');
  const context = vm.createContext({
    document: { body, createElement: tag => new FakeElement(tag) },
    Promise, String, Object, Array,
  });
  vm.runInContext(between('let khDialogQueue = Promise.resolve();',
                          '// **The console has to be able to prove who it is.**')
    + ';globalThis.__d = {uiConfirm, uiAlert, uiPrompt};', context);
  return { body, d: context.__d };
}

const dialogs = body => body.children.filter(child => child.tagName === 'DIALOG');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

const button = (dialog, label) => find(dialog, el => el.tagName === 'BUTTON' && el.textContent === label);

test('the page asks no question through the browser API', () => {
  assert.doesNotMatch(source, /(^|[^A-Za-z0-9_])(confirm|alert|prompt)\(/,
    'a native confirm/alert/prompt call came back into the console');
});

test('a confirm answers true on the button and false on Cancel', async () => {
  const { body, d } = harness();
  const yes = d.uiConfirm('Delete this pool?');
  await tick();
  const first = dialogs(body)[0];
  assert.equal(first.children[0].textContent, 'Delete this pool?');
  button(first, 'Continue').click();
  assert.equal(await yes, true);

  const no = d.uiConfirm('Delete this pool?');
  await tick();
  button(dialogs(body)[1], 'Cancel').click();
  assert.equal(await no, false);
});

test('Escape cancels, and Enter in a prompt accepts the typed value', async () => {
  const { body, d } = harness();
  const escaped = d.uiConfirm('Continue?');
  await tick();
  dialogs(body)[0].dispatch('cancel');
  assert.equal(await escaped, false);

  const typed = d.uiPrompt('New name', 'old');
  await tick();
  const prompt = dialogs(body)[1];
  const field = find(prompt, el => el.tagName === 'INPUT');
  assert.equal(field.value, 'old', 'the current value is offered');
  field.value = 'renamed';
  field.dispatch('keydown', { key: 'Enter' });
  assert.equal(await typed, 'renamed');
});

test('a prompt answers null on Cancel and the text on Save', async () => {
  const { body, d } = harness();
  const cancelled = d.uiPrompt('New name', 'old');
  await tick();
  button(dialogs(body)[0], 'Cancel').click();
  assert.equal(await cancelled, null);

  const saved = d.uiPrompt('New name', '');
  await tick();
  const dialog = dialogs(body)[1];
  find(dialog, el => el.tagName === 'INPUT').value = 'shop-net';
  button(dialog, 'Save').click();
  assert.equal(await saved, 'shop-net');
});

test('an alert has one button and resolves nothing', async () => {
  const { body, d } = harness();
  const shown = d.uiAlert('Could not save this Mac\'s image model: offline');
  await tick();
  const dialog = dialogs(body)[0];
  assert.equal(button(dialog, 'Cancel'), null, 'an alert is not a question');
  button(dialog, 'OK').click();
  assert.equal(await shown, undefined);
});

test('a second question waits for the first to be answered', async () => {
  const { body, d } = harness();
  const first = d.uiConfirm('First?');
  const second = d.uiConfirm('Second?');
  await tick();
  assert.equal(dialogs(body).length, 1, 'two dialogs must never sit on top of each other');
  button(dialogs(body)[0], 'Continue').click();
  assert.equal(await first, true);
  await tick();
  assert.equal(dialogs(body).length, 2);
  button(dialogs(body)[1], 'Continue').click();
  assert.equal(await second, true);
});
