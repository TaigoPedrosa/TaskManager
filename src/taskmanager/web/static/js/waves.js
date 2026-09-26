// Waves view: `tm wave discover`'s own choice, simulated forward from live state and
// rendered as one card per wave. Deliberately self-contained (its own fetch, its own render
// target #waves-content) rather than routed through core.js's api()/scheduleRender(): the
// only globals it leans on are the ones every other view already shares -- esc/renderIcon/
// getTheme (core.js), filters.specMode (filters.js), showGraphInspector (detail.js) and
// window.tmStore.

let waveDepth = 1;
let waveSize = null;
let waveMaxSize = null;
let waveData = [];
let waveLoading = false;
let waveError = null;
// Indices whose Held section the user toggled away from its default (open when the card
// itself has no entries -- "Nothing claimable.", collapsed otherwise).
let waveHeldOverrides = new Set();
let waveRequestSeq = 0;
let waveRefetchScheduled = false;

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
    throw new Error(detail || `GET ${path} failed (${res.status})`);
  }
  return res.json();
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
  const seq = ++waveRequestSeq;
  waveLoading = true;
  waveError = null;
  renderWaves();
  const params = new URLSearchParams({ depth: String(waveDepth) });
  if (waveSize !== null) params.set('size', String(waveSize));
  waveSpecFilter().forEach((s) => params.append('spec', s));
  try {
    const res = await getJson(`/api/waves?${params}`);
    if (seq !== waveRequestSeq) return; // superseded by a later request
    waveData = res.waves;
  } catch (e) {
    if (seq !== waveRequestSeq) return;
    waveData = [];
    waveError = e.message;
  } finally {
    if (seq === waveRequestSeq) waveLoading = false;
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
  const last = waveData[waveData.length - 1];
  return !!last && last.entries.length === 0;
}

