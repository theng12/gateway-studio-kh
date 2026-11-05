/* Hub defaults and owner intent stay visibly separate until the Hub confirms a change. */
(() => {
  let rows = [];
  const root = () => document.getElementById('image-limits');
  const message = text => { const node = document.getElementById('image-limits-msg'); if (node) node.textContent = text; };
  const number = value => value == null ? 'Not reported' : String(value);
  const describe = cell => cell ? `${cell.allowed === true ? 'On' : cell.allowed === false ? 'Off' : 'Not reported'} · max ${number(cell.max_reference_images)} · swap ${number(cell.swap_allowance_gb)} GB` : 'Not reported';

  function render(data) {
    rows = data.rows || [];
    const target = root();
    if (!target) return;
    if (!rows.length) { target.innerHTML = '<p class="muted">No Hub has reported an image limits table yet. Refresh its models after updating Studio Hub.</p>'; return; }
    const tiers = [...new Set(rows.flatMap(row => Object.keys(row.tiers || {})))].sort((a, b) => Number(a) - Number(b));
    const cells = (row, index) => tiers.map(tier => {
      const cell = row.tiers[tier];
      if (!cell) return '<td>Not reported</td>';
      const value = cell.change || cell.effective || cell.default;
      const label = `${row.provider_label} ${row.model_id}, ${row.mode}, ${row.reference_count == null ? 'general' : row.reference_count + ' references'}, ${tier} GiB`;
      return `<td><div>${esc(describe(cell.effective))}</div><div class="hint">${esc(cell.effective?.source || 'Not reported')}</div>
        ${cell.change ? `<p><b>your change</b>: ${esc(describe(cell.change))}<br><span class="hint">${esc(cell.detail || cell.status)}</span></p>` : ''}
        <details><summary>Edit</summary><p class="hint">Hub default: ${esc(describe(cell.default))}<br>${esc(cell.default?.source || 'Not reported')}<br>${esc(cell.default?.receipt || '')}</p>
          <form data-limit-row="${index}" data-limit-tier="${esc(tier)}">
          <label>Allowed <select name="allowed" aria-label="${esc(label)} allowed"><option value="true"${value.allowed ? ' selected' : ''}>On</option><option value="false"${!value.allowed ? ' selected' : ''}>Off</option></select></label>
          <label>Max references <input name="maximum" aria-label="${esc(label)} maximum references" type="number" min="0" step="1" required value="${esc(value.max_reference_images)}" style="width:7em"></label>
          <label>Swap allowance (GB) <input name="swap" aria-label="${esc(label)} swap allowance in GB" type="number" min="0" step="any" required value="${esc(value.swap_allowance_gb)}" style="width:7em"></label>
          <p><button type="submit" class="sm">Save change</button> <button type="button" class="sm" data-limit-reset${cell.change ? '' : ' disabled'}>Reset</button></p>
          </form></details></td>`;
    }).join('');
    target.innerHTML = `<p class="hint">Swap: ${esc(data.swap_allowance_units || 'Not reported')}. Zero permits no growth. Saved changes await Hub support and confirmation; effective limits remain in force.</p>
      <div style="overflow-x:auto" tabindex="0" role="region" aria-label="Model limits by physical RAM tier"><table class="grid" style="min-width:64rem"><thead><tr><th>Model and job</th>${tiers.map(tier => `<th>${esc(tier)} GiB</th>`).join('')}</tr></thead><tbody>${rows.map((row, index) => `<tr><th scope="row" style="width:16rem;max-width:16rem;overflow-wrap:anywhere;text-align:left">${esc(row.model_id)}<br><span class="hint">${esc(row.provider_label)} · ${esc(row.mode === 'edit' ? 'Reference to image' : 'Text to image')} · ${row.reference_count == null ? 'general limit' : esc(row.reference_count) + ' references'}</span></th>${cells(row, index)}</tr>`).join('')}</tbody></table></div>`;
  }

  function payload(form, reset) {
    const row = rows[Number(form.dataset.limitRow)];
    const tier = form.dataset.limitTier;
    if (!row || !Object.hasOwn(row.tiers, tier)) throw new Error('This cell changed. Refresh the table.');
    let change = null;
    if (!reset) {
      const maximumText = form.elements.maximum.value.trim();
      const swapText = form.elements.swap.value.trim();
      const maximum = Number(maximumText), swap = Number(swapText);
      if (!maximumText || !swapText || !Number.isSafeInteger(maximum) || maximum < 0 || !Number.isFinite(swap) || swap < 0) throw new Error('Use a whole reference maximum and a finite swap allowance of zero or more.');
      change = {allowed: form.elements.allowed.value === 'true', max_reference_images: maximum, swap_allowance_gb: swap};
    }
    return {provider_id: row.provider_id, model_id: row.model_id, mode: row.mode, reference_count: row.reference_count, tier, change};
  }

  async function save(form, reset) {
    const buttons = [...form.querySelectorAll('button')];
    try {
      const body = payload(form, reset);
      buttons.forEach(button => { button.disabled = true; });
      render(await api('/settings/image-limits', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}));
      message(reset ? 'Reset to the current Hub default.' : 'Your change was saved here. Awaiting Hub support; it is not applied.');
    } catch (error) { message(error.message || 'Could not save the change. Refresh and try again.'); }
    finally { buttons.forEach(button => { button.disabled = false; }); }
  }

  async function load() {
    const target = root();
    if (!target) return;
    target.innerHTML = '<p class="muted">Reading Hub limits…</p>';
    target.onsubmit = event => { const form = event.target.closest('form[data-limit-row]'); if (form) { event.preventDefault(); void save(form, false); } };
    target.onclick = event => { const button = event.target.closest('[data-limit-reset]'); if (button) void save(button.closest('form'), true); };
    try { render(await api('/settings/image-limits')); message(''); }
    catch (error) { target.innerHTML = ''; message(error.message || 'Could not read image limits. Refresh and try again.'); }
  }
  window.GatewayImageLimits = {load, render, payload};
})();
