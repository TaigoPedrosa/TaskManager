// Waves view: `tm wave discover`'s own choice, simulated forward from live state and
// rendered as one card per wave. Deliberately self-contained (its own fetch, its own render
// target #waves-content) rather than routed through core.js's api()/scheduleRender(): the
// only globals it leans on are the ones every other view already shares -- the shared
// renderers, openNode, setWavesLoadPending and isStaticMode (core.js), filters.specMode
// (filters.js) and window.tmStore.

let waveDepth = 1;
let waveSize = null;
let waveMaxSize = null;
let waveMaxDepth = null;
let waveData = [];
// /api/waves' `nodes`: a named node's kind, title, display and live lease.
let waveNodes = {};
let waveLoading = false;
let waveError = null;
// Indices whose Held section the user toggled away from its default: open only when wave 1
// itself has no entries ("Nothing claimable." with nothing else on screen to explain it),
// collapsed otherwise, an empty later wave included.
let waveHeldOverrides = new Set();
let waveRequestSeq = 0;
let waveRefetchScheduled = false;
// Nothing is fetched until the view is first shown, so a page opened on another view never
// asks for waves or flashes the load bar for them.
let wavesShown = false;

async function getJson(path) {
  let res;
  try {
    res = await fetch(path);
  } catch (e) {
    throw new Error('Network error: could not reach the server.');
  }
  if (!res.ok) {
    let detail = null;
    try {
      detail = (await res.json()).detail;
    } catch (e) {
      /* non-JSON body */
    }
    throw new Error(detailText(detail) || `GET ${path} failed (${res.status})`);
  }
  return res.json();
}

// FastAPI answers a validation failure (422) with `detail` as a list of {loc, msg, type}
// objects, and an HTTPException with a plain string.
function detailText(detail) {
  if (detail == null) return null;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((d) => (d && d.msg ? d.msg : JSON.stringify(d))).join('; ');
  return JSON.stringify(detail);
}

async function loadWaveMeta() {
  try {
    const meta = await getJson('/api/meta');
    waveSize = meta.dispatch.wave_size;
    waveMaxSize = meta.dispatch.tick_budget;
  } catch (e) {
    // Left null: the size input carries no default/cap of its own, and the server still
    // enforces the real bound on every request regardless.
  }
}

// Only inclusion has a matching server param (/api/waves has no exclude list); this mirrors
// the tri-state control's own read (modeEntries in filters.js) without needing that helper.
function waveSpecFilter() {
  const specs = [];
  filters.specMode.forEach((mode, id) => {
    if (mode === 'include') specs.push(id);
  });
  return specs;
}

async function fetchWaves() {
  // The one chokepoint every wave request routes through (init, size change, compute, reset,
  // and a filters.js refetch) -- a static export has no /api/waves behind any of them.
  if (isStaticMode || !wavesShown) return;
  const seq = ++waveRequestSeq;
  waveLoading = true;
  waveError = null;
  setWavesLoadPending(true);
  renderWaves();
  // The size bound is refused here rather than by /api/waves: a 400 or 422 response logs a
  // browser console error no amount of catching in JS can silence. Only a plain run of digits
  // counts, so 0, negatives, fractions, exponents and an empty box all take the refusal.
  const size = waveSize === null ? null : String(waveSize).trim();
  if (size !== null && waveMaxSize !== null && !(/^\d+$/.test(size) && Number(size) >= 1 && Number(size) <= waveMaxSize)) {
    waveData = [];
    waveError = `Could not compute waves: wave size must be 1–${waveMaxSize} (this project's dispatch.tick_budget).`;
    waveLoading = false;
    setWavesLoadPending(false);
    renderWaves();
    return;
  }
  const params = new URLSearchParams({ depth: String(waveDepth) });
  if (size !== null) params.set('size', size);
  waveSpecFilter().forEach((s) => params.append('spec', s));
  try {
    const res = await getJson(`/api/waves?${params}`);
    if (seq !== waveRequestSeq) return; // superseded by a later request
    waveData = res.waves;
    waveNodes = res.nodes || {};
    waveMaxDepth = res.max_depth;
  } catch (e) {
    if (seq !== waveRequestSeq) return;
    waveData = [];
    waveError = `Could not compute waves: ${e.message}`;
  } finally {
    if (seq === waveRequestSeq) {
      waveLoading = false;
      setWavesLoadPending(false);
    }
    renderWaves();
  }
}

function computeNextWave() {
  if (waveComputeDisabled()) return;
  waveDepth += 1;
  fetchWaves();
}

function resetWaves() {
  waveDepth = 1;
  waveHeldOverrides = new Set();
  fetchWaves();
}

