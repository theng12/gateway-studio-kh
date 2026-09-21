const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
const styles = source.slice(source.indexOf('<style>'), source.indexOf('</style>'));

/** Every `selector { body }` pair in the page's own stylesheet. */
function allRules() {
  const rules = [];
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  let match;
  while ((match = pattern.exec(styles)) !== null) {
    rules.push({ selector: match[1].trim(), body: match[2] });
  }
  return rules;
}

/** Declaration bodies whose selector list contains `fragment`. */
function rulesFor(fragment) {
  return allRules().filter(rule => rule.selector.includes(fragment)).map(rule => rule.body);
}

test('every element carrying an action handler is natively focusable', () => {
  // A div with onclick is invisible to Tab: the control exists for a mouse and not
  // for a keyboard, which is exactly what this packet is about.
  const withHandlers = [...source.matchAll(/<([a-zA-Z0-9]+)([^>]*\bon[a-z]+="[^"]*"[^>]*)>/g)];
  assert.ok(withHandlers.length > 20, 'the page should still be full of controls');
  const focusable = new Set(['button', 'input', 'select', 'textarea', 'a', 'summary']);
  const offenders = withHandlers.map(([, tag]) => tag.toLowerCase()).filter(tag => !focusable.has(tag));
  assert.deepEqual(offenders, [], `non-focusable elements carry handlers: ${offenders.join(', ')}`);
});

test('a focused control is ringed, and no rule removes the outline without one', () => {
  for (const selector of ['button:focus-visible', 'input:focus-visible', 'select:focus-visible', 'textarea:focus-visible']) {
    const blocks = rulesFor(selector);
    assert.ok(blocks.length, `${selector} must exist`);
    assert.ok(blocks.some(block => /outline:\s*2px solid/.test(block)),
      `${selector} must draw a 2px outline`);
  }
  // `outline: none` is only defensible next to a rule that puts a ring back.
  for (const block of rulesFor(':focus')) {
    if (!/outline:\s*(none|0)/.test(block)) continue;
    assert.fail(`a :focus rule still removes the outline: ${block.trim()}`);
  }
});

test('a keyboard-focused control is not scrolled under a sticky header', () => {
  const rule = allRules().find(candidate => /scroll-margin-top/.test(candidate.body));
  assert.ok(rule, 'focusable controls need scroll-margin-top');
  for (const selector of ['button', 'input', 'select', 'textarea']) {
    assert.ok(rule.selector.includes(selector), `scroll-margin-top should cover ${selector}`);
  }
});

test('the create tabs announce their selection and stay in the tab order', () => {
  assert.match(source, /id="create-tabs"[^>]*role="group"/, 'the tab strip needs a labelled group');
  assert.match(source, /aria-label="Create modality"/);
  // `aria-selected` without `role="tab"` says nothing to assistive tech; pressed
  // toggle buttons keep every tab reachable by Tab.
  assert.match(source, /aria-pressed="\$\{t\.key === createTab\}"/);
  assert.doesNotMatch(source, /aria-selected="\$\{t\.key === createTab\}"/);
});
