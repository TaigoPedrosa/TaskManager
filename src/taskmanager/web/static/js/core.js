// State Management
let treeData = [];
let graphData = { nodes: [], edges: [] };
let statsData = {};
let selectedNodeId = null;
let visNodesDS = null;
let currentMode = window.VIEW_MODES.DOCUMENT;
let networkInstance = null;
let isStaticMode = typeof window.STATIC_DATA !== 'undefined';
const collapsedNodes = new Set();
const expandedSections = new Set();
// Group headers ("Tasks (N)", "Sections (N)") are a third, independent collapse level:
// they hide a plan's task-card list or a section list's row of <details> summaries
// without touching collapsedNodes (the plan/task body) or expandedSections (a section's
// own open state). A group id's default (collapsed or not) varies by group type, so this
// set stores only ids whose state differs from their default; see groupCollapsed().
// Session-only, never persisted, same as the two sets above.
const collapsedGroups = new Set();
let allSectionIds = [];


// DOM Elements
const documentPane = document.getElementById('document-pane');
const graphPane = document.getElementById('graph-pane');
const sidebarPane = document.getElementById('sidebar-pane');
const unifiedDocument = document.getElementById('unified-document');
const treeList = document.getElementById('tree-list');
const searchBox = document.getElementById('search-box');
const statsDigest = document.getElementById('stats-digest');
const viewDocBtn = document.getElementById('view-doc-btn');
const viewGraphBtn = document.getElementById('view-graph-btn');
const graphFitBtn = document.getElementById('graph-fit-btn');
const refreshBtn = document.getElementById('refresh-btn');
const expandAllBtn = document.getElementById('expand-all-btn');
const toggleSectionsBtn = document.getElementById('toggle-sections-btn');
const sidebarResizeHandle = document.getElementById('sidebar-resize-handle');
const graphInspector = document.getElementById('graph-inspector');
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
// no space in Document view so the document pane reads at its own full width.
// The base shape (h-full aspect-square, matching index.html's own markup) stays fixed;
// only the active/inactive colour classes toggle. Reassigning the whole className to a
// differently-shaped string (px-3 py-1.5, no aspect-square) on the first switch was what
// changed the button's size -- every call after the first kept perpetuating that wrong shape.
const VIEW_BTN_BASE = 'h-full aspect-square flex items-center justify-center rounded-md font-medium transition';
const VIEW_BTN_ACTIVE = `${VIEW_BTN_BASE} bg-zinc-800 text-white shadow-sm`;
const VIEW_BTN_INACTIVE = `${VIEW_BTN_BASE} text-zinc-400 hover:text-white`;

function setViewMode(mode) {
  currentMode = mode;
  if (mode === window.VIEW_MODES.DOCUMENT) {
    viewDocBtn.className = VIEW_BTN_ACTIVE;
    viewGraphBtn.className = VIEW_BTN_INACTIVE;
    documentPane.classList.remove('hidden');
    graphPane.classList.add('hidden');
    sidebarPane.classList.add('hidden');
    toggleSectionsBtn.classList.remove('hidden');
  } else {
    viewGraphBtn.className = VIEW_BTN_ACTIVE;
    viewDocBtn.className = VIEW_BTN_INACTIVE;
    documentPane.classList.add('hidden');
    graphPane.classList.remove('hidden');
    sidebarPane.classList.remove('hidden');
    toggleSectionsBtn.classList.add('hidden');
    if (networkInstance) {
      setTimeout(() => networkInstance.fit(), 50);
    }
  }
}

viewDocBtn.addEventListener('click', () => setViewMode(window.VIEW_MODES.DOCUMENT));
viewGraphBtn.addEventListener('click', () => setViewMode(window.VIEW_MODES.GRAPH));
graphFitBtn.addEventListener('click', () => networkInstance && networkInstance.fit());
inspectorCloseBtn.addEventListener('click', () => graphInspector.classList.add('hidden'));
refreshBtn.addEventListener('click', loadAllData);


// Document sections: default collapsed, remembered for this session only (never persisted),
// so a re-render after a filter change never surprise-collapses one the user just opened.
function updateToggleSectionsButton() {
  const label = expandedSections.size > 0 ? 'Collapse all sections' : 'Expand all sections';
  toggleSectionsBtn.title = label;
  toggleSectionsBtn.setAttribute('aria-label', label);
}

toggleSectionsBtn.addEventListener('click', () => {
  if (expandedSections.size > 0) {
    expandedSections.clear();
  } else {
    allSectionIds.forEach(id => expandedSections.add(id));
  }
  renderUnifiedDocument();
});


// Expand / Collapse All. Expanding also opens every group header; collapsing does not
// fold them back, since a group's default state already starts most of them closed.
expandAllBtn.addEventListener('click', () => {
  if (collapsedNodes.size > 0) {
    collapsedNodes.clear();
    collapsedGroups.clear();
  } else {
    function collect(node) {
      if (node.children && node.children.length > 0) {
        collapsedNodes.add(node.id);
        node.children.forEach(collect);
      }
    }
    treeData.forEach(collect);
  }
  renderTree(treeData);
  renderUnifiedDocument();
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

// Progress: per-status counts over a spec's or plan's tasks. Only COMPLETED counts as done.
function progressParts(node) {
  const p = node.progress || { total: 0, counts: {} };
  const codes = Object.keys(window.STATUS_THEMES).filter(c => p.counts[c]);
  return { total: p.total, completed: p.counts.COMPLETED || 0, codes, counts: p.counts };
}

function progressText(node) {
  const p = progressParts(node);
  if (p.total === 0) return 'No tasks';
  return p.codes.map(c => `${p.counts[c]} ${getTheme(c).label.toLowerCase()}`).join(' · ');
}

function progressBar(node, height = 'h-1.5') {
  const p = progressParts(node);
  const text = esc(progressText(node));
  const segs = p.codes.map(c => `<span class="st-seg st-${c}" style="width:${(p.counts[c] / p.total) * 100}%"></span>`).join('');
  return `<div class="flex w-full ${height} rounded-full overflow-hidden bg-zinc-800" role="img" aria-label="${text}" title="${text}">${segs}</div>`;
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

// Fetch and Load Data
async function loadAllData() {
  if (isStaticMode) {
    treeData = window.STATIC_DATA.tree || [];
    graphData = window.STATIC_DATA.graph || { nodes: [], edges: [] };
    statsData = window.STATIC_DATA.stats || {};
    populateFilterOptions();
    renderAll();
    renderGraph(graphData);
    return;
  }

  loadIndicator.classList.remove('hidden');
  try {
    const [treeRes, graphRes, statsRes] = await Promise.all([
      fetch('/api/tree'),
      fetch('/api/graph'),
      fetch('/api/stats')
    ]);
    treeData = await treeRes.json();
    graphData = await graphRes.json();
    statsData = await statsRes.json();

    populateFilterOptions();
    renderAll();
    renderGraph(graphData);
  } catch (err) {
    console.error('Failed to load data:', err);
  } finally {
    loadIndicator.classList.add('hidden');
  }
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

// WebSocket Live Updates
if (!isStaticMode) {
  function connectWS() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${location.host}/ws`;
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => setBrandLive('synced');

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'reload' || data.type === 'update') {
          loadAllData();
        }
      } catch (e) {
        console.error('WS error:', e);
      }
    };

    ws.onclose = () => {
      setBrandLive('disconnected');
      setTimeout(connectWS, 3000);
    };
  }
  connectWS();
} else {
  setBrandLive('static');
}

