// The console's colour tokens, measured as numbers rather than judged by eye.
//
// Owner, 2026-11-13: "the colour scheme is pale and bad, no proper nice contrast." Light and
// dark are explicit token ramps; every text pair must reach WCAG AA (4.5:1) on every surface it
// is used on, and control borders must reach 3:1. status text is checked on its own chip, not on
// the page. The rendered measurement (computed colours) is contrast.mjs at the gate; this test
// pins the source values so a later edit cannot quietly lower them.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');

function block(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + 1);
  if (first < 0 || last < 0) throw new Error(`admin_ui.html moved: ${start}`);
  return source.slice(first, last);
}

function tokens(text) {
  const map = {};
  for (const match of text.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;/g)) map[match[1]] = match[2];
  return map;
}

const light = tokens(block(':root {', '@media (prefers-color-scheme: dark)'));
const darkBlock = block('@media (prefers-color-scheme: dark)', '* { box-sizing');
const dark = { ...light, ...tokens(darkBlock) };

function luminance(hex) {
  const channels = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255);
  const f = c => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * f(channels[0]) + 0.7152 * f(channels[1]) + 0.0722 * f(channels[2]);
}
function ratio(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100;
}

const TEXT_PAIRS = [
  ['text', 'bg'], ['text', 'panel'], ['text', 'side'],
  ['dim', 'bg'], ['dim', 'panel'], ['dim', 'panel2'], ['dim', 'side'],
  ['tertiary', 'panel'], ['tertiary', 'panel2'],
  ['chip-text', 'chip-bg'],
  ['ok-text', 'ok-bg'], ['warn-text', 'warn-bg'], ['bad-text', 'bad-bg'],
  ['info-text', 'info-bg'], ['off-text', 'off-bg'],
  ['on-accent', 'accent'],
];
const BORDER_PAIRS = [['btn-line', 'panel'], ['line-strong', 'panel']];

for (const [scheme, map] of [['light', light], ['dark', dark]]) {
  test(`${scheme}: every text pair is AA on every surface`, () => {
    for (const [fg, bg] of TEXT_PAIRS) {
      assert.ok(map[fg] && map[bg], `${scheme}: missing --${fg} or --${bg}`);
      const value = ratio(map[fg], map[bg]);
      assert.ok(value >= 4.5, `${scheme}: --${fg} on --${bg} is ${value}:1, under 4.5:1`);
    }
  });
  test(`${scheme}: control borders reach 3:1`, () => {
    for (const [fg, bg] of BORDER_PAIRS) {
      const value = ratio(map[fg], map[bg]);
      assert.ok(value >= 3, `${scheme}: --${fg} on --${bg} is ${value}:1, under 3:1`);
    }
  });
  test(`${scheme}: the rail and the card are separate layers from the page`, () => {
    assert.ok(ratio(map.side, map.bg) >= 1.08,
      `${scheme}: the sidebar (${map.side}) is not a layer against the page (${map.bg})`);
    assert.ok(ratio(map.panel, map.bg) >= 1.08,
      `${scheme}: the card surface (${map.panel}) is not a layer against the page (${map.bg})`);
  });
}

test('the console no longer depends on system Canvas colours for its ramp', () => {
  for (const banned of ['CanvasText', 'AccentColor', '--bg: Canvas']) {
    assert.ok(!source.includes(banned), `${banned} came back into the palette`);
  }
});