async function initWaves() {
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

const ACTION_STATUS_CODE = { implement: 'IMPLEMENTING', review: 'REVIEWING', fix: 'FIXING', merge: 'MERGING' };

function actionChip(action) {
  const code = ACTION_STATUS_CODE[action] || 'STALE';
  const t = getTheme(code);
  const label = action.charAt(0).toUpperCase() + action.slice(1);
  return `<span class="st-chip st-${t.code} inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full font-medium text-[10px]" title="${esc(t.description)}">${renderIcon(t.icon, 'w-3 h-3')}<span>${esc(label)}</span></span>`;
}

// A wave entry's status_before/status_after are the raw Status a real claim would carry
// (core.status.Status), not a DisplayStatus -- IMPLEMENTED/REVIEWED/FIXED fold to whichever
// waiting status their own next action would show, same as a real node's display would.
const WAVE_STATUS_DISPLAY = {
  IMPLEMENTED: 'WAITING_REVIEW',
  REVIEWED: 'WAITING_MERGE',
  FIXED: 'WAITING_REVIEW',
};

function waveStatusChip(rawStatus) {
  return statusChip(WAVE_STATUS_DISPLAY[rawStatus] || rawStatus);
}

function heldRowHtml(row) {
  const sep = row.indexOf(': ');
  const id = sep === -1 ? '' : row.slice(0, sep);
  const reason = sep === -1 ? row : row.slice(sep + 2);
  return `
    <div class="flex items-start gap-2 text-[11px] text-zinc-400 px-1">
      <span class="font-mono text-zinc-300 flex-shrink-0">${esc(id)}</span>
      <span class="truncate">${esc(reason)}</span>
    </div>
  `;
}

function waveEntryHtml(entry) {
  const repoPills = entry.repos.map((r) => `<span class="px-1.5 py-0.5 rounded bg-zinc-900 border border-zinc-800 text-zinc-400 font-mono text-[10px]">${esc(r)}</span>`).join('');
  const inFlight = entry.in_flight
    ? '<span class="px-1.5 py-0.5 rounded bg-zinc-900 border border-zinc-800 text-zinc-400 text-[10px]">in flight</span>'
    : '';
  return `
    <button type="button" class="wave-entry-card w-full text-left border border-zinc-800/80 rounded-lg bg-zinc-900/60 hover:border-zinc-700 hover:bg-zinc-900 transition p-2.5 space-y-1.5 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500" data-node-id="${esc(entry.id)}" aria-label="${esc(entry.id)}: ${esc(entry.title)}">
      <div class="flex items-center gap-1.5 flex-wrap">
        <span class="font-mono text-xs font-semibold text-emerald-400">${esc(entry.id)}</span>
        ${actionChip(entry.action)}
        <span class="px-1.5 py-0.5 rounded bg-purple-950/60 text-purple-300 border border-purple-800/80 font-mono text-[10px]">${esc(entry.model)}</span>
        ${repoPills}
        ${inFlight}
      </div>
      <div class="text-xs text-zinc-200 truncate">${esc(entry.title)}</div>
      <div class="flex items-center gap-1.5">
        ${waveStatusChip(entry.status_before)}
        ${renderIcon('chevron-right', 'w-3 h-3 text-zinc-600')}
        ${waveStatusChip(entry.status_after)}
      </div>
    </button>
  `;
}

function waveCardHtml(wave, waveNumber, index) {
  const isEmpty = wave.entries.length === 0;
  const heldOpen = isEmpty !== waveHeldOverrides.has(index);
  const heldHtml = wave.held.length > 0
    ? `
      <div class="pt-2 border-t border-zinc-800/60">
        <button type="button" class="wave-held-toggle flex items-center gap-1.5 text-[11px] text-zinc-400 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded" data-wave-index="${index}" aria-expanded="${heldOpen}">
          ${renderIcon(heldOpen ? 'chevron-down' : 'chevron-right', 'w-3 h-3')}<span>Held (${wave.held.length})</span>
        </button>
        <div class="mt-1.5 space-y-1 ${heldOpen ? '' : 'hidden'}">${wave.held.map(heldRowHtml).join('')}</div>
      </div>`
    : '';
  return `
    <div class="border border-zinc-800/80 rounded-lg bg-zinc-950/40 p-3.5 space-y-2.5">
      <div class="flex items-center justify-between">
        <h3 class="text-sm font-semibold text-zinc-100">Wave ${waveNumber}</h3>
        <span class="text-xs text-zinc-400">${wave.entries.length} task${wave.entries.length === 1 ? '' : 's'}</span>
      </div>
      ${isEmpty ? '<div class="text-xs text-zinc-500 italic py-2">Nothing claimable.</div>' : `<div class="space-y-2">${wave.entries.map(waveEntryHtml).join('')}</div>`}
      ${heldHtml}
    </div>
  `;
}

function controlsHtml() {
  const captionText = waveMaxSize !== null ? `1–${waveMaxSize} (tick_budget)` : '';
  const maxAttr = waveMaxSize !== null ? ` max="${waveMaxSize}"` : '';
  const valueAttr = waveSize !== null ? waveSize : '';
  return `
    <div class="flex items-center gap-3 flex-wrap">
      <label class="flex items-center gap-2 text-xs text-zinc-300">
        <span>Wave size</span>
        <input id="wave-size-input" type="number" min="1"${maxAttr} value="${esc(valueAttr)}" aria-describedby="wave-size-caption" class="h-8 w-20 px-2.5 rounded-lg bg-zinc-950 border border-zinc-800 text-xs text-zinc-200 focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500">
      </label>
      <span id="wave-size-caption" class="text-[11px] text-zinc-500">${esc(captionText)}</span>
      <button id="wave-reset-btn" type="button" class="h-8 px-3 rounded-lg text-xs font-medium bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-zinc-300 hover:text-white transition focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500">Reset</button>
    </div>
  `;
}

function footerHtml() {
  const last = waveData[waveData.length - 1];
  const lastEmpty = !!last && last.entries.length === 0;
  const disabled = waveComputeDisabled();
  return `
    <div class="flex items-center gap-3">
      <button id="wave-compute-btn" type="button" ${disabled ? 'disabled' : ''} class="h-9 px-4 rounded-lg text-xs font-semibold transition bg-emerald-600 hover:bg-emerald-500 text-black focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400 disabled:opacity-40 disabled:cursor-not-allowed">Compute next wave</button>
      ${lastEmpty && !waveLoading ? '<span class="text-xs text-zinc-400">The last wave is empty.</span>' : ''}
    </div>
  `;
}

function wavesHtml() {
  if (waveError) {
    return `
      ${controlsHtml()}
      <div role="alert" class="border border-red-800/60 rounded-lg bg-red-950/40 text-red-200 text-xs p-4 flex items-center justify-between gap-3">
        <span>${esc(waveError)}</span>
        <button id="wave-retry-btn" type="button" class="underline decoration-dotted flex-shrink-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-400 rounded">Retry</button>
      </div>
    `;
  }
  if (waveLoading && waveData.length === 0) {
    return `
      ${controlsHtml()}
      <div class="border border-zinc-800/80 rounded-lg bg-zinc-950/40 p-6 space-y-3">
        <div class="h-0.5 bg-emerald-500 animate-pulse rounded-full"></div>
        <div class="text-xs text-zinc-400 text-center py-4">Loading&hellip;</div>
      </div>
    `;
  }
  return `
    ${controlsHtml()}
    <div class="space-y-4">${waveData.map((w, i) => waveCardHtml(w, i + 1, i)).join('')}</div>
    ${footerHtml()}
  `;
}

function wireWavesHandlers(root) {
  const sizeInput = root.querySelector('#wave-size-input');
  if (sizeInput) {
    sizeInput.addEventListener('change', () => {
      const n = Number(sizeInput.value);
      if (Number.isFinite(n) && n > 0) onWaveSizeChange(n);
    });
  }
  const resetBtn = root.querySelector('#wave-reset-btn');
  if (resetBtn) resetBtn.addEventListener('click', resetWaves);
  const computeBtn = root.querySelector('#wave-compute-btn');
  if (computeBtn) computeBtn.addEventListener('click', computeNextWave);
  const retryBtn = root.querySelector('#wave-retry-btn');
  if (retryBtn) retryBtn.addEventListener('click', fetchWaves);
  root.querySelectorAll('.wave-held-toggle').forEach((btn) => {
    btn.addEventListener('click', () => {
      const idx = Number(btn.getAttribute('data-wave-index'));
      if (waveHeldOverrides.has(idx)) waveHeldOverrides.delete(idx);
      else waveHeldOverrides.add(idx);
      renderWaves();
    });
  });
  root.querySelectorAll('.wave-entry-card').forEach((btn) => {
    btn.addEventListener('click', () => showGraphInspector(btn.getAttribute('data-node-id')));
  });
}

function renderWaves() {
  const content = document.getElementById('waves-content');
  if (!content) return;
  content.innerHTML = wavesHtml();
  wireWavesHandlers(content);
}

initWaves();
