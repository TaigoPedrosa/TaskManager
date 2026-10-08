// State Management
let graphData = { nodes: [], edges: [] };
let selectedNodeId = null;
let visNodesDS = null;
let currentMode = window.VIEW_MODES.DOCUMENT;
// The view the toolbar left for Decisions. Decisions shows no node, so a node opens there.
let viewModeBeforeDecisions = window.VIEW_MODES.DOCUMENT;
let networkInstance = null;
let isStaticMode = typeof window.STATIC_DATA !== 'undefined';
// A node's expand/collapse state now lives in the store's open and watch sets (open reveals
// a container's children, watch fetches a node's own body); this set is this view's own record
// of which ids it asked the store to expand, since the store exposes no getter for either set.
const expandedIds = new Set();
const expandedSections = new Set();
// Group headers ("Tasks (N)", "Sections (N)") are a third, independent collapse level:
// they hide a plan's task-card list or a section list's rows
// without touching expandedIds (the plan/task body) or expandedSections (a section's
// own open state). A group id's default (collapsed or not) varies by group type, so this
// set stores only ids whose state differs from their default; see groupCollapsed().
// Session-only, never persisted, same as the two sets above.
const collapsedGroups = new Set();
// Every section id rendered by the Document view's current pass, rebuilt from scratch on
// each render (renderUnifiedDocument resets it first) -- the toggle-all-sections button
// reads it, so it only ever reflects the sections actually on screen right now.
let allSectionIds = [];


// DOM Elements
const documentPane = document.getElementById('document-pane');
const graphPane = document.getElementById('graph-pane');
const sidebarPane = document.getElementById('sidebar-pane');
const unifiedDocument = document.getElementById('unified-document');
const treeList = document.getElementById('tree-list');
const searchBox = document.getElementById('search-box');
const statsDigest = document.getElementById('stats-digest');
const viewWavesBtn = document.getElementById('view-waves-btn');
const viewGraphBtn = document.getElementById('view-graph-btn');
const viewDocBtn = document.getElementById('view-doc-btn');
const graphFitBtn = document.getElementById('graph-fit-btn');
const refreshBtn = document.getElementById('refresh-btn');
const expandAllBtn = document.getElementById('expand-all-btn');
const toggleSectionsBtn = document.getElementById('toggle-sections-btn');
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
const toolbarEl = document.getElementById('toolbar');
const filtersToggleBtn = document.getElementById('filters-toggle-btn');
const filtersPanel = document.getElementById('filters-panel');
const legendBtn = document.getElementById('legend-btn');
const legendPanel = document.getElementById('legend-panel');
const legendBody = document.getElementById('legend-body');
const legendCloseBtn = document.getElementById('legend-close-btn');
const toolbarActions = document.getElementById('toolbar-actions');
const loadIndicator = document.getElementById('load-indicator');
const dialogRoot = document.getElementById('dialog-root');
const toastRoot = document.getElementById('toast-root');
toastRoot.className = 'fixed bottom-4 right-4 z-50 flex flex-col items-end gap-2 pointer-events-none';
toastRoot.setAttribute('aria-live', 'polite');

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


// Mode Switching. The sidebar is a graph-view tool for jumping to a node; it takes
// no space in Waves or Document view so that pane reads at its own full width.
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
    viewWavesBtn.className = VIEW_BTN_ACTIVE;
    viewGraphBtn.className = VIEW_BTN_INACTIVE;
    viewDocBtn.className = VIEW_BTN_INACTIVE;
    documentPane.classList.add('hidden');
    graphPane.classList.remove('hidden');
    sidebarPane.classList.add('hidden');
    toggleSectionsBtn.classList.add('hidden');
    initWaves();
  } else if (mode === window.VIEW_MODES.DOCUMENT) {
    viewWavesBtn.className = VIEW_BTN_INACTIVE;
    viewGraphBtn.className = VIEW_BTN_INACTIVE;
    viewDocBtn.className = VIEW_BTN_ACTIVE;
    documentPane.classList.remove('hidden');
    graphPane.classList.add('hidden');
    sidebarPane.classList.add('hidden');
    toggleSectionsBtn.classList.remove('hidden');
  } else {
    viewGraphBtn.className = VIEW_BTN_ACTIVE;
    viewWavesBtn.className = VIEW_BTN_INACTIVE;
    viewDocBtn.className = VIEW_BTN_INACTIVE;
    documentPane.classList.add('hidden');
    graphPane.classList.remove('hidden');
    sidebarPane.classList.remove('hidden');
    toggleSectionsBtn.classList.add('hidden');
    if (networkInstance) {
      setTimeout(() => networkInstance.fit(), 50);
    }
  }
  // An id link's href names the view it was drawn in.
  scheduleRender();
}

