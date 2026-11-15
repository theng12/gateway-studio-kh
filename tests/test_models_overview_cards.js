// The Models page's overview cards — driven in a VM over the page's own source.
//
// The owner's report (2026-11-15): the Models page was not up to standard.
//   1. Empty category cards were tall blocks holding one line of grey text.
//   2. The chat card said "8 of 1613 offered" above a single inline row: the
//      count came from the server summary while the list came from the rendered
//      rows, so the two could contradict each other on the same card.
//   3. Every button looked the same, so nothing on the card read as the action.
//
// No app, no gateway, no network: the page's own functions run against a
// hand-built DOM.

const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
const START = source.indexOf('function renderModelsOverview(');
// End at the function's own closing brace so the slice carries the renderer and
// nothing that runs at load time.
function endOfFunction(text, from) {
  let depth = 0;
  let seen = false;
  for (let i = from; i < text.length; i += 1) {
    if (text[i] === '{') { depth += 1; seen = true; }
    else if (text[i] === '}') { depth -= 1; if (seen && depth === 0) return i + 1; }
  }
  return -1;
}
const END = endOfFunction(source, START);
if (START < 0 || END < 0) throw new Error('renderModelsOverview boundary changed in admin_ui.html');

function harness() {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) {
      nodes.set(id, {
        id, innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
        listeners: {}, addEventListener() {}, closest() { return null; },
      });
    }
    return nodes.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: node, querySelectorAll: () => [] },
    esc: value => String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;'),
    modalityLabel: m => (m === 'chat' ? 'LLM chat' : m),
    discoverModality() {},
    setModelsMode() {},
    toggleModelEverywhere() {},
    state: { modelRows: [], meta: {} },
    console,
  });
  vm.runInContext(source.slice(START, END), context);
  return { context, get: node };
}

function row(id, modality, enabled) {
  return {
    provider_id: 'p', provider_name: 'P', model_id: id, display_name: id,
    modality, enabled, provider_enabled: true, is_location: false,
  };
}

test('an empty category renders one compact line, not a tall blank card', () => {
  const { context, get } = harness();
  // The page only draws categories the summary knows about, so an empty one is
  // a category with a total and no enabled rows — exactly what the owner saw.
  context.state.modelRows = [];
  context.state.meta = { summary: { chat: { enabled: 0, total: 1613 } } };
  context.renderModelsOverview();
  const html = get('models-overview').innerHTML;
  assert.ok(html.includes('model-card is-empty'),
    'a category with nothing offered is marked as empty so it cannot render as a full card');
  assert.ok(html.includes('Nothing offered yet.'), 'it still says why it is empty');
  assert.ok(html.includes('Discover'), 'and offers the one action that fixes it');
  assert.ok(!html.includes('model-mini'),
    'an empty card carries no model list, so nothing reserves blank height');
});

test('the count on a card matches the rows it actually draws', () => {
  const { context, get } = harness();
  const rows = Array.from({ length: 8 }, (_, i) => row(`m${i}`, 'chat', true));
  context.state.modelRows = rows;
  context.state.meta = { summary: { chat: { enabled: 8, total: 1613 } } };
  context.renderModelsOverview();
  const html = get('models-overview').innerHTML;

  const drawn = (html.match(/<li>/g) || []).length;
  assert.equal(drawn, 6, 'at most six rows are drawn inline');

  // The card must not claim eight while drawing one: the count and the list come
  // from the same rows now, and the remainder is stated rather than implied.
  assert.ok(html.includes('8 of'), 'the true offered total is still stated');
  assert.ok(html.includes('showing 6'), 'the card says it is showing a subset');
  assert.ok(html.includes('2 more offered'), 'and says how many are left');
  assert.ok(html.includes('Manage all'), 'with one action to see them');
});

test('a card with one offered row says one, not a summary figure', () => {
  const { context, get } = harness();
  context.state.modelRows = [row('only', 'chat', true)];
  context.state.meta = { summary: { chat: { enabled: 1, total: 1613 } } };
  context.renderModelsOverview();
  const html = get('models-overview').innerHTML;
  assert.ok(html.includes('1 of'), 'the count is the row it draws');
  assert.ok(!html.includes('more offered'), 'nothing is hidden, so nothing is claimed');
  assert.equal((html.match(/<li>/g) || []).length, 1);
});

test('a card with something offered is not marked empty', () => {
  const { context, get } = harness();
  context.state.modelRows = [row('a', 'chat', true)];
  context.state.meta = { summary: { chat: { enabled: 1, total: 1613 } } };
  context.renderModelsOverview();
  const html = get('models-overview').innerHTML;
  assert.ok(!html.includes('is-empty'), 'a populated category keeps its full card');
  assert.ok(html.includes('Manage'), 'and its Manage action');
});
