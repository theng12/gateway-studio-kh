/* One owner-run finite vector; no timer, automatic submission or approval. */
(() => {
  'use strict';
  const key = t => `${t.controller_id}/${t.node_id}`;
  const unknown = v => v == null ? 'Not recorded' : String(v);
  const intersect = lists => lists.length ? lists[0].filter(v => lists.every(a => a.includes(v))) : [];
  const gcd = (a, b) => b ? gcd(b, a % b) : a;
  function common(caps, modelId, ceiling) {
    if (!caps.length || caps.some(c => !c.supported || !c.setting.verified || !c.setting.enabled))
      throw Error('Read every selected Mac and turn its verified Developer Mode switch On.');
    const models = caps.map(c => c.models.find(m => m.model.id === modelId && m.installable));
    if (models.some(m => !m)) throw Error('Choose a model installable on every selected Mac.');
    const bounds = {};
    for (const name of ['width_px', 'height_px', 'steps', 'seed', 'reference_count', 'prompt_characters', 'guidance']) {
      const ranges = models.map(m => m.settings_bounds[name]);
      if (ranges.every(r => r === null)) { bounds[name] = null; continue; }
      if (ranges.some(r => r === null)) throw Error('These Macs disagree about guidance; select a compatible group.');
      bounds[name] = {minimum: Math.max(...ranges.map(r => r.minimum)), maximum: Math.min(...ranges.map(r => r.maximum))};
    }
    bounds.prompt_characters.maximum = Math.min(bounds.prompt_characters.maximum, ceiling);
    bounds.reference_count.maximum = Math.min(bounds.reference_count.maximum, ...caps.map(c => c.bounds.input_refs));
    const multiple = models.reduce((a, m) => a / gcd(a, m.settings_bounds.dimension_multiple) * m.settings_bounds.dimension_multiple, 1);
    if (!Number.isSafeInteger(multiple)) throw Error('No safe common image dimension multiple.');
    for (const name of ['width_px', 'height_px']) bounds[name].minimum = Math.ceil(bounds[name].minimum / multiple) * multiple;
    if (Object.values(bounds).some(r => r && r.minimum > r.maximum)) throw Error('The selected Macs have no common settings range.');
    const modes = intersect(models.map(m => m.memory_modes));
    const kinds = intersect(caps.map(c => c.test_kinds));
    const fixtures = caps[0].fixture_sets.filter(f => caps.every(c => c.fixture_sets.some(x => x.id === f.id && x.sha256 === f.sha256)));
    if (!modes.length || !kinds.length || !fixtures.length) throw Error('No shared memory mode, test kind or packaged fixture set.');
    const limits = {};
    for (const name of Object.keys(caps[0].safety_policy.requested_limits)) {
      const ranges = caps.map(c => c.safety_policy.requested_limits[name]);
      limits[name] = {minimum: Math.max(...ranges.map(r => r.minimum)), maximum: Math.min(...ranges.map(r => r.maximum)), unit: ranges[0].unit};
      if (limits[name].minimum > limits[name].maximum) throw Error('No common attempt budget.');
    }
    return {bounds, multiple, modes, kinds, fixtures, limits, models};
  }
  const measurementNames = {measured_start_min_available_memory_bytes: 'Available memory before start', measured_runtime_seconds: 'Inference time', measured_peak_process_tree_rss_bytes: 'Peak owned process memory', measured_system_swap_growth_bytes: 'System swap growth'};
  function matchingTargets(targets, {filter = '', chip = '', ram = ''}) {
    return targets.filter(t => (!filter || `${t.label} ${t.node_id}`.toLowerCase().includes(filter)) && (!chip || t.chip === chip) && (!ram || String(t.ram_bytes) === ram));
  }
  function freshInventoryAt(selected, inventory) {
    if (!inventory.fresh) throw Error('The fleet inventory is stale. Refresh before Run.');
    for (const target of selected) {
      const matches = inventory.targets.filter(t => key(t) === key(target));
      if (matches.length !== 1 || !matches[0].online || matches[0].paused || matches[0].chip !== target.chip || matches[0].ram_bytes !== target.ram_bytes)
        throw Error('A selected Mac changed or became unavailable. Refresh and review the targets before Run.');
    }
    return inventory.inventory_at;
  }
  const state = {targets: [], runs: [], caps: new Map(), selected: new Set(), values: {}, model: '', filter: '', chip: '', ram: '', pending: new Map(), ceiling: null};
  const el = (tag, text, attrs = {}) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    Object.entries(attrs).forEach(([k, v]) => k in node ? node[k] = v : node.setAttribute(k, v));
    return node;
  };
  const button = (text, action, disabled = false) => {
    const b = el('button', text, {type: 'button', disabled});
    b.addEventListener('click', async () => { b.disabled = true; try { await action(); } catch (e) { message(e.message); } finally { b.disabled = disabled; } });
    return b;
  };
  const root = id => document.getElementById(id);
  const message = text => { root('developer-message').textContent = text || ''; };
  const call = (path = '', options) => api('/developer-tests' + path, options);
  const post = (path, data) => call(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
  const visible = () => matchingTargets(state.targets, state);
  async function read(t) {
    try {
      const answer = await call(`/targets/${encodeURIComponent(t.controller_id)}/${encodeURIComponent(t.node_id)}/capabilities`);
      state.caps.set(key(t), {cap: answer.capabilities});
      const pending = state.pending.get(key(t));
      if (pending && answer.capabilities.setting.request_id === pending.request_id) state.pending.delete(key(t));
    } catch (e) { state.caps.set(key(t), {error: e.message}); }
  }
  async function changeSwitch(t, cap) {
    const path = `/targets/${encodeURIComponent(t.controller_id)}/${encodeURIComponent(t.node_id)}/setting`;
    const request = state.pending.get(key(t)) || {format: 'studio.developer-test.node-setting.v1', enabled: !cap.setting.enabled, expected_revision: cap.setting.revision, request_id: crypto.randomUUID()};
    state.pending.set(key(t), request);
    try { await post(path, request); state.pending.delete(key(t)); }
    catch (e) { message(`Switch unresolved: ${e.message} Use Reconcile switch; it reuses this action and reads back rather than repeating it.`); }
    await read(t); render();
  }
  function renderTargets() {
    const host = root('developer-targets'); host.replaceChildren();
    root('developer-selection').textContent = `${state.selected.size} selected · ${visible().length} shown`;
    for (const t of visible()) {
      const row = el('div', null, {className: 'developer-target'});
      const select = el('input', null, {type: 'checkbox', checked: state.selected.has(key(t)), disabled: t.paused || !t.online, 'aria-label': `Select ${t.label}`});
      select.addEventListener('change', async () => { if (select.checked) { state.selected.add(key(t)); await read(t); } else state.selected.delete(key(t)); render(); });
      const label = el('label'); label.append(select, document.createTextNode(` ${t.label} · ${unknown(t.chip)} · ${t.ram_bytes == null ? 'RAM not recorded' : (t.ram_bytes / 1024 ** 3).toLocaleString() + ' GiB'}`));
      row.append(label, el('span', t.paused ? 'Paused — not selectable' : !t.online ? 'Offline — not selectable' : 'Online', {className: 'muted'}));
      const entry = state.caps.get(key(t)); const cap = entry?.cap;
      if (cap) {
        const verified = cap.setting.verified;
        const toggle = button(state.pending.has(key(t)) ? 'Reconcile switch' : verified ? (cap.setting.enabled ? 'On' : 'Off') : 'Unverified — read again', () => changeSwitch(t, cap), !verified);
        toggle.setAttribute('aria-label', `Developer Mode for ${t.label}`);
        toggle.setAttribute('role', 'switch'); toggle.setAttribute('aria-checked', String(verified && cap.setting.enabled));
        row.append(toggle);
        if (!cap.supported) row.append(el('span', cap.reason?.detail || 'This Hub has not proved support.', {className: 'muted'}));
      }
      row.append(button(entry?.error ? 'Read again' : 'Read switch', async () => { await read(t); render(); }, !t.online));
      if (entry?.error) row.append(el('span', entry.error, {className: 'muted'}));
      host.append(row);
    }
    if (!host.childNodes.length) host.append(el('p', 'No registered Macs match these filters.'));
  }
  function selectField(host, name, label, options, initial, changed) {
    const wrap = el('label', label); const input = el('select', null, {name, id: 'developer-' + name});
    for (const option of options) input.append(el('option', option.label ?? option, {value: option.value ?? option}));
    input.value = options.some(o => (o.value ?? o) === initial) ? initial : input.options[0]?.value || '';
    input.addEventListener('change', () => changed(input.value)); wrap.append(input); host.append(wrap); return input.value;
  }
  function numberField(host, name, range, step = 1) {
    const wrap = el('label', name.replaceAll('_', ' '));
    if (!range) { wrap.append(el('span', 'Not used (null)')); state.values[name] = null; host.append(wrap); return; }
    if (!(name in state.values)) state.values[name] = name.startsWith('max_') ? range.maximum : range.minimum;
    const input = el('input', null, {type: 'number', name, id: 'developer-' + name, min: range.minimum, max: range.maximum, step, required: true, value: state.values[name]});
    input.addEventListener('input', () => { state.values[name] = input.value === '' ? null : Number(input.value); });
    wrap.append(input, el('small', `${range.minimum.toLocaleString()}–${range.maximum.toLocaleString()} inclusive${range.unit ? ' ' + range.unit : ''}${step !== 'any' && step !== 1 ? ' · multiple of ' + step : ''}`)); host.append(wrap);
  }
  function renderForm() {
    const host = root('developer-vector'); host.replaceChildren();
    const selected = state.targets.filter(t => state.selected.has(key(t)));
    if (!selected.length) { host.append(el('p', 'Select Macs above to read their shared models and settings.')); return; }
    try {
      const caps = selected.map(t => { const entry = state.caps.get(key(t)); if (!entry?.cap) throw Error(entry?.error || 'Reading selected Macs…'); return entry.cap; });
      const ids = intersect(caps.map(c => c.models.filter(m => m.installable).map(m => m.model.id)));
      if (!ids.length) throw Error('No shared installable model is declared.');
      state.model = selectField(host, 'model', 'Model', ids, state.model, value => { state.model = value; state.values = {}; renderForm(); });
      const shared = common(caps, state.model, state.ceiling);
      state.values.kind = selectField(host, 'kind', 'Test', shared.kinds, state.values.kind, value => { state.values.kind = value; });
      state.values.memory_mode = selectField(host, 'memory_mode', 'Memory mode', shared.modes, state.values.memory_mode, value => { state.values.memory_mode = value; });
      state.values.fixture_set_id = selectField(host, 'fixture_set_id', 'Packaged fixtures', shared.fixtures.map(f => f.id), state.values.fixture_set_id, value => { state.values.fixture_set_id = value; renderForm(); });
      const fixture = shared.fixtures.find(f => f.id === state.values.fixture_set_id);
      shared.bounds.reference_count.maximum = Math.min(shared.bounds.reference_count.maximum, fixture.reference_order.length);
      if (shared.bounds.reference_count.minimum > shared.bounds.reference_count.maximum) throw Error('Packaged references do not cover the required count.');
      const form = el('form', null, {id: 'developer-run-form'}); const fields = el('div', null, {className: 'developer-fields'});
      Object.entries(shared.bounds).forEach(([name, range]) => numberField(fields, name, range, ['width_px', 'height_px'].includes(name) ? shared.multiple : name === 'guidance' ? 'any' : 1));
      const limits = el('fieldset'); limits.append(el('legend', 'This attempt’s safety budgets'));
      Object.entries(shared.limits).forEach(([name, range]) => numberField(limits, name, range, name === 'max_runtime_seconds' ? 'any' : 1));
      const note = el('p', `Production runs first. Tests wait for an idle slot and may yield when production arrives. Missing models are downloaded by the Hub and verified; installed weights stay after the test. ${shared.models.filter(m => !m.installed).length} selected Macs need installation. These budgets apply only to this attempt. Run does not approve evidence or change model defaults. Gateway reader ceiling: ${state.ceiling.toLocaleString()} bytes; prompt construction also stops at that many code points.`, {className: 'muted'});
      const run = el('button', 'Run test', {type: 'submit', className: 'primary', disabled: selected.some(t => state.pending.has(key(t)))});
      form.append(el('p', `Run on: ${selected.map(t => `${t.label} (${t.node_id})`).join(', ')}`, {id: 'developer-run-targets'}), fields, limits, note, run);
      form.addEventListener('submit', async event => {
        event.preventDefault(); if (!form.reportValidity()) return;
        run.disabled = true;
        const settings = {};
        for (const name of [...Object.keys(shared.bounds), 'memory_mode', 'fixture_set_id']) settings[name] = state.values[name];
        const requested_limits = Object.fromEntries(Object.keys(shared.limits).map(name => [name, state.values[name]]));
        const filters = {chip: state.chip || null, ram_bytes: state.ram ? Number(state.ram) : null, query: state.filter};
        const payload = {run_id: crypto.randomUUID(), inventory_at: state.inventory_at, filters, targets: selected.map(t => ({controller_id: t.controller_id, node_id: t.node_id})), kind: state.values.kind, model_id: state.model, settings, requested_limits};
        try {
          payload.inventory_at = freshInventoryAt(selected, await call());
          await post('/runs', payload); message('Run recorded. Collect status / log to follow the original attempt.'); await load();
        }
        catch (e) { message(`${e.message} Refresh the ledger before starting anything else; an attempted submission may need recovery.`); }
        finally { run.disabled = true; }
      });
      host.append(form);
    } catch (e) { host.append(el('p', e.message, {role: 'status'})); }
  }
  function renderRuns() {
    const body = root('developer-results'); body.replaceChildren();
    for (const row of state.runs) {
      const receipt = row.receipt; const metrics = receipt?.measurements; const tr = el('tr');
      const settings = row.request?.settings;
      const summary = settings ? `${settings.width_px}×${settings.height_px} · ${settings.steps} steps · ${settings.reference_count} references · ${settings.prompt_characters} characters · ${settings.memory_mode}` : null;
      for (const value of [row.node_id, receipt?.hardware?.chip_model, receipt?.hardware?.ram_bytes, row.request?.model?.id,
        summary, metrics?.generation_seconds, metrics?.peak_process_memory_bytes,
        metrics?.swap_growth_bytes, receipt?.measurement_metadata?.available_memory_before_start_bytes,
        row.log_total_bytes == null ? null : `${row.log_collected_bytes ?? 0} / ${row.log_total_bytes} bytes${row.log_complete ? ' · verified' : ' · collect remaining'}`,
        row.state, row.reason]) tr.append(el('td', unknown(value)));
      const actions = el('td');
      actions.append(button('Collect status / log', async () => { await post(`/runs/${row.id}/refresh`, {}); await load(); }));
      if (row.submitted && !receipt) actions.append(button('Cancel test', async () => { await post(`/runs/${row.id}/cancel`, {format: 'studio.developer-test.cancel.v1', request_id: crypto.randomUUID()}); await load(); }));
      actions.append(button('Read log', async () => { const log = await call(`/runs/${row.id}/log`); root('developer-detail').textContent = log.complete ? log.text : log.reason; }));
      if (row.state === 'passed' && row.log_complete) actions.append(button('Review evidence', () => review(row)));
      tr.append(actions); body.append(tr);
    }
    root('developer-empty').hidden = state.runs.length > 0;
  }
  async function review(row) {
    const proposal = await post(`/runs/${row.id}/proposal`, {});
    const host = root('developer-review'); host.replaceChildren(el('h2', 'Review measured evidence'), el('p', 'Applies only to this exact node, hardware, model revision, runner and settings. This is observed success, never an engine maximum.'));
    host.append(el('p', `${proposal.scope.node_ids.join(', ')} · ${proposal.scope.chip_model} · ${(proposal.scope.ram_bytes / 1024 ** 3).toLocaleString()} GiB · ${proposal.scope.model.id} (${proposal.scope.model.revision})`));
    const changes = el('ul');
    for (const change of proposal.changes) changes.append(el('li', `${measurementNames[change.field]}: ${change.previous_value == null ? 'not previously approved' : change.previous_value.toLocaleString()} → ${change.value.toLocaleString()} ${change.unit}`));
    const details = el('details'); details.append(el('summary', 'Exact settings and evidence identity'), el('pre', JSON.stringify(proposal, null, 2)));
    host.append(changes, details);
    host.append(button('Approve and publish measured limits', async () => {
      await post(`/runs/${row.id}/approve`, {proposal_sha256: proposal.proposal_sha256, expected_store_revision: proposal.expected_store_revision, owner_action_id: crypto.randomUUID()});
      host.replaceChildren(el('p', 'Measured limits published for this exact scope. Intrinsic limits, Hub grants and model defaults are unchanged.')); await load();
    }));
  }
  async function renderApprovals() {
    const evidence = await call('/measurements'); const host = root('developer-approvals');
    host.replaceChildren(el('h2', 'Published measured limits'));
    if (!evidence.rows.length) host.append(el('p', 'No measured limits published. A passed test still needs owner review and approval.'));
    for (const entry of evidence.rows) {
      const scope = entry.scope;
      const summary = `${scope.node_ids.join(', ')} · ${scope.chip_model} · ${(scope.ram_bytes / 1024 ** 3).toLocaleString()} GiB · ${scope.model.id} · ${scope.settings.reference_count} references · ${scope.settings.memory_mode}`;
      const values = el('ul');
      for (const [field, value] of Object.entries(entry.values)) values.append(el('li', `${measurementNames[field] || field}: ${unknown(value)} ${field === 'measured_runtime_seconds' ? 'seconds' : 'bytes'}`));
      host.append(el('p', summary), values, button('Restore previous evidence', async () => { await post(`/approvals/${entry.approval_id}/rollback`, {expected_store_revision: evidence.store_revision}); await load(); }));
    }
  }
  function render() {
    const selectable = new Set(visible().filter(t => t.online && !t.paused).map(key));
    state.selected = new Set([...state.selected].filter(id => selectable.has(id)));
    renderTargets(); renderForm(); renderRuns();
  }
  async function load() {
    try {
      const data = await call(); state.targets = data.targets; state.runs = data.runs; state.inventory_at = data.inventory_at; state.ceiling = data.reader_byte_ceiling;
      render(); await renderApprovals();
    } catch (e) { message(`Could not read Developer Mode: ${e.message}`); }
  }
  let bound = false;
  window.loadDeveloperTests = async () => {
    if (!bound) {
      bound = true;
      root('developer-refresh').addEventListener('click', async () => { for (const t of state.targets.filter(t => state.selected.has(key(t)))) await read(t); await load(); });
      root('developer-filter').addEventListener('input', event => { state.filter = event.target.value.toLowerCase(); render(); });
      for (const field of ['chip', 'ram']) root('developer-' + field).addEventListener('change', event => { state[field] = event.target.value; render(); });
      root('developer-select-shown').addEventListener('click', async () => { for (const t of visible().filter(t => t.online && !t.paused)) { state.selected.add(key(t)); await read(t); } render(); });
      root('developer-clear').addEventListener('click', () => { state.selected.clear(); render(); });
    }
    await load();
    for (const [name, values] of [['chip', [...new Set(state.targets.map(t => t.chip).filter(Boolean))]], ['ram', [...new Set(state.targets.map(t => t.ram_bytes).filter(v => v != null))]]]) {
      const select = root('developer-' + name); select.replaceChildren(el('option', name === 'chip' ? 'All chips' : 'All RAM sizes', {value: ''}));
      values.forEach(value => select.append(el('option', name === 'ram' ? (value / 1024 ** 3).toLocaleString() + ' GiB' : value, {value: String(value)}))); select.value = state[name];
    }
  };
  window.DeveloperTestsUI = {common, intersect, unknown, matchingTargets, freshInventoryAt};
})();
