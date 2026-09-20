const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
const functionSource = source.slice(source.indexOf('async function loadPacks()'), source.indexOf('async function importPack('));

test('library retry clears its old read error and blocks unavailable downloads', async () => {
  const message = {dataset:{}, className:'', textContent:''};
  const library = {innerHTML:''};
  let failing = true;
  const context = vm.createContext({
    document: {getElementById: id => id === 'pack-message' ? message : library},
    api: async () => {
      if (failing) throw Error('offline');
      return {archives:[{pack_id:'test.fixture', version:'1.0.0', archive_sha256:'a'.repeat(64), integrity:'unavailable'}]};
    },
    esc: value => String(value ?? ''),
  });
  vm.runInContext(functionSource, context);
  await context.loadPacks();
  assert.match(message.textContent, /Cannot read library/);
  failing = false;
  await context.loadPacks();
  assert.equal(message.textContent, '');
  assert.match(library.innerHTML, /disabled.*onclick="savePack/);
  assert.match(library.innerHTML, /Ownership: unverified/);
  message.textContent = 'Preserved test.fixture; not installed.';
  await context.loadPacks();
  assert.match(message.textContent, /Preserved/);
});

test('the full console script remains valid JavaScript', () => {
  new vm.Script(source.match(/<script>([\s\S]*)<\/script>/)[1]);
});
