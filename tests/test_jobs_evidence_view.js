const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
const start = html.indexOf('async function loadJobs()');
const end = html.indexOf('// ---------------------------------------------------------------------------', start);
const source = html.slice(start, end);

async function render(job) {
  const box = { innerHTML: '' };
  const ctx = vm.createContext({
    document: { getElementById: () => box }, state: { view: 'jobs' },
    api: async () => ({ jobs: [job] }), stateBadgeClass: () => 'ok',
    requesterLabel: () => 'Owner', ago: n => `time-${n}`,
    esc: n => String(n ?? '').replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
  });
  vm.runInContext(source, ctx);
  await ctx.loadJobs();
  return box.innerHTML;
}

const job = { id: 'test-job', state: 'succeeded', modality: 'image', model_id: 'Klein',
  provider_name: 'Remote studio', executed_by: 'Remote studio (controller)', created_at: 10 };

test('jobs show the qualified machine and measured reference count, including zero', async () => {
  const result = await render({ ...job, reference_count: 0, started_at: 11, finished_at: 12 });
  assert.match(result, /Remote studio \(controller\)/);
  assert.match(result, /0 reference images used/);
  assert.match(result, /Gateway dispatched: time-11/);
  assert.match(result, /Gateway settled: time-12/);
});

test('old jobs show unrecorded clocks without inventing reference counts', async () => {
  const result = await render(job);
  assert.match(result, /Gateway dispatched: not recorded/);
  assert.match(result, /Gateway settled: not recorded/);
  assert.doesNotMatch(result, /reference images used/);
});

test('execution labels are escaped', async () => {
  const result = await render({ ...job, executed_by: '<script>bad</script>', reference_count: 1 });
  assert.match(result, /&lt;script&gt;bad&lt;\/script&gt;/);
  assert.match(result, /1 reference image used/);
  assert.doesNotMatch(result, /<script>/);
});
