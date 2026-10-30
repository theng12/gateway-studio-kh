const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('gateway/admin_ui.html', 'utf8');
const start = html.indexOf('/* -------------------------------- voice tags');
const end = html.indexOf('/* ---------------------------------- models', start);

test('tag filter shows only matching voices', () => {
  const elements = {};
  function element(id) {
    return elements[id] ||= { value: '', children: [], replaceChildren() { this.children = []; }, append(...items) { this.children.push(...items); }, setAttribute() {} };
  }
  const document = { getElementById: element, createElement: () => ({ children: [], append(...items) { this.children.push(...items); }, setAttribute() {} }) };
  element('voice-tag-filter').options = [{value:''}, {value:'Khmer'}, {value:'English'}];
  element('voice-tag-filter').value = 'Khmer';
  const context = vm.createContext({document, Option: function(value) { this.value = value; }, console});
  vm.runInContext(html.slice(start, end) + '\nvoiceTagRows = [{id:"a",display_name:"First",tags:["Khmer"]},{id:"b",display_name:"Second",tags:["English"]}]; voiceTagLimits = {max_length:32};', context);
  context.renderVoiceTags();
  const rows = element('voice-tag-rows').children;
  assert.equal(rows.length, 2); // matching row and suggestion list
  assert.equal(rows[0].children[0].children[0].textContent, 'First');
});