function onWaveSizeChange(size) {
  waveSize = size;
  fetchWaves();
}

function waveComputeDisabled() {
  if (waveLoading) return true;
  // /api/waves refuses a depth past its cap, so the button stops there.
  if (waveMaxDepth !== null && waveData.length >= waveMaxDepth) return true;
  const last = waveData[waveData.length - 1];
  return !!last && last.entries.length === 0;
}

// Runs on every switch to Waves (core.js's setViewMode); only the first one loads.
async function initWaves() {
  // A static export has no server behind /api/meta or /api/waves, and main.js removes its
  // Waves toggle, so there is nothing here to render or ask.
  if (isStaticMode || wavesShown) return;
  wavesShown = true;
  waveLoading = true;
  renderWaves();
  await loadWaveMeta();
  fetchWaves();
}

// A counts-tree change (window.tmStore's statusesChanged) means some node's status may have
// moved, so wave 1 onward may no longer be what tm would choose -- refetched at the depth
// already on screen, coalesced the same way core.js's scheduleRender() is.
function scheduleWavesRefetch() {
  if (waveRefetchScheduled) return;
  waveRefetchScheduled = true;
  requestAnimationFrame(() => {
    waveRefetchScheduled = false;
    fetchWaves();
  });
}
window.tmStore.onChange((patch) => {
  if (patch && patch.statusesChanged) scheduleWavesRefetch();
});


// Rendering -----------------------------------------------------------------------------------

// A wave entry's status_before/status_after are the raw Status a real claim would carry
// (core.status.Status), not a DisplayStatus -- IMPLEMENTED/REVIEWED/FIXED fold to whichever
// waiting status their own next action would show, same as a real node's display would.
const WAVE_STATUS_DISPLAY = {
  IMPLEMENTED: 'WAITING_REVIEW',
  REVIEWED: 'WAITING_MERGE',
  FIXED: 'WAITING_REVIEW',
};

function waveDisplay(rawStatus) {
  return WAVE_STATUS_DISPLAY[rawStatus] || rawStatus;
}

const WAVE_ROW = 'wave-row flex items-center gap-2 h-6 px-2 rounded-md cursor-pointer hover:bg-zinc-800/60';

// One line per step: now -> next, the kind badge on a container, the id, the title, then the
// model when the wave mixes models, a repo other than the default, and the live lease.
function waveRowHtml(entry, showModel) {
  const info = waveNodes[entry.id];
  const lease = entry.in_flight && info ? info.lease : null;
  return `
    <div class="${WAVE_ROW}" data-node-id="${esc(entry.id)}">
      ${statusIcon(waveDisplay(entry.status_before))}
      <span class="text-[11px] leading-4 text-zinc-400" aria-hidden="true">→</span>
      ${statusIcon(waveDisplay(entry.status_after))}
      ${kindBadge(entry.kind)}
      ${idLink(entry.id, entry.kind)}
      <span class="flex-1 min-w-0 truncate text-xs leading-4 text-zinc-200" title="${esc(entry.title)}">${esc(entry.title)}</span>
      ${showModel ? modelPill(entry.model) : ''}
      ${entry.repos.filter((r) => r !== '.').map(repoPill).join('')}
      ${leasePulse(lease)}
    </div>
  `;
}

function heldRowHtml(row) {
  const sep = row.indexOf(': ');
  const id = sep === -1 ? '' : row.slice(0, sep);
  const reason = sep === -1 ? row : row.slice(sep + 2);
  const info = waveNodes[id];
  return `
    <div class="${WAVE_ROW}" data-node-id="${esc(id)}">
      ${info ? statusIcon(info.display) : ''}
      ${info ? kindBadge(info.kind) : ''}
      ${id ? idLink(id, info && info.kind) : ''}
      <span class="flex-1 min-w-0 truncate text-xs leading-4 text-zinc-200" title="${esc(info ? info.title : '')}">${esc(info ? info.title : '')}</span>
      <span class="flex-1 min-w-0 truncate text-right font-mono text-[11px] leading-4 text-zinc-400" title="${esc(reason)}">${esc(reason)}</span>
    </div>
  `;
}