// Back to the view's default: the pane at the top, nothing selected, every group at its
// default open state. Filters are not part of a view, so they stay; the sort is part of the two
// lists it orders.
function resetView() {
  const view = currentMode;
  if (view === window.VIEW_MODES.DECISIONS) {
    resetDecisions();
  } else {
    closeDetailDrawer();
    setSelectedNode(null);
    if (view === window.VIEW_MODES.WAVES) {
      resetWaves();
      wavesPane.scrollTop = 0;
    } else {
      if (expandedIds.size > 0) {
        window.tmStore.close([...expandedIds]);
        window.tmStore.unwatch([...expandedIds]);
        expandedIds.clear();
      }
      expandedSections.clear();
      collapsedGroups.clear();
      documentPane.scrollTop = 0;
      treeList.scrollTop = 0;
      if (view === window.VIEW_MODES.GRAPH && networkInstance) {
        networkInstance.unselectAll();
        networkInstance.fit();
      }
    }
  }
  navigate({ view, sort: SORTED_VIEWS.includes(view) ? 'progress' : sortOrder }, { replace: true });
  scheduleRender();
}

// Another view's segment opens that view as a new history entry; the current view's
// segment resets it.
function onViewSegment(view, id = null) {
  if (view === currentMode) resetView();
  else navigate({ view, id });
}

viewDocBtn.addEventListener('click', () => onViewSegment(window.VIEW_MODES.DOCUMENT));
viewGraphBtn.addEventListener('click', () => onViewSegment(window.VIEW_MODES.GRAPH));
viewWavesBtn.addEventListener('click', () => onViewSegment(window.VIEW_MODES.WAVES));
graphFitBtn.addEventListener('click', () => networkInstance && networkInstance.fit());
refreshBtn.addEventListener('click', () => window.tmStore.resync());


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
  scheduleRender();
});


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

// The shared renderers every view draws a node's facts with. Each returns markup; a
// focusable one carries `data-tip`, which the one tooltip below opens on hover and on focus.
const FOCUS_RING = 'focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400';

function statusIcon(code, size = 'w-3.5 h-3.5') {
  const t = getTheme(code);
  return `<span class="st-text st-${t.code} relative z-[1] inline-flex flex-shrink-0 rounded-sm ${FOCUS_RING}" role="img" tabindex="0" data-tip title="${esc(t.label)}" aria-label="${esc(t.label)}">${renderIcon(t.icon, size)}</span>`;
}

function nodeView() {
  return currentMode === window.VIEW_MODES.DECISIONS ? viewModeBeforeDecisions : currentMode;
}

// How an id link draws: inline (the default) never truncates; `wrap` breaks where nothing else on
// its line can yield; `row` stretches over its `relative` row, which takes the click and the focus
// ring while the row's own controls sit above it; `chip` is the awaiting-decision chip.
const ID_TEXT = 'font-bold text-xs leading-4 text-emerald-400';
const ID_LOOK = {
  inline: `relative z-[1] flex-shrink-0 whitespace-nowrap rounded-sm ${ID_TEXT} hover:underline ${FOCUS_RING}`,
  wrap: `relative z-[1] min-w-0 break-all rounded-sm ${ID_TEXT} hover:underline ${FOCUS_RING}`,
  row: `flex-shrink-0 whitespace-nowrap ${ID_TEXT} focus:outline-none after:absolute after:inset-0 after:rounded-lg after:content-[''] focus-visible:after:ring-2 focus-visible:after:ring-emerald-400`,
  chip: `decision-chip relative z-[1] inline-flex items-center gap-1 flex-shrink-0 whitespace-nowrap px-1.5 py-0.5 rounded-full bg-amber-950 border border-amber-400/40 hover:border-amber-400 font-medium text-[10px] leading-[14px] text-amber-400 ${FOCUS_RING}`,
};

