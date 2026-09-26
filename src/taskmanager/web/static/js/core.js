// State Management
let graphData = { nodes: [], edges: [] };
let selectedNodeId = null;
let visNodesDS = null;
let currentMode = window.VIEW_MODES.WAVES;
let networkInstance = null;
let isStaticMode = typeof window.STATIC_DATA !== 'undefined';
// A node's expand/collapse state now lives in the store's open and watch sets (open reveals
// a container's children, watch fetches a node's own body); this set is this view's own record
// of which ids it asked the store to expand, since the store exposes no getter for either set.
const expandedIds = new Set();
const expandedSections = new Set();
// Group headers ("Tasks (N)", "Sections (N)") are a third, independent collapse level:
// they hide a plan's task-card list or a section list's row of <details> summaries
// without touching expandedIds (the plan/task body) or expandedSections (a section's
// own open state). A group id's default (collapsed or not) varies by group type, so this
// set stores only ids whose state differs from their default; see groupCollapsed().
// Session-only, never persisted, same as the two sets above.
const collapsedGroups = new Set();


// DOM Elements
const graphPane = document.getElementById('graph-pane');
const sidebarPane = document.getElementById('sidebar-pane');
const treeList = document.getElementById('tree-list');
const searchBox = document.getElementById('search-box');
const statsDigest = document.getElementById('stats-digest');
const viewDocBtn = document.getElementById('view-doc-btn');
const viewGraphBtn = document.getElementById('view-graph-btn');
const graphFitBtn = document.getElementById('graph-fit-btn');
const refreshBtn = document.getElementById('refresh-btn');
const expandAllBtn = document.getElementById('expand-all-btn');
const sidebarResizeHandle = document.getElementById('sidebar-resize-handle');
const graphInspector = document.getElementById('graph-inspector');
const networkCanvas = document.getElementById('network-canvas');
const graphFitWrap = document.getElementById('graph-fit-wrap');
const wavesPane = document.getElementById('waves-pane');
const inspectorCloseBtn = document.getElementById('inspector-close-btn');
const brandIcon = document.getElementById('brand-icon');
const brandIconTitle = document.getElementById('brand-icon-title');
const repoFilter = document.getElementById('repo-filter');
const modelFilterEl = document.getElementById('model-filter');
const specFilterEl = document.getElementById('spec-filter');
const phaseFilterEl = document.getElementById('phase-filter');
const scoreFilterEl = document.getElementById('score-filter');
const clearFiltersBtn = document.getElementById('clear-filters-btn');
const filtersToggleBtn = document.getElementById('filters-toggle-btn');
const filterControlsGroup = document.getElementById('filter-controls-group');
const legendBtn = document.getElementById('legend-btn');
const legendPanel = document.getElementById('legend-panel');
const legendBody = document.getElementById('legend-body');
const legendCloseBtn = document.getElementById('legend-close-btn');
const toolbarActions = document.getElementById('toolbar-actions');
const loadIndicator = document.getElementById('load-indicator');
const dialogRoot = document.getElementById('dialog-root');
const toastRoot = document.getElementById('toast-root');
toastRoot.className = 'fixed bottom-4 right-4 z-50 flex flex-col items-end gap-2 pointer-events-none';

// Sidebar width: Graph view only, drag-resizable, remembered per browser.
const SIDEBAR_MIN_WIDTH = 200;
const SIDEBAR_MAX_WIDTH = 560;
const SIDEBAR_DEFAULT_WIDTH = 320;

function loadSidebarWidth() {
  try {
    const saved = Number(localStorage.getItem('tm-sidebar-width'));
    if (saved >= SIDEBAR_MIN_WIDTH && saved <= SIDEBAR_MAX_WIDTH) return saved;
  } catch (e) {
    console.error('Could not read the saved sidebar width:', e);
  }
  return SIDEBAR_DEFAULT_WIDTH;
}

function setSidebarWidth(px) {
  sidebarPane.style.width = `${px}px`;
}

setSidebarWidth(loadSidebarWidth());

sidebarResizeHandle.addEventListener('mousedown', (e) => {
  e.preventDefault();
  const startX = e.clientX;
  const startWidth = sidebarPane.getBoundingClientRect().width;

  function onMove(moveEvent) {
    const next = Math.min(
      SIDEBAR_MAX_WIDTH,
      Math.max(SIDEBAR_MIN_WIDTH, startWidth + (moveEvent.clientX - startX))
    );
    setSidebarWidth(next);
  }

  function onUp() {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    try {
      localStorage.setItem('tm-sidebar-width', String(sidebarPane.getBoundingClientRect().width));
    } catch (e) {
      console.error('Could not persist the sidebar width:', e);
    }
  }

  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
});


