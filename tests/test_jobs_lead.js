// Jobs leads with the answer: what the loaded rows are doing, counted by plain state words, and
// each count is the filter for it. The raw Gateway word stays in the badge's tooltip.
//
// Owner's rule the Fleet page already follows (2026-11-18): a page leads with its answer, and a
// state reads as a word a person says, never `accepted` or `queued`.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
const START = source.indexOf('function imageModeLabel');
const END = source.indexOf('async function retryJob');
if (START < 0 || END < 0) throw new Error('Jobs section boundary changed');

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function harness(jobs = [], apiOverride = null) {
  const nodes = {
    'jobs-lead': { innerHTML: 'Reading jobs…' },
    'jobs-body': { innerHTML: '' },
    'jobs-image-mode': { value: '' },
  };
  const requests = [];
  const sandbox = {
    esc,
    ago: () => 'just now',
    stateBadgeClass: () => '',
    requesterLabel: () => 'Story Studio',
    Number, JSON, Array,
    state: { view: 'jobs' },
    document: { getElementById: id => nodes[id] || null },
    api: apiOverride || (async path => { requests.push(path); return { jobs }; }),
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(source.slice(START, END), ctx);
  return { ctx, nodes, requests };
}

const job = (id, state, model = 'qwen-image') => ({
  id, short_id: id.slice(0, 8), modality: 'image', image_mode: 'text-to-image',
  submitted_reference_count: 0, state, model_id: model, model_name: model,
  provider_name: 'terranash-1500', fleet: true, executed_by: null,
  created_at: Date.now() / 1000, caller: 'story',
});

test('accepted and queued read as Waiting; the raw word is kept in the tooltip', () => {
  const { ctx } = harness();
  for (const raw of ['accepted', 'queued']) {
    assert.equal(vm.runInContext(`jobsStateWord('${raw}')`, ctx), 'Waiting');
  }
  const html = vm.runInContext(`jobsTable([${JSON.stringify(job('abcdef123456', 'accepted'))}])`, ctx);
  assert.match(html, />Waiting<\/span>/);
  assert.match(html, /title="accepted"/);
  assert.doesNotMatch(html, />accepted</);
});

test('the lead counts the loaded rows by word, and each count is its filter', () => {
  const rows = [job('aaaa1111', 'running'), job('bbbb2222', 'accepted'),
    job('cccc3333', 'accepted'), job('dddd4444', 'succeeded')];
  const { ctx, nodes } = harness();
  vm.runInContext(`jobsLastRows = ${JSON.stringify(rows)}; renderJobsLead(jobsLastRows);`, ctx);
  const lead = nodes['jobs-lead'].innerHTML;
  assert.match(lead, /4 jobs/);
  assert.match(lead, /running<span class="n">1<\/span>/);
  assert.match(lead, /waiting<span class="n">2<\/span>/);
  assert.match(lead, /succeeded<span class="n">1<\/span>/);
  assert.match(lead, /onclick="jobsFilterState\('Waiting'\)"/);
  assert.match(lead, /4 jobs recorded, newest first/);
});

test('a full hundred-row read says it is a window, not the whole history', () => {
  const rows = Array.from({ length: 100 }, (_, index) => job(`job${String(index).padStart(5, '0')}`, 'succeeded'));
  const { ctx, nodes } = harness();
  vm.runInContext(`jobsLastRows = ${JSON.stringify(rows)}; renderJobsLead(jobsLastRows);`, ctx);
  assert.match(nodes['jobs-lead'].innerHTML, /latest 100 loaded jobs, newest first — not every job this gateway has run\./);
});

test('a count filters the table to that word, and clicking it again restores the table', () => {
  const rows = [job('aaaa1111', 'running', 'run-model'), job('bbbb2222', 'accepted', 'wait-model')];
  const { ctx, nodes } = harness();
  vm.runInContext(`jobsLastRows = ${JSON.stringify(rows)}; renderJobsBody();`, ctx);
  assert.match(nodes['jobs-body'].innerHTML, /run-model/);

  vm.runInContext(`jobsFilterState('Waiting');`, ctx);
  assert.match(nodes['jobs-body'].innerHTML, /wait-model/);
  assert.doesNotMatch(nodes['jobs-body'].innerHTML, /run-model/);
  assert.match(nodes['jobs-lead'].innerHTML, /Show all/);
  assert.match(nodes['jobs-lead'].innerHTML, /showing 1 of 2 jobs, newest first/);

  vm.runInContext(`jobsFilterState('Waiting');`, ctx);
  assert.match(nodes['jobs-body'].innerHTML, /run-model/);
  assert.doesNotMatch(nodes['jobs-lead'].innerHTML, /Show all/);
});

test('a filter with nothing in it says so, and a failed read clears the lead', async () => {
  const { ctx, nodes } = harness();
  vm.runInContext(`jobsLastRows = [${JSON.stringify(job('aaaa1111', 'succeeded'))}]; jobsFilterState('Failed');`, ctx);
  assert.match(nodes['jobs-body'].innerHTML, /No jobs are failed in this read\./);

  const failing = harness([], async () => { throw new Error('gateway asleep'); });
  await vm.runInContext('loadJobs()', failing.ctx);
  assert.equal(failing.nodes['jobs-lead'].innerHTML, '');
  assert.match(failing.nodes['jobs-body'].innerHTML, /Could not read jobs: gateway asleep/);
});

test('loadJobs stores one read and draws both the lead and the table from it', async () => {
  const rows = [job('aaaa1111', 'running'), job('bbbb2222', 'failed')];
  const { ctx, nodes, requests } = harness(rows);
  await vm.runInContext('loadJobs()', ctx);
  assert.deepEqual(requests, ['/jobs?limit=100']);
  assert.match(nodes['jobs-lead'].innerHTML, /2 jobs/);
  assert.match(nodes['jobs-body'].innerHTML, /aaaa1111/);
  assert.match(nodes['jobs-body'].innerHTML, /bbbb2222/);
});