// A node opens in the view it is named in; a decision always opens the Decisions pane.
function idLink(id, kind, look = 'inline') {
  const isDecision = kind === 'decision';
  const view = isDecision ? window.VIEW_MODES.DECISIONS : nodeView();
  const icon = look === 'chip' ? renderIcon('help-circle', 'w-3 h-3 flex-shrink-0') : '';
  return `<a href="${esc(pathFor(view, id))}" class="id-link font-mono ${ID_LOOK[look]}" data-id="${esc(id)}"${isDecision ? ' data-decision' : ''}>${icon}${esc(id)}</a>`;
}

// A spec's or plan's id does not name its kind; a task's is the default and a decision's id says it.
function kindBadge(kind) {
  if (kind !== 'spec' && kind !== 'plan') return '';
  return `<span class="kind-badge inline-flex items-center flex-shrink-0 px-1.5 py-0.5 rounded-full bg-zinc-800 border border-zinc-700 font-mono font-medium text-[10px] leading-[14px] tracking-[0.04em] text-zinc-300">${esc(kind.toUpperCase())}</span>`;
}

function priorityPill(p) {
  return `<span class="priority-pill inline-flex items-center flex-shrink-0 px-1.5 py-0.5 rounded-full bg-zinc-900 border border-zinc-800 font-mono text-[10px] leading-[14px] text-zinc-400">P${esc(p ?? 50)}</span>`;
}

function modelPill(model) {
  return `<span class="model-pill inline-flex items-center flex-shrink-0 px-1.5 py-0.5 rounded-full bg-purple-950/60 border border-purple-800/80 font-mono text-[10px] leading-[14px] text-purple-300">${esc(model)}</span>`;
}

function repoPill(repo) {
  return `<span class="repo-pill inline-flex items-center flex-shrink-0 px-1.5 py-0.5 rounded-full bg-zinc-900 border border-zinc-800 font-mono text-[10px] leading-[14px] text-cyan-400">${esc(repo)}</span>`;
}

const LEASE_ACTION_STATUS = { implement: 'IMPLEMENTING', review: 'REVIEWING', fix: 'FIXING', merge: 'MERGING' };

function leaseAction(lease) {
  const code = LEASE_ACTION_STATUS[lease.action];
  return code ? getTheme(code).label : 'Leased';
}