function renderIcon(iconName, classes = 'w-4 h-4') {
  return `<svg class="${classes}" fill="none" stroke="currentColor"><use href="#icon-${iconName}"></use></svg>`;
}

function getTheme(status) {
  return window.STATUS_THEMES[status] || { ...window.STATUS_THEMES.STALE, label: String(status) };
}

// What a reader sees for a node: the derived display, else (a decision) its own status.
function displayOf(n) {
  return n.display || n.status;
}

function phaseChip(code, size = 'text-[10px]') {
  const t = window.PHASE_THEMES[code];
  if (!t) return '';
  return `<span class="st-chip ph-${t.code} inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full font-medium ${size}" title="${esc(t.description)}">${renderIcon(t.icon, 'w-3 h-3')}<span>${esc(t.label)}</span></span>`;
}


// Mode Switching. The sidebar is a graph-view tool for jumping to a node; it takes
// no space in Waves view so that pane reads at its own full width.
// The base shape (h-full aspect-square, matching index.html's own markup) stays fixed;
// only the active/inactive colour classes toggle. Reassigning the whole className to a
// differently-shaped string (px-3 py-1.5, no aspect-square) on the first switch was what
// changed the button's size -- every call after the first kept perpetuating that wrong shape.
const VIEW_BTN_BASE = 'h-full aspect-square flex items-center justify-center rounded-md font-medium transition';
const VIEW_BTN_ACTIVE = `${VIEW_BTN_BASE} bg-zinc-800 text-white shadow-sm`;
const VIEW_BTN_INACTIVE = `${VIEW_BTN_BASE} text-zinc-400 hover:text-white`;

function setViewMode(mode) {
  currentMode = mode;
  wavesPane.classList.toggle('hidden', mode !== window.VIEW_MODES.WAVES);
  networkCanvas.classList.toggle('hidden', mode !== window.VIEW_MODES.GRAPH);
  graphFitWrap.classList.toggle('hidden', mode !== window.VIEW_MODES.GRAPH);
  if (mode === window.VIEW_MODES.WAVES) {
    // The waves button reuses the toolbar's original view-switcher slot, so it lights up
    // the same way that slot always has.
    viewDocBtn.className = VIEW_BTN_ACTIVE;
    viewGraphBtn.className = VIEW_BTN_INACTIVE;
    graphPane.classList.remove('hidden');
    sidebarPane.classList.add('hidden');
  } else {
    viewGraphBtn.className = VIEW_BTN_ACTIVE;
    viewDocBtn.className = VIEW_BTN_INACTIVE;
    graphPane.classList.remove('hidden');
    sidebarPane.classList.remove('hidden');
    if (networkInstance) {
      setTimeout(() => networkInstance.fit(), 50);
    }
  }
}

viewDocBtn.addEventListener('click', () => setViewMode(window.VIEW_MODES.WAVES));
viewGraphBtn.addEventListener('click', () => setViewMode(window.VIEW_MODES.GRAPH));
graphFitBtn.addEventListener('click', () => networkInstance && networkInstance.fit());
inspectorCloseBtn.addEventListener('click', () => graphInspector.classList.add('hidden'));
refreshBtn.addEventListener('click', () => window.tmStore.resync());


// Expand / Collapse All. A click opens every visible, still-collapsed container one level
// further (the next level's rows arrive once the store answers); once nothing visible is
// left to open, the same button collapses back to the roots. Unlike the old whole-tree
// walk, this never touches a row the store hasn't sent yet -- there is nothing else to walk.
expandAllBtn.addEventListener('click', () => {
  const containers = [...window.tmStore.rows.values()].filter(r => r.kind !== 'task');
  const toOpen = containers.filter(r => !expandedIds.has(r.id)).map(r => r.id);
  if (toOpen.length > 0) {
    toOpen.forEach(id => expandedIds.add(id));
    window.tmStore.open(toOpen);
    window.tmStore.watch(toOpen);
  } else {
    const roots = [...window.tmStore.rows.values()].filter(r => r.parent === null).map(r => r.id);
    window.tmStore.close(roots);
    window.tmStore.unwatch([...expandedIds]);
    expandedIds.clear();
  }
});


function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