function waveCardHtml(wave, waveNumber, index) {
  const isEmpty = wave.entries.length === 0;
  const heldOpen = (isEmpty && index === 0) !== waveHeldOverrides.has(index);
  const models = new Set(wave.entries.map((e) => e.model));
  const sharedModel = models.size === 1 ? [...models][0] : null;
  const count = wave.entries.length;
  const heldHtml = wave.held.length > 0
    ? `
      <div class="pt-2">
        ${disclosureHeader('Held', wave.held.length, heldOpen, `wave-held-${index}`)}
        <div class="${heldOpen ? '' : 'hidden'}">${wave.held.map(heldRowHtml).join('')}</div>
      </div>`
    : '';
  return `
    <section class="border border-zinc-800 rounded-xl bg-zinc-900/30 overflow-hidden">
      <div class="wave-head flex items-center gap-1.5 px-3 py-2 bg-zinc-900/95 border-b border-zinc-800 font-mono text-xs leading-4">
        <h3 class="font-bold uppercase text-emerald-400">Wave ${waveNumber}</h3>
        <span class="text-zinc-400">· ${count} step${count === 1 ? '' : 's'}${sharedModel ? ` · ${esc(sharedModel)}` : ''}</span>
      </div>
      <div class="wave-body p-2">
        ${isEmpty ? paneState('empty', 'Nothing claimable.') : wave.entries.map((e) => waveRowHtml(e, !sharedModel)).join('')}
        ${heldHtml}
      </div>
    </section>
  `;
}

function controlsHtml() {
  const captionText = waveMaxSize !== null ? `1–${waveMaxSize} (tick_budget)` : '';
  const maxAttr = waveMaxSize !== null ? ` max="${waveMaxSize}"` : '';
  const valueAttr = waveSize !== null ? waveSize : '';
  const sizeBorderCls = waveError ? 'border-red-700' : 'border-zinc-800';
  return `
    <div class="wave-controls flex items-center justify-between gap-3 flex-wrap">
      <div class="flex items-center gap-2 flex-wrap">
        <label class="flex items-center gap-2 text-xs font-medium text-zinc-400">
          <span>Wave size</span>
          <input id="wave-size-input" type="number" min="1"${maxAttr} value="${esc(valueAttr)}" aria-describedby="wave-size-caption" class="h-8 w-16 px-2.5 rounded-lg bg-zinc-950 border ${sizeBorderCls} font-mono text-xs font-normal text-zinc-200 focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500">
        </label>
        <span id="wave-size-caption" class="text-xs font-medium text-zinc-400">${esc(captionText)}</span>
      </div>
      <button id="wave-reset-btn" type="button" class="h-7 px-2.5 rounded-md text-[11px] font-medium bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-zinc-200 transition focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400">Reset</button>
    </div>
  `;
}

function footerHtml() {
  const disabled = waveComputeDisabled();
  return `
    <div class="wave-footer flex items-center">
      <button id="wave-compute-btn" type="button" ${disabled ? 'disabled' : ''} class="h-8 px-3 rounded-lg text-xs font-semibold transition bg-emerald-600 hover:bg-emerald-500 text-black focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400 disabled:opacity-40 disabled:cursor-not-allowed">Compute next wave</button>
    </div>
  `;
}

function wavesHtml() {
  if (waveError) return `${controlsHtml()}${paneState('error', waveError, fetchWaves)}`;
  if (waveLoading && waveData.length === 0) return `${controlsHtml()}${paneState('loading')}${footerHtml()}`;
  return `
    ${controlsHtml()}
    <div class="space-y-4">${waveData.map((w, i) => waveCardHtml(w, i + 1, i)).join('')}</div>
    ${footerHtml()}
  `;
}

function wireWavesHandlers(root) {
  const sizeInput = root.querySelector('#wave-size-input');
  if (sizeInput) {
    sizeInput.addEventListener('change', () => onWaveSizeChange(sizeInput.value));
  }
  const resetBtn = root.querySelector('#wave-reset-btn');
  if (resetBtn) resetBtn.addEventListener('click', resetWaves);
  const computeBtn = root.querySelector('#wave-compute-btn');
  if (computeBtn) computeBtn.addEventListener('click', computeNextWave);
  root.querySelectorAll('.disclosure[data-group-id^="wave-held-"]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const idx = Number(btn.getAttribute('data-group-id').slice('wave-held-'.length));
      if (waveHeldOverrides.has(idx)) waveHeldOverrides.delete(idx);
      else waveHeldOverrides.add(idx);
      renderWaves();
    });
  });
  // A row's own id link and tooltips keep their clicks; anywhere else on the row opens it.
  root.querySelectorAll('.wave-row').forEach((row) => {
    row.addEventListener('click', (e) => {
      const id = row.getAttribute('data-node-id');
      if (id && !e.target.closest('a, [data-tip]')) openNode(id);
    });
  });
}

function renderWaves() {
  const content = document.getElementById('waves-content');
  if (!content) return;
  content.innerHTML = wavesHtml();
  wireWavesHandlers(content);
}