function heartbeatAge(iso, now = Date.now()) {
  const at = Date.parse(iso || '');
  if (Number.isNaN(at)) return '';
  const s = Math.max(0, Math.floor((now - at) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

// The header line of a leased node: the live dot and the heartbeat age; the action and agent
// are its tooltip.
function leasePulse(lease) {
  if (!lease) return '';
  const label = esc(`${leaseAction(lease)} · ${lease.agent_id}`);
  const age = heartbeatAge(lease.last_heartbeat);
  return `<span class="lease-pulse relative z-[1] inline-flex items-center gap-1.5 flex-shrink-0 rounded-sm ${FOCUS_RING}" role="img" tabindex="0" data-tip title="${label}" aria-label="${label}"><span class="w-1.5 h-1.5 rounded-full bg-emerald-400 ring-[3px] ring-emerald-500/40"></span>${age ? `<span class="font-mono text-[11px] leading-4 text-zinc-400">${esc(age)}</span>` : ''}</span>`;
}

// Where the agent is shown: the step's action as its status icon, then the agent.
function leaseBadge(lease) {
  if (!lease) return '';
  const code = LEASE_ACTION_STATUS[lease.action];
  return `<span class="lease-badge inline-flex items-center gap-1 flex-shrink-0 px-1.5 py-0.5 rounded-full bg-zinc-900 border border-zinc-700">${code ? statusIcon(code) : ''}<span class="font-mono text-[10px] leading-[14px] text-zinc-300">${esc(lease.agent_id)}</span></span>`;
}

// `group-header` keeps the drawer's own toggle wiring (detail.js) finding these.
function disclosureHeader(label, count, expanded, groupId = '') {
  return `<button type="button" class="disclosure group-header group flex items-center gap-1.5 w-full p-1 rounded-md text-left ${FOCUS_RING}" aria-expanded="${expanded}" data-group-id="${esc(groupId)}">${renderIcon(expanded ? 'chevron-down' : 'chevron-right', 'w-3 h-3 flex-shrink-0 text-zinc-400 group-hover:text-zinc-200')}<span class="text-[11px] leading-4 font-semibold uppercase tracking-[0.05em] text-zinc-400 group-hover:text-zinc-200">${esc(label)}</span> <span class="px-1.5 rounded-full bg-zinc-800 font-mono text-[10px] leading-4 text-zinc-300">${esc(count)}</span></button>`;
}

// Retry handlers are looked up by index from one delegated listener, so the markup stays a
// string like every other renderer's.
// ponytail: one entry per distinct onRetry function; callers pass a stable function, never a
// fresh closure per render, or this list grows with every error render.
const paneRetries = [];
const PANE_ICON = { empty: 'circle-dashed', error: 'octagon-x' };
// A read shows its loading pane state only once it has been in flight this long.
const LOADING_DELAY_MS = 300;

function paneState(kind, message = 'Loading…', onRetry = null) {
  const isError = kind === 'error';
  const icon = kind === 'loading'
    ? '<span class="w-5 h-5 rounded-full border-2 border-zinc-700 border-t-emerald-400 animate-spin" aria-hidden="true"></span>'
    : renderIcon(PANE_ICON[kind] || PANE_ICON.empty, `w-5 h-5 ${isError ? 'text-red-400' : 'text-zinc-400'}`);
  let retry = '';
  if (isError && onRetry) {
    let i = paneRetries.indexOf(onRetry);
    if (i < 0) i = paneRetries.push(onRetry) - 1;
    retry = `<button type="button" class="pane-retry h-7 px-2.5 rounded-md bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-[11px] leading-4 font-medium text-zinc-200 ${FOCUS_RING}" data-retry="${i}">Retry</button>`;
  }
  return `<div class="pane-state flex flex-col items-center gap-2.5 px-6 py-8 rounded-xl border border-zinc-800 bg-zinc-900/30 text-center" role="${isError ? 'alert' : 'status'}" data-pane-state="${kind}">${icon}<p class="text-[13px] leading-5 ${isError ? 'text-red-300' : 'text-zinc-300'}">${esc(message)}</p>${retry}</div>`;
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest && e.target.closest('.pane-retry');
  if (btn) paneRetries[Number(btn.getAttribute('data-retry'))]();
});

// Opening a node is a new history entry naming it in the current view; the location then
// reveals it (Document) or opens the drawer on it (Graph, Waves).
function openNode(id) {
  const view = nodeView();
  const here = readLocation();
  navigate({ view, id }, { replace: here.view === view && here.id === id });
}

// A plain click on an id link opens it in place; a modified one is left to the browser.
document.addEventListener('click', (e) => {
  const link = e.target.closest && e.target.closest('a.id-link');
  if (!link || e.button > 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  hideTip();
  const id = link.getAttribute('data-id');
  if (!link.hasAttribute('data-decision')) openNode(id);
  else if (typeof goToDecision === 'function') goToDecision(id);
  else navigate({ view: window.VIEW_MODES.DECISIONS, id });
});

// The one tooltip: hover and keyboard focus open it on any [data-tip] (its text is the
// element's aria-label), leaving, blur and Escape close it. The native title is held aside
// while it shows, so the two never stack.
const tipEl = document.createElement('div');
tipEl.id = 'tm-tooltip';
tipEl.setAttribute('role', 'tooltip');
tipEl.className = 'hidden fixed z-50 max-w-xs px-2 py-1 rounded-md border border-zinc-700 bg-zinc-900 shadow-2xl text-[11px] leading-4 font-medium text-zinc-200 pointer-events-none';
document.body.appendChild(tipEl);
let tipAnchor = null;

// An `interactive` tooltip (the Graph node's, with its decision links) takes the pointer.
function showTip(anchor, html, interactive = false) {
  hideTip();
  tipAnchor = anchor;
  const title = anchor.getAttribute('title');
  if (title !== null) {
    anchor.setAttribute('data-held-title', title);
    anchor.removeAttribute('title');
  }
  tipEl.innerHTML = html;
  tipEl.classList.toggle('pointer-events-none', !interactive);
  tipEl.classList.remove('hidden');
  const r = anchor.getBoundingClientRect();
  const w = tipEl.getBoundingClientRect().width;
  tipEl.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - w - 8))}px`;
  tipEl.style.top = `${r.bottom + 6}px`;
}

function hideTip() {
  if (!tipAnchor) return;
  const held = tipAnchor.getAttribute('data-held-title');
  if (held !== null) {
    tipAnchor.setAttribute('title', held);
    tipAnchor.removeAttribute('data-held-title');
  }
  tipAnchor = null;
  tipEl.classList.add('hidden');
}

function tipTarget(e) {
  return e.target && e.target.closest ? e.target.closest('[data-tip]') : null;
}

document.addEventListener('mouseover', (e) => {
  const a = tipTarget(e);
  if (a && a !== tipAnchor) showTip(a, esc(a.getAttribute('aria-label')));
});
document.addEventListener('mouseout', (e) => {
  const a = tipTarget(e);
  if (a && a === tipAnchor && !(e.relatedTarget && a.contains(e.relatedTarget))) hideTip();
});
document.addEventListener('focusin', (e) => {
  const a = tipTarget(e);
  if (a) showTip(a, esc(a.getAttribute('aria-label')));
});
document.addEventListener('focusout', (e) => {
  if (tipAnchor && tipTarget(e) === tipAnchor) hideTip();
});
tipEl.addEventListener('mouseleave', hideTip);
// An open tooltip takes the first Escape, so the dialog or drawer under it stays open.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || !tipAnchor) return;
  e.preventDefault();
  hideTip();
});

// Copy-id button: a 28px icon button, wired via attachCopyHandlers so it works after any
// re-render; stopPropagation keeps it from also toggling the card it sits on. A copy shows
// check-circle-2 for COPY_FEEDBACK_MS and says "Copied" through the button's own live region,
// which is on the page before the copy so the announcement is read.
const COPY_FEEDBACK_MS = 1500;

function copyIdButton(id) {
  return `<button type="button" class="copy-id-btn relative z-[1] w-7 h-7 flex items-center justify-center flex-shrink-0 rounded-md bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-zinc-300 hover:text-zinc-100 transition ${FOCUS_RING}" data-copy-id="${esc(id)}" title="Copy ID" aria-label="Copy ID ${esc(id)}">${renderIcon('copy', 'copy-id-icon w-3.5 h-3.5')}<span class="copy-id-live sr-only" aria-live="polite"></span></button>`;
}

function attachCopyHandlers(root) {
  root.querySelectorAll('.copy-id-btn').forEach(btn => {
    btn.onclick = (e) => {
      e.stopPropagation();
      const id = btn.getAttribute('data-copy-id');
      navigator.clipboard.writeText(id).then(() => {
        const icon = btn.querySelector('.copy-id-icon use');
        const live = btn.querySelector('.copy-id-live');
        icon.setAttribute('href', '#icon-check-circle-2');
        live.textContent = 'Copied';
        setTimeout(() => {
          icon.setAttribute('href', '#icon-copy');
          live.textContent = '';
        }, COPY_FEEDBACK_MS);
      }).catch(err => console.error('Could not copy id:', err));
    };
  });
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

// Sibling order. Both read only rows and the counts tree, so a static export sorts the same way.
const RUNNING_DISPLAYS = ['IMPLEMENTING', 'REVIEWING', 'FIXING', 'MERGING', 'WAITING_MERGE_AGENT'];

function planOrder(a, b) {
  return a.ordinal - b.ordinal || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}

// [rung, completed share]: ongoing 0, incomplete 1, complete 2, set aside 3. A container's own
// display can be a roll-up of its tasks (Implementing once part of it is done), so only its
// counts, which hold its own step and its tasks, say whether a step is running.
function progressRank(row) {
  const display = displayOf(row);
  if (SET_ASIDE_DISPLAYS.has(display)) return [3, 0];
  if (display === 'COMPLETED') return [2, 0];
  if (row.kind === 'task') return [RUNNING_DISPLAYS.includes(display) ? 0 : 1, 0];
  const p = progressParts(countsForRow(row));
  if (RUNNING_DISPLAYS.some(c => p.counts[c])) return [0, 0];
  return [1, p.total ? p.completed / p.total : 0];
}

function progressOrder(a, b) {
  const [rungA, shareA] = progressRank(a);
  const [rungB, shareB] = progressRank(b);
  return rungA - rungB || shareA - shareB || planOrder(a, b);
}

function priorityOrder(a, b) {
  return (b.priority ?? 50) - (a.priority ?? 50) || planOrder(a, b);
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
    const err = new Error((data && data.detail) || `${method} ${path} failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

const TOAST_TONE = {
  info: 'bg-zinc-800 border-zinc-700 text-zinc-100',
  success: 'bg-emerald-950 border-emerald-700 text-emerald-200',
  error: 'bg-red-950 border-red-700 text-red-200'
};
const TOAST_ICON = { success: 'check-circle-2', error: 'octagon-x' };
const TOAST_MS = 6000;
let toastTimer = null;

// One toast at a time, announced by #toast-root's polite live region and never focused. An
// error stays until its Retry, its close button or Escape; any other tone also leaves after 6 s.
// `action` ({ label, run }) is its one control besides close; `retry` is the Retry action.
// `opts` may still be a bare tone string.
function toast(message, opts = {}) {
  const { tone = 'info', retry = null, action = retry && { label: 'Retry', run: retry } } = typeof opts === 'string' ? { tone: opts } : opts;
  dismissToast();
  const el = document.createElement('div');
  el.className = `toast pointer-events-auto flex items-center gap-2 px-3 py-2 rounded-lg border text-xs shadow-2xl max-w-sm ${TOAST_TONE[tone] || TOAST_TONE.info}`;
  el.setAttribute('data-tone', tone);
  el.innerHTML = (TOAST_ICON[tone] ? renderIcon(TOAST_ICON[tone], 'w-3.5 h-3.5 flex-shrink-0') : '')
    + `<span class="flex-1 min-w-0 break-words">${esc(message)}</span>`
    + (action ? `<button type="button" class="toast-action${retry ? ' toast-retry' : ''} h-7 px-2.5 rounded-md bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-[11px] font-medium text-zinc-200 flex-shrink-0 ${FOCUS_RING}">${esc(action.label)}</button>` : '')
    + `<button type="button" class="toast-close p-1 rounded-md hover:bg-black/20 flex-shrink-0 ${FOCUS_RING}" aria-label="Close">${renderIcon('x', 'w-3.5 h-3.5')}</button>`;
  toastRoot.appendChild(el);
  el.querySelector('.toast-close').addEventListener('click', dismissToast);
  const act = el.querySelector('.toast-action');
  if (act) act.addEventListener('click', () => { dismissToast(); action.run(); });
  if (tone !== 'error') toastTimer = setTimeout(dismissToast, TOAST_MS);
}

function dismissToast() {
  clearTimeout(toastTimer);
  toastRoot.innerHTML = '';
}

// Escape closes a toast first; a dialog under it stays open for a second Escape.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || !toastRoot.querySelector('.toast')) return;
  e.preventDefault();
  dismissToast();
});