// Shared by every small absolutely-positioned popover (the tri-state filter popovers, the +
// New menu): a right-0-anchored popup overflows off-screen when its trigger sits near the
// left edge of a narrow viewport, and a left-0-anchored one does the same near the right
// edge. Called after the popup is shown (so getBoundingClientRect reads real geometry), it
// flips the anchor only when the popup actually overflows either edge.
function clampToViewport(el, margin = 8) {
  el.style.left = '';
  el.style.right = '';
  let rect = el.getBoundingClientRect();
  if (rect.right > window.innerWidth - margin) {
    el.style.left = 'auto';
    el.style.right = '0px';
    rect = el.getBoundingClientRect();
  }
  if (rect.left < margin) {
    el.style.left = `${margin}px`;
    el.style.right = 'auto';
  }
}

function statusChip(code, size = 'text-[10px]') {
  const t = getTheme(code);
  return `<span class="st-chip st-${t.code} inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full font-medium ${size}" title="${esc(t.description)}">${renderIcon(t.icon, 'w-3 h-3')}<span>${esc(t.label)}</span></span>`;
}

function statusIcon(code, size = 'w-3.5 h-3.5') {
  const t = getTheme(code);
  return `<span class="st-text st-${t.code} flex-shrink-0" title="${esc(t.label)}">${renderIcon(t.icon, size)}</span>`;
}

// Copy-id button: "(icon ID)", wired via attachCopyHandlers so it works after any
// re-render; stopPropagation keeps it from also toggling the card it sits on.
function copyIdButton(id) {
  return `
    <button class="copy-id-btn flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono text-zinc-400 hover:text-white hover:bg-zinc-800 transition flex-shrink-0" data-copy-id="${esc(id)}" title="Copy ID: ${esc(id)}" aria-label="Copy ID ${esc(id)}">
      ${renderIcon('copy', 'w-3 h-3')}<span>ID</span>
    </button>
  `;
}

function attachCopyHandlers(root) {
  root.querySelectorAll('.copy-id-btn').forEach(btn => {
    btn.onclick = (e) => {
      e.stopPropagation();
      const id = btn.getAttribute('data-copy-id');
      navigator.clipboard.writeText(id).then(() => {
        const original = btn.innerHTML;
        btn.innerHTML = `${renderIcon('check', 'w-3 h-3')}<span>Copied</span>`;
        setTimeout(() => { if (btn.isConnected) btn.innerHTML = original; }, 1200);
      }).catch(err => console.error('Could not copy id:', err));
    };
  });
}

// A tree row carries exactly one status marker: a coloured dot, name in the tooltip only.
function statusDot(code) {
  const t = getTheme(code);
  return `<span class="st-dot st-${t.code} inline-block w-2.5 h-2.5 rounded-full flex-shrink-0" title="${esc(t.label)}"></span>`;
}

// Progress: per-display-status counts from the store's counts tree (statuses), not a
// per-node field -- a spec's or plan's bar shows before its own body is ever fetched.
// Only COMPLETED counts as done; a set-aside status (deferred/abandoned/superseded) can
// never finish, so it is dropped from `total` but kept in `counts` for the full breakdown.
const SET_ASIDE_DISPLAYS = new Set(['DEFERRED', 'ABANDONED', 'SUPERSEDED']);

function progressParts(counts) {
  const totalAll = Object.values(counts).reduce((a, b) => a + b, 0);
  const setAside = Object.entries(counts).reduce((a, [c, n]) => a + (SET_ASIDE_DISPLAYS.has(c) ? n : 0), 0);
  const codes = Object.keys(window.STATUS_THEMES).filter(c => counts[c]);
  return { total: totalAll - setAside, completed: counts.COMPLETED || 0, codes, counts };
}

function progressText(counts) {
  const p = progressParts(counts);
  if (p.total === 0) return 'No tasks';
  return p.codes.map(c => `${p.counts[c]} ${getTheme(c).label.toLowerCase()}`).join(' · ');
}

function progressBar(counts, height = 'h-1.5') {
  const p = progressParts(counts);
  const text = esc(progressText(counts));
  const segs = p.codes.map(c => `<span class="st-seg st-${c}" style="width:${(p.counts[c] / p.total) * 100}%"></span>`).join('');
  return `<div class="flex w-full ${height} rounded-full overflow-hidden bg-zinc-800" role="img" aria-label="${text}" title="${text}">${segs}</div>`;
}

// Reads the store's counts tree for one spec's or one plan's bar (§ "The counts tree" in the
// plan's parent context): a spec's own entry sums every one of its plans' counts, a plan
// reads its own entry directly. Neither exists in the tree until the store's first snapshot.
function specStatusEntry(specId) {
  return window.tmStore.statuses.find(s => s.spec === specId) || null;
}

