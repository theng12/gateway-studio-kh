// Offline diagnostic: real console functions, fake DOM/network, no browser/live API.
// Run: node _handoffs/checks/console_action_probe.js
// A repair should change the recorded messages; this is not a passing test suite.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../gateway/admin_ui.html'), 'utf8');
function section(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  if (first < 0 || last < 0) throw new Error('Console function boundary changed');
  return source.slice(first, last);
}
const elements = {
  'cf-msg': {}, 'cf-model': {value: 'mock::chat'}, 'keep-msg-job-0': {},
};
let requests = 0;
const context = vm.createContext({
  document: {getElementById: id => elements[id]},
  currentTab: () => ({key: 'chat', modality: 'chat'}),
  CREATE_FIELDS: {chat: []}, cfValue: () => '',
  crypto: {randomUUID: () => 'offline-probe'},
  adminHeaders: extra => extra || {}, showTokenPrompt: () => {},
  fetch: async () => {
    requests += 1;
    return {status: 200, ok: true, text: async () => JSON.stringify({id: 'job', state: 'accepted'})};
  },
  renderCreateResult: () => {}, pollCreate: () => {},
});
vm.runInContext([
  section('async function api(path, opts)', '// Contents cannot go through'),
  section('async function submitCreate(btn)', 'function pollCreate(id)'),
  section('async function keepArtifact(jobId, assetId, btn)', 'function renderCreateResult(job)'),
].join('\n'), context);
(async () => {
  await context.submitCreate({});
  await context.keepArtifact('job', '0', {});
  console.log(JSON.stringify({
    baseline: 'fcfde43', method: 'offline console functions with mocked successful HTTP responses',
    successful_mock_requests: requests,
    generate_message: elements['cf-msg'].textContent,
    keep_message: elements['keep-msg-job-0'].textContent,
  }, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