const SPINNER = '<span class="inline-block w-3.5 h-3.5 rounded-full border-2 border-current border-r-transparent animate-spin" aria-hidden="true"></span>';

// Every write: the control that fired it is disabled and shows the spinner until the
// response; on success it resolves with the server's answer. On failure the control comes
// back, every field stays as typed, and the error toast's Retry sends `request` again, so the
// promise resolves on whichever attempt succeeds.
function submitWrite(control, request) {
  return new Promise((resolve) => {
    const send = () => {
      const label = control.innerHTML;
      if (control.offsetWidth) control.style.minWidth = `${control.offsetWidth}px`;
      control.disabled = true;
      control.setAttribute('aria-busy', 'true');
      control.innerHTML = SPINNER;
      const restore = () => {
        control.innerHTML = label;
        control.disabled = false;
        control.removeAttribute('aria-busy');
        control.style.minWidth = '';
      };
      api(request.method, request.path, request.body).then((data) => {
        restore();
        resolve(data);
      }, (err) => {
        restore();
        toast(err.message, { tone: 'error', retry: send });
      });
    };
    send();
  });
}

const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

// Shows `node` (an overlay holding a role="dialog" panel) with focus on its least
// destructive control, keeps Tab inside it, and on Cancel, Close or Escape takes it off the
// page with its fields as typed, runs `onClose` and returns focus to `opener`.
function openDialog(node, opener = document.activeElement, onClose = null) {
  dialogRoot.appendChild(node);
  const focusables = () => [...node.querySelectorAll(FOCUSABLE)]
    .filter(el => !el.disabled && !el.closest('.hidden, [hidden]'));
  function close() {
    document.removeEventListener('keydown', onKeydown);
    node.remove();
    if (onClose) onClose();
    if (opener && opener !== document.body && document.contains(opener)) opener.focus();
  }
  function onKeydown(e) {
    if (e.defaultPrevented) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
      return;
    }
    if (e.key !== 'Tab') return;
    const els = focusables();
    if (els.length === 0) return;
    const at = els.indexOf(document.activeElement);
    if (e.shiftKey && at <= 0) {
      e.preventDefault();
      els[els.length - 1].focus();
    } else if (!e.shiftKey && (at === -1 || at === els.length - 1)) {
      e.preventDefault();
      els[0].focus();
    }
  }
  document.addEventListener('keydown', onKeydown);
  node.querySelectorAll('.dlg-cancel, .dlg-close').forEach(btn => { btn.onclick = close; });
  (node.querySelector('.dlg-cancel') || node.querySelector('.dlg-close') || focusables()[0] || node).focus();
  return { close };
}

