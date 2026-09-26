// Waves view: `tm wave discover`'s own choice, simulated forward from live state and
// rendered as one card per wave. Deliberately self-contained (its own fetch, its own render
// target #waves-content) rather than routed through core.js's api()/scheduleRender(): the
// only globals it leans on are the ones every other view already shares -- esc/renderIcon/
// getTheme/setWavesLoadPending/isStaticMode (core.js), filters.specMode (filters.js),
// showGraphInspector (detail.js) and window.tmStore.

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
  // The one chokepoint every wave request routes through (init, retry, size change, compute,
  // reset, and a filters.js refetch) -- a static export has no /api/waves behind any of them.
  if (isStaticMode) return;
  const seq = ++waveRequestSeq;
  waveLoading = true;
  waveError = null;
  setWavesLoadPending(true);
  renderWaves();
  // The one bound the client already knows before ever asking the server: an out-of-range
  // size would otherwise reach /api/waves, and its 400 response logs a browser console
  // error no amount of catching in JS can silence.
  if (waveSize !== null && waveMaxSize !== null && (waveSize < 1 || waveSize > waveMaxSize)) {
    waveData = [];
    waveError = `Could not compute waves: wave size must be 1–${waveMaxSize} (this project's dispatch.tick_budget).`;
    waveLoading = false;
    setWavesLoadPending(false);
    renderWaves();
    return;
  }
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
  const last = waveData[waveData.length - 1];
  return !!last && last.entries.length === 0;
}

