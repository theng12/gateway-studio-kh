const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../gateway/admin_ui.html'), 'utf8');
const start = html.indexOf('function imageModeLabel(');
const source = html.slice(start, html.indexOf('// ---------------------------------------------------------------------------', start));

function context(view = 'analytics') {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, { innerHTML: '', textContent: '', value: '' });
    return nodes.get(id);
  };
  node('analytics-group').value = 'by_modality';
  const requests = [];
  const ctx = vm.createContext({
    document: { getElementById: node }, state: { view },
    api: async url => {
      requests.push(url);
      return url.startsWith('/jobs') ? { jobs: [] } : {
        totals: { jobs: 1, succeeded: 1, failed: 0, cancelled: 0 }, limit: 20,
        by_modality: [{ key: 'image', jobs: 1, succeeded: 1, failed: 0,
                        seconds_median: null, seconds_mean: null, bytes_jobs: 0, bytes: 0 }],
      };
    },
    stateBadgeClass: () => 'ok', requesterLabel: () => 'Owner', ago: () => 'now',
    fleetBytes: value => `${value} bytes`,
    esc: n => String(n ?? '').replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
  });
// Every question in the page is now an in-app dialog; the tests' own confirm/alert/prompt
// stubs are where each answer comes from.
vm.runInContext(`globalThis.uiConfirm = async (t) => (typeof confirm === 'function' ? confirm(t) : true);
globalThis.uiAlert = async (t) => { if (typeof alert === 'function') alert(t); };
globalThis.uiPrompt = async (t, v) => (typeof prompt === 'function' ? prompt(t, v) : v);`, ctx);

  vm.runInContext(source, ctx);
  return { ctx, node, requests };
}

test('Usage mode filter reaches the shared server before pagination', async () => {
  const { ctx, node, requests } = context();
  node('analytics-image-mode').value = 'reference-to-image';
  await ctx.loadAnalytics();
  assert.ok(requests.includes('/jobs?limit=100&modality=image&image_mode=reference-to-image'));
  assert.match(node('analytics-image-jobs').innerHTML, /No matching jobs/);
});

test('Jobs mode filter uses the same server field', async () => {
  const { ctx, node, requests } = context('jobs');
  node('jobs-image-mode').value = 'text-to-image';
  await ctx.loadJobs();
  assert.deepEqual(requests, ['/jobs?limit=100&image_mode=text-to-image']);
});

test('Usage preserves unknown measurements and shows the actual sample limit', async () => {
  const { ctx, node } = context();
  await ctx.loadAnalytics();
  assert.match(node('analytics-summary').textContent, /Latest 20 records/);
  assert.equal((node('analytics-body').innerHTML.match(/Not recorded/g) || []).length, 3);
  assert.doesNotMatch(node('analytics-body').innerHTML, /0 bytes/);
});

test('Usage is a shared page with exactly the two owner mode labels', () => {
  assert.match(html, /id="view-analytics"/);
  assert.doesNotMatch(html, /analytics: 'Analytics is in the Gateway app/);
  for (const id of ['jobs-image-mode', 'analytics-image-mode']) {
    const select = html.slice(html.indexOf(`id="${id}"`)).split('</select>')[0];
    assert.match(select, /Text to image/);
    assert.match(select, /Reference to image/);
    assert.doesNotMatch(select, />Edit</);
  }
});
