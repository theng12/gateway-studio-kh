// Overflow menus on every page: shown under their button, flipped above it when the window's
// bottom is near, measured after they are visible, and closed when anything scrolls or resizes.
//
// 0.2.278 moved these menus to fixed positioning so a scrolling `.tablewrap` could not clip them.
// A fixed menu keeps the coordinates it was given, so without the close-on-scroll below it hangs
// where its button used to be; and without a height measurement a row near the window's bottom
// opens its menu off-screen.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
const START = source.indexOf('function closeMenus()');
const END = source.indexOf("document.addEventListener?.('click'");
if (START < 0 || END < 0) throw new Error('admin_ui.html menu section moved');
const FUNCTIONS = source.slice(START, END);

function harness({ width = 1000, height = 800, nodes = {} } = {}) {
  const ctx = vm.createContext({
    window: { innerWidth: width, innerHeight: height },
    document: { getElementById: id => nodes[id] || null, querySelectorAll: () => [] },
  });
  vm.runInContext(FUNCTIONS
    + ';globalThis.__m = {closeMenus, toggleCardMenu, placeMenuUnderButton};', ctx);
  return ctx.__m;
}

function menu({ hidden = true, w = 240, h = 100 } = {}) {
  return {
    hidden, style: {}, offsetWidth: w,
    get offsetHeight() { return this.hidden ? 0 : h; },
  };
}

function button(top, bottom, right = 200) {
  return { getBoundingClientRect: () => ({ top, bottom, right }), attrs: {},
    setAttribute(name, value) { this.attrs[name] = value; } };
}

test('a menu opens under its button, left-aligned to its right edge and kept on screen', () => {
  const m = harness();
  const node = menu();
  m.placeMenuUnderButton(node, button(100, 120, 200));
  assert.equal(node.style.position, 'fixed');
  assert.equal(node.style.top, '124px');
  assert.equal(node.style.left, '8px');        // 200 - 240 is off the left edge
  assert.equal(node.style.right, 'auto');
});

test('a menu on a row near the bottom flips above its button', () => {
  const m = harness({ height: 768 });
  const node = menu({ hidden: false, h: 200 });
  m.placeMenuUnderButton(node, button(700, 720));
  assert.equal(node.style.top, '496px');       // 700 - 200 - 4
});

test('a narrow window keeps the menu inside both edges', () => {
  const m = harness({ width: 320 });
  const right = menu({ hidden: false });
  m.placeMenuUnderButton(right, button(100, 120, 300));
  assert.equal(right.style.left, '60px');      // 300 - 240, still inside
  const offscreen = menu({ hidden: false });
  m.placeMenuUnderButton(offscreen, button(100, 120, 900));
  assert.equal(offscreen.style.left, '72px');  // 320 - 240 - 8
});

test('it stays below when there is no room above either', () => {
  const m = harness({ height: 768 });
  const node = menu({ hidden: false, h: 700 });
  m.placeMenuUnderButton(node, button(60, 80));
  assert.equal(node.style.top, '84px');
});

test('the menu is measured after it is shown, or its height would read zero', () => {
  const node = menu({ h: 200 });
  const trigger = button(700, 720);
  const m = harness({ height: 768, nodes: { 'pmenu-one': node } });
  m.toggleCardMenu(trigger, 'one');
  assert.equal(node.hidden, false);
  assert.equal(trigger.attrs['aria-expanded'], 'true');
  assert.equal(node.style.top, '496px');       // flipped from a measured 200px height
});

test('a scroll or a resize closes any open menu', () => {
  const listeners = {};
  const menus = [menu({ hidden: false }), menu({ hidden: false })];
  const triggers = [{ attrs: { 'aria-expanded': 'true' },
    setAttribute(name, value) { this.attrs[name] = value; } }];
  const document = {
    addEventListener: (type, handler) => { listeners[`document:${type}`] = handler; },
    querySelectorAll: selector => selector === '.more-menu' ? menus
      : selector === '[aria-haspopup="true"]' ? triggers : [],
  };
  const window = {
    innerWidth: 1000, innerHeight: 800,
    addEventListener: (type, handler) => { listeners[`window:${type}`] = handler; },
  };
  const ctx = vm.createContext({ document, window });
  vm.runInContext(FUNCTIONS
    + `;document.addEventListener?.('scroll', closeMenus, true);
       window.addEventListener?.('resize', closeMenus);`, ctx);

  assert.equal(typeof listeners['document:scroll'], 'function');
  assert.equal(typeof listeners['window:resize'], 'function');
  listeners['document:scroll']();
  assert.equal(menus[0].hidden, true);
  assert.equal(menus[1].hidden, true);
  assert.equal(triggers[0].attrs['aria-expanded'], 'false');
});