function countsForRow(row) {
  if (row.kind === 'spec') {
    const entry = specStatusEntry(row.id);
    if (!entry) return {};
    const totals = {};
    entry.plans.forEach(p => Object.entries(p.counts).forEach(([k, n]) => { totals[k] = (totals[k] || 0) + n; }));
    return totals;
  }
  if (row.kind === 'plan') {
    const entry = specStatusEntry(row.parent);
    if (!entry) return {};
    const planEntry = entry.plans.find(p => p.plan === row.id);
    return planEntry ? planEntry.counts : {};
  }
  return {};
}

// A section's content is routinely imported straight from a markdown document, so its
// own body can start with the same "## <Header>" line the summary already shows on its
// own -- rendered raw, that reads as the header appearing twice. Drop the leading heading
// line (and any blank line after it) only when it matches the label already displayed.

// True once server-backed writes exist; a static export (window.STATIC_DATA) is always read-only.
function canEdit() {
  return !isStaticMode;
}

// One JSON round trip for every write route: throws with the server's own `detail` message
// (OperationError's text, per the write guard's refusal contract) so a caller can show it
// verbatim in a toast rather than a generic "request failed".
async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new Error('Network error: could not reach the server.');
  }
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch (e) { /* non-JSON body */ }
  }
  if (!res.ok) {
    throw new Error((data && data.detail) || `${method} ${path} failed (${res.status})`);
  }
  return data;
}

const TOAST_TONE = {
  info: 'bg-zinc-800 border-zinc-700 text-zinc-100',
  success: 'bg-emerald-950 border-emerald-700 text-emerald-200',
  error: 'bg-red-950 border-red-700 text-red-200'
};

function toast(message, tone = 'info') {
  const el = document.createElement('div');
  el.className = `pointer-events-auto px-3 py-2 rounded-lg border text-xs shadow-2xl max-w-sm transition-opacity ${TOAST_TONE[tone] || TOAST_TONE.info}`;
  el.setAttribute('role', tone === 'error' ? 'alert' : 'status');
  el.textContent = message;
  toastRoot.appendChild(el);
  setTimeout(() => {
    el.classList.add('opacity-0');
    setTimeout(() => el.remove(), 200);
  }, 3500);
}

// Live state lives on the brand icon's own colour -- no separate connection chip.

function setBrandLive(state) {
  const colors = {
    synced: 'text-emerald-400', disconnected: 'text-red-400', static: 'text-zinc-500'
  };
  const labels = { synced: 'Synced', disconnected: 'Disconnected', static: 'Static Export' };
  brandIcon.className = `w-5 h-5 transition-colors ${colors[state]}`;
  brandIconTitle.textContent = labels[state];
}

// The brand icon follows the store's connection state and the load indicator its `pending`
// flag; both are read straight off the store rather than mirrored into local variables, so
// there is exactly one place either can drift from what the store actually reports.
// Waves' own fetch cycle runs entirely outside the store (waves.js is deliberately
// self-contained), so it feeds the same bar through this flag instead.
let wavesLoadPending = false;
function setWavesLoadPending(pending) {
  wavesLoadPending = pending;
  syncConnectionUi();
}

function syncConnectionUi() {
  setBrandLive(isStaticMode ? 'static' : (window.tmStore.connected ? 'synced' : 'disconnected'));
  loadIndicator.classList.toggle('hidden', !window.tmStore.pending && !wavesLoadPending);
}

// Re-render coalesced to at most once per animation frame: a subscription can update rows,
// statuses, facets and bodies several times in a burst (a single CLI write already fans out
// into more than one item), and re-rendering per store notification re-walks and rebuilds
// the whole document/tree/filters DOM for each one.
let renderScheduled = false;
function scheduleRender() {
  if (renderScheduled) return;
  renderScheduled = true;
  requestAnimationFrame(() => {
    renderScheduled = false;
    renderAll();
  });
}

// window.tmStore is created here, before filters.js/tree.js/main.js run, but with whatever
// filters happen to be in scope at this point (none yet -- readHash() runs later, in main.js).
// That is safe: in live mode the store's own connect() only reaches the network on the
// WebSocket's `onopen`, which cannot fire before this synchronous script pass finishes, so
// main.js's later setFilters() call still lands before the one subscribe frame this page ever
// sends on load. In static mode everything below runs synchronously and gets recomputed again
// once main.js applies the real filters, which is just a local, in-memory pass.
window.tmStore = isStaticMode
  ? createStore({ staticData: window.STATIC_DATA })
  : createStore({});
window.tmStore.onChange((patch) => {
  syncConnectionUi();
  if (patch && patch.error) toast(patch.error, 'error');
  scheduleRender();
});
syncConnectionUi();
