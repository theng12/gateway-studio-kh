// The console's More/filters menus: open/close state and the document-click exemption.
//
// A control whose own click opens a .more-menu must be exempt from the document handler, or the
// handler closes the menu again in the same click. The Fleet Filters button and the Fleet bulk
// button are the two outside an .overflow wrapper; the provider, SFX, worker and table More
// buttons sit inside one.
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

function element(hidden = false) {
  return { hidden, attrs: {}, setAttribute(name, value) { this.attrs[name] = value; } };
}

function context() {
  const menus = [element(true), element(true)];
  const buttons = [element(false), element(false)];
  const byId = {
    'pmenu-one': menus[0], 'fleet-filters': menus[1],
    'fleet-filters-btn': buttons[1],
  };
  const document = {
    getElementById: id => byId[id] || null,
    querySelectorAll: selector => (selector === '.more-menu' ? menus
      : selector === '[aria-haspopup="true"]' ? buttons : []),
  };
  const ctx = vm.createContext({ document });
  vm.runInContext(between('function closeMenus()', "document.addEventListener?.('click'")
    + between('function toggleFleetFilters()', '// The empty states carry their own way out')
    + ';globalThis.__m = {closeMenus, toggleCardMenu, toggleFleetFilters};', ctx);
  return { m: ctx.__m, menus, buttons, byId };
}

test('a card More button opens and closes its menu and says so', () => {
  const { m, menus, byId } = context();
  const button = element(false);
  button.attrs['aria-expanded'] = 'false';
  byId['pmenu-one'] = menus[0];

  m.toggleCardMenu(button, 'one');
  assert.equal(menus[0].hidden, false);
  assert.equal(button.attrs['aria-expanded'], 'true');

  m.toggleCardMenu(button, 'one');
  assert.equal(menus[0].hidden, true);
  assert.equal(button.attrs['aria-expanded'], 'false');
});

test('opening one menu closes the other and resets its button', () => {
  const { m, menus, buttons } = context();
  menus[1].hidden = false;
  buttons[0].attrs['aria-expanded'] = 'true';

  m.toggleFleetFilters();
  assert.equal(menus[1].hidden, false, 'the filters menu opens');
  assert.equal(buttons[1].attrs['aria-expanded'], 'true');
  assert.equal(menus[0].hidden, true, 'the other menu closes');
  assert.equal(buttons[0].attrs['aria-expanded'], 'false', 'the other button resets');
});

test('the two menu buttons outside an overflow wrapper are exempt from the document closer', () => {
  // The handler itself is an inline listener; this pins the two ids it must allow through.
  assert.match(source, /closest\('#fleet-filters-btn'\)/);
  assert.match(source, /closest\('#fleet-bulk-btn'\)/);
  assert.match(source, /id="fleet-bulk-btn"/);
});