// Live state lives on the brand icon's own colour -- no separate connection chip.

function setBrandLive(state) {
  const colors = {
    synced: 'text-emerald-400', disconnected: 'text-red-400', static: 'text-zinc-500'
  };
  const labels = { synced: 'Synced', disconnected: 'Disconnected', static: 'Static Export' };
  // An <svg>'s className is read-only (an SVGAnimatedString), so the class goes through setAttribute.
  brandIcon.setAttribute('class', `w-5 h-5 transition-colors ${colors[state]}`);
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
// The parser can yield between this page's inline scripts, so a store notification can land
// before tree.js has defined what renderAll calls: nothing renders until every script has run.
let renderScheduled = false;
function scheduleRender() {
  if (renderScheduled) return;
  renderScheduled = true;
  const frame = () => requestAnimationFrame(() => {
    renderScheduled = false;
    renderAll();
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', frame, { once: true });
  else frame();
}

// window.tmStore is created here, before filters.js/tree.js/main.js run, but with whatever
// filters happen to be in scope at this point (none yet -- main.js reads the URL later).
// That is safe: in live mode the store's own connect() only reaches the network on the
// WebSocket's `onopen`, which cannot fire before this synchronous script pass finishes, so
// main.js's later setFilters() call still lands before the one subscribe frame this page ever
// sends on load. In static mode everything below runs synchronously and gets recomputed again
// once main.js applies the real filters, which is just a local, in-memory pass.
window.tmStore = isStaticMode
  ? createStore({ staticData: window.STATIC_DATA })
  : createStore({});
// Before any row arrives the panes show loading until the first answer, and an error once
// the socket has closed or the subscribe was refused without one.
let storeAnswered = isStaticMode;
let storeError = null;
let socketLost = false;
window.tmStore.onChange((patch) => {
  if (patch && patch.connectionChanged) socketLost = !window.tmStore.connected;
  else if (patch && patch.error) storeError = patch.error;
  else if (patch && !patch.pendingChanged) storeAnswered = true;
  syncConnectionUi();
  if (patch && patch.error) toast(patch.error, { tone: 'error' });
  scheduleRender();
});
syncConnectionUi();