async function initWaves() {
  // A static export has no server behind /api/meta or /api/waves, and main.js removes its
  // Waves toggle, so there is nothing here to render or ask.
  if (isStaticMode) return;
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

// The frame's action chip is the lowercase action word itself, no icon -- unlike every
// other status-coloured chip on the page (statusChip/phaseChip in core.js), which do carry
// one.
function actionChip(action) {
  const code = ACTION_STATUS_CODE[action] || 'STALE';
  const t = getTheme(code);
  return `<span class="st-chip st-${t.code} inline-flex items-center px-1.5 py-0.5 rounded-full font-medium text-[10px]" title="${esc(t.description)}">${esc(action)}</span>`;
}

// A wave entry's status_before/status_after are the raw Status a real claim would carry
// (core.status.Status), not a DisplayStatus -- IMPLEMENTED/REVIEWED/FIXED fold to whichever
// waiting status their own next action would show, same as a real node's display would.
const WAVE_STATUS_DISPLAY = {
  IMPLEMENTED: 'WAITING_REVIEW',
  REVIEWED: 'WAITING_MERGE',
  FIXED: 'WAITING_REVIEW',
};

// The frame labels a from/to chip with the raw status word itself (e.g. "Implemented"), in
// the colour of the display status it folds to for a waiting one -- unlike statusChip()
// (core.js), which would show that display status's own label ("Waiting Review") and icon.
function waveStatusChip(rawStatus) {
  const t = getTheme(WAVE_STATUS_DISPLAY[rawStatus] || rawStatus);
  const label = rawStatus.charAt(0) + rawStatus.slice(1).toLowerCase();
  return `<span class="st-chip st-${t.code} inline-flex items-center px-1.5 py-0.5 rounded-full font-medium text-[10px]">${esc(label)}</span>`;
}

function heldRowHtml(row) {
  const sep = row.indexOf(': ');
  const id = sep === -1 ? '' : row.slice(0, sep);
  const reason = sep === -1 ? row : row.slice(sep + 2);
  // At 375 the frame stacks id over reason instead of truncating the reason on one row --
  // a long "waits on ..." list needs the room, and the rest of the page keeps this row's
  // truncation from sm up, where it fits.
  return `
    <div class="flex flex-col gap-0.5 sm:flex-row sm:items-center sm:gap-3 p-2 bg-zinc-950/60">
      <span class="text-zinc-300 flex-shrink-0">${esc(id)}</span>
      <span class="text-zinc-400 sm:truncate">${esc(reason)}</span>
    </div>
  `;
}

function waveEntryHtml(entry) {
  const pill = 'px-1.5 py-0.5 rounded border font-mono text-[10px]';
  const inFlight = entry.in_flight ? `<span class="${pill} bg-zinc-800 border-zinc-700 text-zinc-300">in flight</span>` : '';
  const repoPills = entry.repos.map((r) => `<span class="${pill} bg-zinc-900 border-zinc-800 text-cyan-400">${esc(r)}</span>`).join('');
  const pillsInner = `${inFlight}<span class="${pill} bg-purple-950/60 border-purple-800/80 text-purple-300">${esc(entry.model)}</span>${repoPills}`;
  // The frame moves this row from the header band (1440, 768) into the body, under the
  // title (375) -- rendered twice, each half hidden by the page's own `sm` breakpoint,
  // rather than reflowed with CSS alone.
  return `
    <button type="button" class="wave-entry-card block w-full text-left border border-zinc-800/80 rounded-lg bg-zinc-950/40 overflow-hidden hover:border-zinc-700 transition focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500" data-node-id="${esc(entry.id)}" aria-label="${esc(entry.id)}: ${esc(entry.title)}">
      <div class="wave-entry-head flex items-center justify-between gap-2 flex-wrap px-3 py-2.5 bg-zinc-900/90">
        <div class="flex items-center gap-2 min-w-0">
          <span class="font-mono text-xs font-bold text-emerald-400 truncate">${esc(entry.id)}</span>
          ${actionChip(entry.action)}
        </div>
        <div class="hidden sm:flex items-center gap-1.5 flex-wrap">${pillsInner}</div>
      </div>
      <div class="wave-entry-body px-3.5 py-3 space-y-2.5 bg-zinc-950/80 border-t border-zinc-800/60">
        <div class="text-sm font-medium text-zinc-200 break-words">${esc(entry.title)}</div>
        <div class="flex sm:hidden items-center gap-1.5 flex-wrap">${pillsInner}</div>
        <div class="flex items-center gap-1.5 flex-wrap">
          ${waveStatusChip(entry.status_before)}
          <span class="text-xs text-zinc-500">→</span>
          ${waveStatusChip(entry.status_after)}
        </div>
      </div>
    </button>
  `;
}

function waveCardHtml(wave, waveNumber, index) {
  const isEmpty = wave.entries.length === 0;
  const heldOpen = isEmpty !== waveHeldOverrides.has(index);
  const heldHtml = wave.held.length > 0
    ? `
      <div class="space-y-1.5">
        <button type="button" class="wave-held-toggle w-full flex items-center justify-between text-[11px] font-semibold uppercase tracking-wider text-zinc-400 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded" data-wave-index="${index}" aria-expanded="${heldOpen}">
          <span>Held (${wave.held.length})</span>${renderIcon(heldOpen ? 'chevron-down' : 'chevron-right', 'w-3 h-3')}
        </button>
        <div class="wave-held-rows border border-zinc-800 rounded-lg overflow-hidden divide-y divide-zinc-800 font-mono text-[11px] ${heldOpen ? '' : 'hidden'}">${wave.held.map(heldRowHtml).join('')}</div>
      </div>`
    : '';
  return `
    <section class="border border-zinc-800 rounded-xl bg-zinc-900/30 overflow-hidden">
      <div class="wave-head flex items-center justify-between px-4 py-3.5 bg-zinc-900/95 border-b border-zinc-800 font-mono text-xs">
        <h3 class="font-bold uppercase text-emerald-400">Wave ${waveNumber}</h3>
        <span class="text-zinc-400">${wave.entries.length} task${wave.entries.length === 1 ? '' : 's'}</span>
      </div>
      <div class="p-4 space-y-2.5">
        ${isEmpty ? '<div class="text-sm text-zinc-400">Nothing claimable.</div>' : wave.entries.map(waveEntryHtml).join('')}
        ${heldHtml}
      </div>
    </section>
  `;
}

function controlsHtml() {
  const captionText = waveMaxSize !== null ? `1–${waveMaxSize} (tick_budget)` : '';
  const maxAttr = waveMaxSize !== null ? ` max="${waveMaxSize}"` : '';
  const valueAttr = waveSize !== null ? waveSize : '';
  // A size-range refusal is the one error the input itself caused, so it wears the refusal
  // rather than only the pane-level alert above it (same border/text pair as toast's error tone).
  const sizeBorderCls = waveError ? 'border-red-700' : 'border-zinc-800';
  const sizeTextCls = waveError ? 'text-red-200' : 'text-zinc-200';
  return `
    <div class="wave-controls flex items-center justify-between gap-3 flex-wrap">
      <div class="flex items-center gap-2 flex-wrap">
        <label class="flex items-center gap-2 text-xs text-zinc-300">
          <span>Wave size</span>
          <input id="wave-size-input" type="number" min="1"${maxAttr} value="${esc(valueAttr)}" aria-describedby="wave-size-caption" class="h-8 w-16 px-2.5 rounded-lg bg-zinc-950 border ${sizeBorderCls} text-xs ${sizeTextCls} focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500">
        </label>
        <span id="wave-size-caption" class="text-xs font-medium text-zinc-400">${esc(captionText)}</span>
      </div>
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
    // No retry control: the size input that caused the refusal already refetches on its
    // own `change` event, and the frame draws no button here.
    return `
      ${controlsHtml()}
      <div role="alert" class="border border-red-800/60 rounded-lg bg-red-950/40 text-red-200 text-xs p-4">
        <span>${esc(waveError)}</span>
      </div>
    `;
  }
  if (waveLoading && waveData.length === 0) {
    // The pending cue itself is the shared #load-indicator bar (setWavesLoadPending, above);
    // this is only the content pane's placeholder for the stretch before any wave has ever
    // rendered here -- shaped like a real wave card, per the loading frame.
    return `
      ${controlsHtml()}
      <section class="border border-zinc-800 rounded-xl bg-zinc-900/30 overflow-hidden">
        <div class="wave-head flex items-center justify-between px-4 py-3.5 bg-zinc-900/95 border-b border-zinc-800 font-mono text-xs">
          <h3 class="font-bold uppercase text-emerald-400">Wave 1</h3>
          <span class="text-zinc-400">&hellip;</span>
        </div>
        <div class="p-4">
          <div class="text-xs text-zinc-500 italic">Loading&hellip;</div>
        </div>
      </section>
      ${footerHtml()}
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
