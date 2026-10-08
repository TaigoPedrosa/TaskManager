// Decisions view (§3, §6.4): a fourth view beside Document, Graph and Waves. Wraps core.js's
// setViewMode and edit.js's renderNewMenu rather than editing those files, the same
// reassignment pattern detail.js already uses for renderSectionBody/renderUnifiedDocument --
// every function reassigned here is a plain top-level `function` declared in an earlier
// <script> block, and script tags share one global scope executed in order.

window.VIEW_MODES.DECISIONS = 'decisions';

let decisionsData = [];
let decisionsNextCursor = null;
let decisionsLoading = false;
// False until the current tab's first page answers, so a list still in flight never reads as
// empty and never resolves a bare /decisions to its top row.
let decisionsLoaded = false;
// The router's (filters.js applyLocation) to write; this file selects through navigate().
let selectedDecisionId = null;
let decisionsTab = 'open';
// /api/decisions' own per-status totals, refreshed on every page fetch -- the only source for
// the Answered/Withdrawn tab counts, since (unlike Open) nothing pushes those live.
let decisionsCounts = null;
// Last status this session saw a given decision hold, keyed by id -- a later render of the
// same decision seeing OPEN turn into anything else is a change made elsewhere.
const decisionDetailLastStatus = new Map();
// Each decision's unsent answer (picked option, custom text, rationale), for this page load
// only: nothing here reaches browser storage.
const decisionDrafts = new Map();
// Decisions this page has sent an answer or a withdrawal for: the push that write causes is
// its own echo, never a change made elsewhere, and their detail waits for the write's follow-up.
const ownWrites = new Set();
// The decision another session closed while its form was on screen: the form stays, disabled
// with its draft, until the owner asks for the closed record or moves on.
let heldOpenId = null;
// Where focus goes once a decision's detail has rendered: { id, to: 'option' | 'heading' }.
let pendingFocus = null;
// Below sm: the open drawer ({ close }), its list's scroll kept between openings, and whether
// closing it hands focus back to the Drawer button (a pick sends it to the page instead).
let decisionsDrawer = null;
let drawerListScroll = 0;
let drawerReturnsFocus = true;
const DECISIONS_PAGE_LIMIT = 50;
// The effects that set nodes aside: an answer carrying one names it and is confirmed first.
const DESTRUCTIVE_EFFECTS = ['abandon', 'defer'];

const decisionsPane = document.getElementById('decisions-pane');
const decisionsTabsEl = document.getElementById('decisions-tabs');
const decisionsListEl = document.getElementById('decisions-list');
const decisionsDetailEl = document.getElementById('decisions-detail');
const decisionsAside = decisionsListEl.parentNode;

const DECISION_TABS = [
  { key: 'open', label: 'Open', status: 'OPEN' },
  { key: 'answered', label: 'Answered', status: 'ANSWERED' },
  { key: 'withdrawn', label: 'Withdrawn', status: 'WITHDRAWN' },
];

function decisionTabFor(status) {
  const tab = DECISION_TABS.find(t => t.status === status);
  return tab ? tab.key : 'open';
}

function decisionStatusLabel(status) {
  const tab = DECISION_TABS.find(t => t.status === status);
  return tab ? tab.label : status;
}

// A decision dependency row (blockers/dependencies/dependents) reads its own status, not a
// display theme: none of the display themes means Open, Answered or Withdrawn.
const DECISION_STATUS_ICON = { OPEN: 'help-circle', ANSWERED: 'check-circle-2', WITHDRAWN: 'x-circle' };
function decisionStatusIcon(status, size = 'w-3.5 h-3.5') {
  const icon = DECISION_STATUS_ICON[status] || 'help-circle';
  const label = esc(decisionStatusLabel(status));
  return `<span class="relative z-[1] inline-flex flex-shrink-0 rounded-sm ${FOCUS_RING}" role="img" tabindex="0" data-tip title="${label}" aria-label="${label}">${renderIcon(icon, size)}</span>`;
}

// Below sm the decision is the page and the list is a drawer over it.
function decisionsNarrow() {
  return window.innerWidth < SM_MIN_PX;
}

function nodeCount(n) {
  return `${n} node${n === 1 ? '' : 's'}`;
}

function waitingNodes(n) {
  return `${n} waiting ${n === 1 ? 'node' : 'nodes'}`;
}


// Data ---------------------------------------------------------------------------------------
// Decisions are not rows (§ "Rows"): nothing pushes their list over the store's subscription,
// so this pane pages /api/decisions itself, one status/tab at a time -- refreshDecisionsData()
// replaces the current tab's page, loadMoreDecisions() appends the next one. Only the open
// count is live (window.tmStore.decisionsOpen, part of every snapshot/update); the tracker
// below is what turns that into "refetch the tab that's open" rather than polling.

let decisionsLoadFailed = false;
let lastSeenDecisionsOpen = isStaticMode ? null : window.tmStore.decisionsOpen;
// Set once a first page has been in flight LOADING_DELAY_MS, so a fast answer never flashes
// the loading state.
let decisionsSlowLoad = false;
let decisionsLoadTimer = null;

function decisionsQueryParams(cursor) {
  const status = DECISION_TABS.find(t => t.key === decisionsTab).status;
  const params = new URLSearchParams({ status, limit: String(DECISIONS_PAGE_LIMIT) });
  if (cursor) params.set('cursor', cursor);
  return params;
}

async function refreshDecisionsData() {
  const tab = decisionsTab;
  if (!decisionsLoaded) {
    clearTimeout(decisionsLoadTimer);
    decisionsLoadTimer = setTimeout(() => {
      decisionsSlowLoad = true;
      if (currentMode !== window.VIEW_MODES.DECISIONS) return;
      renderDecisionsTabs();
      renderDecisionsList();
      if (!selectedDecisionId) renderNoSelection();
    }, LOADING_DELAY_MS);
  }
  if (isStaticMode) {
    decisionsData = (window.STATIC_DATA && window.STATIC_DATA.decisions) || [];
    decisionsNextCursor = null;
  } else {
    try {
      const res = await api('GET', `/api/decisions?${decisionsQueryParams()}`);
      // A tab picked while this page was in flight has its own request, which renders.
      if (tab !== decisionsTab) return;
      decisionsData = res.items;
      decisionsNextCursor = res.next;
      decisionsCounts = res.counts;
      decisionsLoadFailed = false;
    } catch (e) {
      if (tab !== decisionsTab) return;
      // A failed load is the list's error pane state with Retry, never an empty queue, and
      // nothing else on the screen repeats it.
      console.error('Failed to load decisions:', e);
      decisionsData = [];
      decisionsNextCursor = null;
      decisionsCounts = null;
      decisionsLoadFailed = true;
    }
  }
  clearTimeout(decisionsLoadTimer);
  decisionsSlowLoad = false;
  decisionsLoaded = true;
  updateDecisionsBadge();
  if (currentMode === window.VIEW_MODES.DECISIONS) renderDecisionsView();
}

async function loadMoreDecisions() {
  if (isStaticMode || !decisionsNextCursor || decisionsLoading) return;
  decisionsLoading = true;
  try {
    const res = await api('GET', `/api/decisions?${decisionsQueryParams(decisionsNextCursor)}`);
    decisionsData = decisionsData.concat(res.items);
    decisionsNextCursor = res.next;
    decisionsCounts = res.counts;
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    decisionsLoading = false;
    renderDecisionsList();
  }
}

// The open count arrives with every snapshot/update regardless of which tab is showing; a
// change to it means some decision moved in or out of Open, which can also change what the
// Answered/Withdrawn tabs hold, so whichever tab is on screen refetches its own page.
window.tmStore.onChange(() => {
  if (isStaticMode) return;
  const open = window.tmStore.decisionsOpen;
  if (open === lastSeenDecisionsOpen) return;
  lastSeenDecisionsOpen = open;
  updateDecisionsBadge();
  if (currentMode === window.VIEW_MODES.DECISIONS) refreshDecisionsData();
});



// Toolbar entry point: the switcher's own fourth segment (index.html), and setViewMode
// wrapped so a fourth mode exists without touching core.js's own Document/Graph/Waves switch.

const viewDecisionsBtn = document.getElementById('view-decisions-btn');
const decisionsBadgeEl = document.getElementById('decisions-badge');
viewDecisionsBtn.addEventListener('click', () => onViewSegment(window.VIEW_MODES.DECISIONS, selectedDecisionId));

function updateDecisionsBadge() {
  // Live everywhere the store is (decisions_open travels with every snapshot/update); a
  // static export has no store push at all, so it counts the one page it was seeded with.
  const openCount = isStaticMode ? decisionsData.filter(d => d.status === 'OPEN').length : window.tmStore.decisionsOpen;
  if (openCount > 0) {
    decisionsBadgeEl.textContent = openCount > 99 ? '99+' : String(openCount);
    decisionsBadgeEl.classList.remove('hidden');
  } else {
    decisionsBadgeEl.classList.add('hidden');
  }
  // The badge span is a sighted-only count; aria-label is what a screen reader (or, below
  // `sm`, a caption-less icon button) actually reads, so the count belongs there too.
  viewDecisionsBtn.setAttribute('aria-label', `Decisions view, ${openCount} open`);
}

if (typeof setViewMode === 'function') {
  const previousSetViewMode = setViewMode;
  // The badge is absolutely positioned against this segment, so it stays relative in both states.
  const DECISIONS_BTN_ACTIVE = `${VIEW_BTN_ACTIVE} relative`;
  const DECISIONS_BTN_INACTIVE = `${VIEW_BTN_INACTIVE} relative`;
  setViewMode = function (mode) {
    if (mode === window.VIEW_MODES.DECISIONS) {
      if (currentMode !== window.VIEW_MODES.DECISIONS) viewModeBeforeDecisions = currentMode;
      closeDetailDrawer();
      currentMode = mode;
      documentPane.classList.add('hidden');
      graphPane.classList.add('hidden');
      sidebarPane.classList.add('hidden');
      decisionsPane.classList.remove('hidden');
      toggleSectionsBtn.classList.add('hidden');
      viewWavesBtn.className = VIEW_BTN_INACTIVE;
      viewGraphBtn.className = VIEW_BTN_INACTIVE;
      viewDocBtn.className = VIEW_BTN_INACTIVE;
      viewDecisionsBtn.className = DECISIONS_BTN_ACTIVE;
      renderDecisionsView();
      refreshDecisionsData();
      return;
    }
    if (mode === window.VIEW_MODES.DOCUMENT) closeDetailDrawer();
    if (decisionsDrawer) closeDecisionsDrawer(false);
    liftToasts(null);
    decisionsPane.classList.add('hidden');
    viewDecisionsBtn.className = DECISIONS_BTN_INACTIVE;
    previousSetViewMode(mode);
  };
}

// Selecting a decision is a new history entry naming it; the router's applyLocation is what
// moves selectedDecisionId and renders it.
function selectDecision(decisionId) {
  const here = readLocation();
  const same = here.view === window.VIEW_MODES.DECISIONS && here.id === decisionId;
  navigate({ view: window.VIEW_MODES.DECISIONS, id: decisionId }, { replace: same });
}

// Reached from a decision id link in any view: switch to the Decisions view and open that
// decision under the tab it is listed in.
function goToDecision(decisionId) {
  if (decisionsDrawer) {
    pickFromDrawer(decisionId);
    return;
  }
  const row = decisionsData.find(d => d.id === decisionId);
  if (row && decisionTabFor(row.status) !== decisionsTab) setDecisionsTab(decisionTabFor(row.status));
  selectDecision(decisionId);
}


// Shared by every control in the pane: the UA's default focus outline is a 1px line that
// barely shows on zinc-950, and nothing else marked a pressed control.
const DEC_FOCUS = 'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 focus-visible:ring-offset-2 focus-visible:ring-offset-zinc-950';
const DEC_MUTED = 'text-zinc-400'; // zinc-500 on zinc-950 is 4.1:1, under AA for 12-14px text


// List -----------------------------------------------------------------------------------------

// The Open tab's count is the live total (decisions_open), pushed with every snapshot/update
// with no fetch needed; Answered/Withdrawn have no live push, so they read the snapshot
// /api/decisions took of every status the last time any tab was fetched. Static mode holds
// every decision at once, so its tabs count straight off it instead. While the list has
// failed, is on its first load, or shows its loading state, no tab shows a count.
function decisionsTabCount(tab) {
  if (isStaticMode) return decisionsData.filter(d => decisionTabFor(d.status) === tab.key).length;
  if (decisionsLoadFailed || (!decisionsLoaded && (decisionsSlowLoad || decisionsCounts === null))) return null;
  if (tab.key === 'open') return window.tmStore.decisionsOpen;
  return decisionsCounts ? decisionsCounts[tab.key] : null;
}

function renderDecisionsTabs() {
  const focusedTab = decisionsTabsEl.contains(document.activeElement) ? document.activeElement.getAttribute('data-tab') : null;
  decisionsTabsEl.innerHTML = DECISION_TABS.map(t => {
    const count = decisionsTabCount(t);
    const active = t.key === decisionsTab;
    const cls = active
      ? 'bg-zinc-800 text-white'
      : 'text-zinc-400 hover:text-white hover:bg-zinc-900 active:bg-zinc-800';
    const pill = count === null ? '' : ` <span class="dec-tab-count px-1.5 py-0.5 rounded-full border text-[10px] font-medium leading-none ${active ? 'bg-zinc-700 border-transparent text-zinc-100' : 'bg-zinc-900 border-zinc-700 text-zinc-400'}">${count}</span>`;
    return `<button type="button" id="dec-tab-${t.key}" role="tab" aria-selected="${active}" aria-controls="decisions-list" tabindex="${active ? '0' : '-1'}" data-tab="${t.key}" class="dec-tab-btn h-7 px-2.5 flex items-center gap-1.5 rounded-md text-xs font-medium transition ${cls} ${DEC_FOCUS}">${t.label}${pill}</button>`;
  }).join('');
  const tabs = Array.from(decisionsTabsEl.querySelectorAll('.dec-tab-btn'));
  function activate(key, focusIt) {
    selectDecisionsTab(key);
    if (focusIt) {
      const btn = decisionsTabsEl.querySelector(`[data-tab="${key}"]`);
      if (btn) btn.focus();
    }
  }
  tabs.forEach((btn, i) => {
    btn.addEventListener('click', () => activate(btn.getAttribute('data-tab'), false));
    // Standard ARIA tabs pattern: arrow keys move focus and selection together.
    btn.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      e.preventDefault();
      const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      activate(next.getAttribute('data-tab'), true);
    });
  });
  if (focusedTab) {
    const btn = decisionsTabsEl.querySelector(`[data-tab="${focusedTab}"]`);
    if (btn) btn.focus();
  }
  decisionsListEl.setAttribute('role', 'tabpanel');
  decisionsListEl.setAttribute('aria-labelledby', `dec-tab-${decisionsTab}`);
}

// A tab shows its own list. From sm up it also selects that list's top row, and activating the
// tab on screen again does the same from the top, read afresh. In the drawer only a pick changes
// the page, so a tab there changes the list alone, and the tab on screen scrolls it back to the top.
function selectDecisionsTab(key) {
  decisionsListEl.scrollTop = 0;
  if (key !== decisionsTab) setDecisionsTab(key);
  else refreshDecisionsData();
  if (decisionsNarrow()) return;
  decisionsDetailEl.scrollTop = 0;
  navigate({ view: window.VIEW_MODES.DECISIONS }, { replace: true });
}

function setDecisionsTab(key) {
  decisionsTab = key;
  decisionsData = [];
  decisionsNextCursor = null;
  decisionsLoaded = false;
  decisionsLoadFailed = false;
  renderDecisionsTabs();
  renderDecisionsList();
  refreshDecisionsData();
}

// The Decisions view's default: the Open tab from the top. resetView's own navigate, right
// after this, clears the selection, and the list's top row then takes it.
function resetDecisions() {
  if (decisionsTab !== 'open') setDecisionsTab('open');
  [decisionsPane, decisionsListEl, decisionsDetailEl].forEach(el => { el.scrollTop = 0; });
}

// Newest first, as /api/decisions pages them, and sorted here so a static export reads the same.
function visibleDecisionRows() {
  return decisionsData
    .filter(d => decisionTabFor(d.status) === decisionsTab)
    .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
}

// What stands in for the rows, through paneState: the failed request with Retry, the loading
// state once the first page has been in flight a while, or the empty tab. '' while there are
// rows, and while a request too fast to show loading is still out.
function decisionsListStateHtml() {
  if (decisionsLoadFailed) return paneState('error', 'Could not load decisions.', refreshDecisionsData);
  if (!decisionsLoaded) return decisionsSlowLoad ? paneState('loading') : '';
  return visibleDecisionRows().length ? '' : paneState('empty', `No ${decisionsTab} decisions.`);
}

// One line, as the Decision row draws it: the id, the title truncating with the whole question
// as its tooltip, the priority when it is not 50, a closed row's "Was blocking n", the age (an
// open row's since it was raised, a closed one's since it closed). The tab is the status, so no
// row repeats it. The title is the row's own target and its overlay spans the line; the id link
// sits above it, so no link nests in another.
function decisionRowHtml(d) {
  const active = d.id === selectedDecisionId;
  const priority = d.priority ?? 50;
  const at = d.closed_at ?? d.created_at;
  return `
    <div class="dec-row-line relative flex items-center gap-2 h-9 px-2.5 rounded-lg border transition ${active ? 'bg-zinc-800 border-zinc-700' : 'bg-zinc-900/60 border-zinc-800 hover:bg-zinc-900 hover:border-zinc-700 active:bg-zinc-800'}">
      ${idLink(d.id, 'decision')}
      <button type="button" class="dec-row flex-1 min-w-0 truncate text-left text-xs leading-4 font-medium text-zinc-100 focus:outline-none after:absolute after:inset-0 after:rounded-lg after:content-[''] focus-visible:after:ring-2 focus-visible:after:ring-emerald-400" data-decision-id="${esc(d.id)}" title="${esc(d.title)}"${active ? ' aria-current="page"' : ''}>${esc(d.title)}</button>
      ${priority !== 50 ? priorityPill(priority) : ''}
      ${d.status !== 'OPEN' ? `<span class="dec-was-blocking flex-shrink-0 text-[10px] leading-[14px] text-zinc-400">Was blocking ${esc(d.was_blocking ?? 0)}</span>` : ''}
      <time class="dec-age flex-shrink-0 font-mono text-[11px] leading-4 text-zinc-400" datetime="${esc(at)}">${esc(heartbeatAge(at))}</time>
    </div>`;
}

function renderDecisionsList() {
  // A row's title or its id link: whichever had focus has it again in the redrawn list.
  const focused = document.activeElement;
  const focusedAttr = decisionsListEl.contains(focused) && ['data-decision-id', 'data-id'].find(a => focused.hasAttribute(a));
  const focusedSel = focusedAttr ? `${focused.localName}[${focusedAttr}="${CSS.escape(focused.getAttribute(focusedAttr))}"]` : null;
  const state = decisionsListStateHtml();
  if (state || !decisionsLoaded) {
    decisionsListEl.innerHTML = state;
    return;
  }

  const loadMoreHtml = decisionsNextCursor
    ? `<button type="button" id="dec-load-more-btn" class="w-full h-8 rounded-lg text-xs font-medium text-zinc-300 hover:text-white hover:bg-zinc-900 active:bg-zinc-800 border border-dashed border-zinc-700 transition ${DEC_FOCUS}" ${decisionsLoading ? 'disabled' : ''}>${decisionsLoading ? 'Loading…' : 'Load more'}</button>`
    : '';

  const top = decisionsListEl.scrollTop;
  decisionsListEl.innerHTML = visibleDecisionRows().map(decisionRowHtml).join('') + loadMoreHtml;
  decisionsListEl.scrollTop = top;

  decisionsListEl.querySelectorAll('.dec-row').forEach(btn => {
    btn.addEventListener('click', () => pickDecision(btn.getAttribute('data-decision-id')));
  });
  const loadMoreBtn = decisionsListEl.querySelector('#dec-load-more-btn');
  if (loadMoreBtn) loadMoreBtn.addEventListener('click', loadMoreDecisions);
  const again = focusedSel && decisionsListEl.querySelector(focusedSel);
  if (again) again.focus();
}

function focusDecisionRow(decisionId) {
  const row = decisionsListEl.querySelector(`.dec-row[data-decision-id="${CSS.escape(decisionId)}"]`);
  if (row) row.focus();
}

function pickDecision(decisionId) {
  if (decisionsDrawer) pickFromDrawer(decisionId);
  else selectDecision(decisionId);
}


// Below sm the list is a dialog over the page. It holds the pane's own tabs and list, moved in
// while it is open and back into the hidden list column when it closes, so every render keeps
// drawing into one place.

function drawerButtonHtml() {
  return `<button type="button" class="dec-drawer-btn sm:hidden w-7 h-7 flex items-center justify-center flex-shrink-0 rounded-md bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-zinc-300 hover:text-zinc-100 ${FOCUS_RING}" aria-label="Decisions" aria-haspopup="dialog" aria-expanded="${Boolean(decisionsDrawer)}" aria-controls="decisions-drawer">${renderIcon('panel-left-open', 'w-4 h-4')}</button>`;
}

function paintDrawerButtons() {
  decisionsDetailEl.querySelectorAll('.dec-drawer-btn').forEach(btn => btn.setAttribute('aria-expanded', String(Boolean(decisionsDrawer))));
}

function openDecisionsDrawer() {
  const overlay = document.createElement('div');
  overlay.className = 'fixed inset-0 z-50 bg-black/60';
  overlay.innerHTML = `
    <div id="decisions-drawer" role="dialog" aria-modal="true" aria-labelledby="decisions-drawer-title" class="absolute inset-y-0 left-0 w-[calc(100%-32px)] flex flex-col bg-zinc-950 border-r border-zinc-700 shadow-2xl">
      <div class="flex items-center gap-2 pl-4 pr-3 pt-3 flex-shrink-0">
        <h2 id="decisions-drawer-title" class="flex-1 min-w-0 text-sm leading-5 font-semibold text-zinc-100">Decisions</h2>
        <button type="button" class="dlg-close w-7 h-7 flex items-center justify-center flex-shrink-0 rounded-md text-zinc-400 hover:text-white hover:bg-zinc-800 ${FOCUS_RING}" aria-label="Close">${renderIcon('x', 'w-4 h-4')}</button>
      </div>
    </div>`;
  const panel = overlay.querySelector('#decisions-drawer');
  panel.append(decisionsTabsEl, decisionsListEl);
  decisionsListEl.scrollTop = drawerListScroll;
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closeDecisionsDrawer(true); });
  // ↑/↓ move focus between rows; only a pick changes the page.
  panel.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    const row = e.target.closest && e.target.closest('.dec-row');
    if (!row) return;
    e.preventDefault();
    const rows = Array.from(decisionsListEl.querySelectorAll('.dec-row'));
    const next = rows[rows.indexOf(row) + (e.key === 'ArrowDown' ? 1 : -1)];
    if (next) next.focus();
  });
  drawerReturnsFocus = true;
  decisionsDrawer = openDialog(overlay, null, () => {
    drawerListScroll = decisionsListEl.scrollTop;
    decisionsAside.append(decisionsTabsEl, decisionsListEl);
    decisionsDrawer = null;
    paintDrawerButtons();
    const btn = decisionsDetailEl.querySelector('.dec-drawer-btn');
    if (drawerReturnsFocus && btn) btn.focus();
  });
  paintDrawerButtons();
  const current = selectedDecisionId && decisionsListEl.querySelector(`.dec-row[data-decision-id="${CSS.escape(selectedDecisionId)}"]`);
  (current || decisionsTabsEl.querySelector('[aria-selected="true"]') || panel).focus();
}

function closeDecisionsDrawer(returnFocus) {
  drawerReturnsFocus = returnFocus;
  decisionsDrawer.close();
}

// A pick closes the drawer and opens that decision's page, with focus on its heading.
function pickFromDrawer(decisionId) {
  closeDecisionsDrawer(false);
  if (decisionId === selectedDecisionId) {
    focusDecisionPage('heading');
    return;
  }
  pendingFocus = { id: decisionId, to: 'heading' };
  selectDecision(decisionId);
}

decisionsDetailEl.addEventListener('click', (e) => {
  if (e.target.closest && e.target.closest('.dec-drawer-btn')) openDecisionsDrawer();
});

window.addEventListener('resize', () => {
  if (decisionsDrawer && !decisionsNarrow()) closeDecisionsDrawer(false);
});

// The page bar of a page with no decision drawn below sm: the Drawer button, then the id the
// location names while that decision loads or fails.
function pageBarHtml(decisionId = null) {
  return `<div class="dec-page-bar sm:hidden sticky top-0 z-10 -mx-4 -mt-5 mb-5 px-4 py-2 flex items-center gap-2 bg-zinc-950 border-b border-zinc-800">${drawerButtonHtml()}${decisionId ? idLink(decisionId, 'decision') : ''}</div>`;
}

// Below sm a toast sits above the fixed answer bar, across the page: 12px over the bar, whose
// height is 57px, or 81px once its "Answer <pick>" line shows.
function liftToasts(bar) {
  toastRoot.classList.toggle('max-sm:left-4', Boolean(bar));
  toastRoot.classList.toggle('max-sm:items-stretch', Boolean(bar));
  toastRoot.classList.toggle('max-sm:bottom-[69px]', bar === 'empty');
  toastRoot.classList.toggle('max-sm:bottom-[93px]', bar === 'picked');
}


// Keys: ↑/↓ select the previous or next row from the list or the detail, 1-9 pick an option
// in the order shown; neither acts inside a text field, where digits and arrows type.
// Ctrl/Cmd+Enter is the answer form's own (wireDecisionAnswerForm), and an open dialog keeps
// its keys.

function isTextField(el) {
  return Boolean(el && el.matches && el.matches('input, textarea, select, [contenteditable]'));
}

document.addEventListener('keydown', (e) => {
  if (currentMode !== window.VIEW_MODES.DECISIONS || e.defaultPrevented || dialogRoot.children.length > 0) return;
  const target = e.target;
  if (target !== document.body && !decisionsPane.contains(target)) return;
  if (isTextField(target) || e.altKey || e.ctrlKey || e.metaKey) return;
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    // The page below sm has no list to move through.
    if (decisionsNarrow()) return;
    const rows = visibleDecisionRows();
    if (rows.length === 0) return;
    e.preventDefault();
    const at = rows.findIndex(d => d.id === selectedDecisionId);
    const next = rows[at < 0 ? 0 : Math.min(rows.length - 1, Math.max(0, at + (e.key === 'ArrowDown' ? 1 : -1)))];
    if (next.id !== selectedDecisionId) selectDecision(next.id);
    focusDecisionRow(next.id);
    return;
  }
  if (/^[1-9]$/.test(e.key)) {
    const card = decisionsDetailEl.querySelectorAll('.dec-answer-form .dec-option-card')[Number(e.key) - 1];
    if (!card || card.disabled) return;
    e.preventDefault();
    card.focus();
    card.click();
  }
});


// Detail -----------------------------------------------------------------------------------------

// The decision whose detail is on screen now, so a re-render of that same decision (a live
// status change, the owner's own write) swaps the markup in place instead of passing through
// the loading state, which would drop the reader's scroll position with it.
let renderedDecisionId = null;
// What that render drew (the detail as fetched, and whether its form is held), so a refresh
// that brings back the same decision unchanged leaves the screen, its focus and caret alone.
let renderedDetailKey = null;

// Every task row the store currently holds, for the New decision dialog's Blocks field.
function visibleTaskRows() {
  return [...window.tmStore.rows.values()].filter(r => r.kind === 'task');
}

// A 404 is a real answer -- there is no such decision -- so it resolves to null; any other
// failure rethrows with the request's own message, which the error state shows as-is.
async function fetchNodeDetail(id) {
  if (isStaticMode) return (window.STATIC_DATA.bodies || {})[id] || null;
  try {
    return await api('GET', `/api/nodes/${id}`);
  } catch (e) {
    if (e.status === 404) return null;
    throw e;
  }
}

// What an effect does to the n nodes waiting on the decision, in the pill's own words and tone.
const EFFECT_PILL = {
  none: { cls: 'bg-zinc-900 border-zinc-800 text-zinc-400', text: () => 'No effect on waiting nodes' },
  drop_edge: { cls: 'bg-zinc-800 border-zinc-700 text-zinc-300', text: n => `${waitingNodes(n)} ${n === 1 ? 'stops' : 'stop'} waiting` },
  reopen: { cls: 'bg-zinc-800 border-zinc-700 text-zinc-300', text: (n, subject) => `Reopens ${subject || 'its subject'}` },
  defer: { cls: 'bg-amber-950/60 border-amber-800/60 text-amber-300', text: n => `Defers ${waitingNodes(n)}` },
  abandon: { cls: 'bg-red-950 border-red-900/60 text-red-300', text: n => `Abandons ${waitingNodes(n)}` },
};

function effectPillHtml(effect, waitingCount, subject) {
  const pill = EFFECT_PILL[effect] || EFFECT_PILL.none;
  return `<span class="dec-effect-pill self-start inline-flex px-2 py-0.5 rounded-full border text-[10px] leading-[14px] font-medium whitespace-nowrap ${pill.cls}" data-effect="${esc(effect || 'none')}">${esc(pill.text(waitingCount, subject))}</span>`;
}

// The recommended option leads, the rest keep their own order; the number keys follow it.
function shownOptions(data) {
  return [...(data.options || [])].sort((a, b) => Number(Boolean(b.recommended)) - Number(Boolean(a.recommended)));
}

// An option reads the same selectable or not: label and Recommended, then the description,
// then its effect on the `waiting` nodes. A selectable one is a radio whose colours follow its
// own aria-checked; the chosen one of an answered decision keeps the checked look, without a dot.
function optionCardHtml(opt, { selectable = false, tabbable = false, waiting = 0, subject = null } = {}) {
  const body = `
    <div class="flex-1 min-w-0 flex flex-col gap-1">
      <div class="flex items-center gap-2">
        <span class="text-sm font-medium text-zinc-100">${esc(opt.label)}</span>
        ${opt.recommended ? '<span class="px-1.5 py-0.5 rounded-full bg-emerald-950/60 text-emerald-300 border border-emerald-800/60 text-[10px] font-medium">Recommended</span>' : ''}
      </div>
      ${opt.description ? `<div class="dec-option-desc text-xs leading-[18px] ${DEC_MUTED}">${renderSectionBody(opt.description)}</div>` : ''}
      ${effectPillHtml(opt.effect, waiting, subject)}
    </div>`;
  if (!selectable) {
    return `<div class="dec-option-card flex items-start p-3 rounded-lg border bg-emerald-950/40 border-emerald-600" data-option-key="${esc(opt.key)}" data-chosen="true">${body}</div>`;
  }
  // Roving tabindex: only the checked card (or, with none checked yet, the first) is a tab
  // stop, so Tab enters the group once instead of stopping on every option in turn.
  return `
    <button type="button" role="radio" aria-checked="false" tabindex="${tabbable ? '0' : '-1'}" data-option-key="${esc(opt.key)}"
      class="dec-option-card group w-full text-left flex items-start gap-2.5 p-3 rounded-lg border transition bg-zinc-900/60 border-zinc-800 hover:border-zinc-600 active:bg-zinc-900 aria-checked:bg-emerald-950/40 aria-checked:border-emerald-600 disabled:opacity-60 disabled:cursor-not-allowed ${DEC_FOCUS}">
      <span class="pt-px flex-shrink-0" aria-hidden="true">
        <span class="dec-radio-dot flex items-center justify-center w-4 h-4 rounded-full border-[1.5px] border-zinc-500 group-aria-checked:border-emerald-500">
          <span class="hidden w-2 h-2 rounded-full bg-emerald-500 group-aria-checked:block"></span>
        </span>
      </span>
      ${body}
    </button>`;
}

// One line per node waiting on the decision, or that was: its current status, its kind when it
// is a container, its id and its title truncating, a chevron, and on an open decision the remove
// ×. The id link spans the line and opens the node in the view the owner came from; the status
// icon and × sit above it.
function waitingRowHtml(t, canEditBlocks) {
  return `
    <div class="dec-waiting-row relative flex items-center gap-2 px-2.5 py-2 rounded-lg bg-zinc-900/60 border border-zinc-800 hover:border-zinc-700 active:bg-zinc-900 transition">
      ${statusIcon(t.status || 'STALE')}
      ${kindBadge(t.kind)}
      ${idLink(t.id, t.kind, 'row')}
      <span class="dec-waiting-title flex-1 min-w-0 truncate text-xs leading-4 text-zinc-400" title="${esc(t.title || '')}">${esc(t.title || '')}</span>
      <span class="flex-shrink-0 text-zinc-400" aria-hidden="true">${renderIcon('chevron-right', 'w-3.5 h-3.5')}</span>
      ${canEditBlocks ? `<button type="button" class="dec-block-remove relative z-[1] flex-shrink-0 p-1 rounded-md text-zinc-400 hover:text-red-400 hover:bg-zinc-800 active:bg-zinc-700 ${DEC_FOCUS}" data-task-id="${esc(t.id)}" aria-label="Stop ${esc(t.id)} waiting on this decision">${renderIcon('x', 'w-3 h-3')}</button>` : ''}
    </div>`;
}

// "Oct 6, 2026, 14:32" in the reader's own locale.
function localTime(iso) {
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

// A decision's context is real markdown (marked.parse); the page's Tailwind build carries no
// Typography plugin, so each rendered tag gets the app's own type scale as classes. The card
// spaces its blocks, so no tag carries a margin.
const CONTEXT_TYPE = {
  h1: 'text-base leading-6 font-bold text-white',
  h2: 'text-base leading-6 font-bold text-white',
  h3: 'text-sm leading-5 font-semibold text-zinc-100',
  h4: 'text-xs leading-5 font-semibold text-zinc-100',
  p: 'text-xs leading-5 text-zinc-300',
  ul: 'list-disc pl-5 space-y-0.5 marker:text-zinc-400',
  ol: 'list-decimal pl-5 space-y-0.5 marker:text-zinc-400',
  li: 'text-xs leading-5 text-zinc-300',
  table: 'w-full table-fixed',
  th: 'px-2.5 py-1.5 bg-zinc-900 text-left align-top text-xs leading-[18px] font-semibold text-zinc-200 break-words',
  td: 'px-2.5 py-1.5 border-t border-zinc-800 align-top text-xs leading-[18px] text-zinc-300 break-words',
  pre: 'p-3 rounded-lg bg-zinc-950 border border-zinc-800 font-mono text-xs leading-5 text-zinc-200 whitespace-pre overflow-x-auto focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400',
  code: 'font-mono text-[11px] text-emerald-300',
  blockquote: 'pl-3 py-0.5 border-l-2 border-zinc-700 text-xs leading-5 italic text-zinc-400',
  a: 'text-emerald-400 underline',
};
// A tag inside one of these reads as its block does: code in a code block, a quote's paragraphs.
const CONTEXT_TYPE_INSIDE = { code: 'pre', p: 'blockquote' };

function contextBodyHtml(markdown) {
  // renderSectionBody's output is already sanitised; DOMParser keeps the walk below inert (no
  // image request, no handler) while the tags get their classes.
  const doc = new DOMParser().parseFromString(renderSectionBody(markdown), 'text/html');
  // Paragraphs reflow to the card's width: under renderSectionBody's `breaks: true` a brief
  // hard-wrapped at 100 columns reads as ragged short lines.
  doc.body.querySelectorAll('p br, li br').forEach(br => br.replaceWith(' '));
  Object.entries(CONTEXT_TYPE).forEach(([tag, cls]) => {
    doc.body.querySelectorAll(tag).forEach(el => {
      if (CONTEXT_TYPE_INSIDE[tag] && el.closest(CONTEXT_TYPE_INSIDE[tag])) return;
      el.classList.add(...cls.split(' '));
    });
  });
  // A code block scrolls sideways inside itself, so the keyboard can reach it to scroll it. A
  // table's rounded border sits on a wrapper, which also scrolls one that cannot fit.
  doc.body.querySelectorAll('pre').forEach(pre => pre.setAttribute('tabindex', '0'));
  doc.body.querySelectorAll('table').forEach(table => {
    const wrap = doc.createElement('div');
    wrap.className = 'overflow-x-auto rounded-lg border border-zinc-800';
    table.replaceWith(wrap);
    wrap.appendChild(table);
  });
  return doc.body.innerHTML;
}

// "<Verb> by <name> · <date, time>", the name the API recorded.
function closedByHtml(verb, name, at) {
  return `<div class="dec-closed-by text-[11px] leading-4 text-zinc-400">${verb} by <span class="dec-closed-name font-medium text-zinc-200">${esc(name)}</span>${at ? ` &middot; ${esc(localTime(at))}` : ''}</div>`;
}

function resolvedHtml(node, data, editable, waiting) {
  const reopen = editable
    ? `<button type="button" class="dec-reopen-btn self-start h-7 px-2.5 rounded-md text-[11px] font-medium bg-zinc-800 hover:bg-zinc-700 active:bg-zinc-600 text-zinc-200 border border-zinc-700 transition ${DEC_FOCUS}">Reopen</button>`
    : '';
  const label = (text, tone) => `<div class="text-[11px] leading-4 font-semibold uppercase tracking-[0.05em] ${tone}">${text}</div>`;
  if (node.status === 'ANSWERED' && data.answer) {
    const a = data.answer;
    const chosen = (data.options || []).find(o => o.key === a.option);
    return `
      <div class="dec-answer flex flex-col gap-2.5">
        ${label('Answer', 'text-emerald-400')}
        ${chosen ? optionCardHtml(chosen, { waiting, subject: data.subject }) : ''}
        ${a.text ? `<div class="text-sm text-zinc-100">${esc(a.text)}</div>` : ''}
        ${a.rationale ? `<div class="text-xs leading-[18px]"><div class="font-semibold text-zinc-300">Rationale</div><div class="text-zinc-400">${esc(a.rationale)}</div></div>` : ''}
        ${closedByHtml('Answered', a.answered_by, a.answered_at)}
        ${reopen}
      </div>`;
  }
  if (node.status === 'WITHDRAWN') {
    return `
      <div class="dec-answer flex flex-col gap-2.5">
        ${label('Withdrawn', 'text-zinc-400')}
        ${data.withdrawn_reason ? `<div class="text-sm text-zinc-100">${esc(data.withdrawn_reason)}</div>` : ''}
        ${data.withdrawn_by ? closedByHtml('Withdrawn', data.withdrawn_by, data.withdrawn_at) : ''}
        ${reopen}
      </div>`;
  }
  return '';
}

const ANSWER_PRIMARY = 'bg-emerald-600 hover:bg-emerald-500 active:bg-emerald-700 text-black disabled:hover:bg-emerald-600';
const ANSWER_DESTRUCTIVE = 'bg-red-600 hover:bg-red-700 active:bg-red-800 text-white';

// Options, custom answer and rationale scroll with the detail. Answer and Withdraw are one pair
// of controls: under the fields from sm up, and below sm a bar fixed to the bottom of the
// viewport, with the picked answer above them; the detail's bottom padding (index.html) keeps
// the last field clear of it.
function answerFormHtml(data, waitingCount) {
  const options = shownOptions(data);
  return `
    <form class="dec-answer-form space-y-3">
      ${options.length ? `
        <div class="space-y-3">
          <div id="dec-options-label" class="text-[11px] font-semibold text-zinc-400 uppercase tracking-wider">Options</div>
          <div class="grid gap-2" role="radiogroup" aria-labelledby="dec-options-label">${options.map((o, i) => optionCardHtml(o, { selectable: true, tabbable: i === 0, waiting: waitingCount, subject: data.subject })).join('')}</div>
        </div>` : ''}
      ${data.allow_custom !== false ? `
        <label class="dec-custom-card block p-3 rounded-lg border border-zinc-800 bg-zinc-900/60 space-y-1.5">
          <span class="block text-xs font-medium text-zinc-300">Custom answer</span>
          <textarea class="dec-custom-text ${TEXTAREA_CLS}" rows="2"></textarea>
        </label>` : ''}
      ${fieldRow('Rationale', `<textarea class="dec-rationale ${TEXTAREA_CLS}" rows="2"></textarea>`)}
      <div class="dec-answer-actions fixed inset-x-0 bottom-0 z-10 flex flex-col gap-2 px-4 pt-2.5 pb-3.5 bg-zinc-900 border-t border-zinc-800 sm:static sm:z-auto sm:p-0 sm:pt-1 sm:bg-transparent sm:border-0">
        <div class="dec-answer-pick-line hidden items-center gap-1.5 min-w-0">
          <span class="flex-shrink-0 text-[11px] leading-4 ${DEC_MUTED}">Answer</span>
          <span class="dec-answer-pick flex-1 min-w-0 truncate text-xs leading-4 font-medium text-zinc-100"></span>
        </div>
        <div class="flex items-center gap-2">
          <button type="submit" class="dec-answer-submit h-8 px-3 rounded-lg text-xs font-semibold transition disabled:opacity-40 disabled:cursor-not-allowed ${ANSWER_PRIMARY} ${DEC_FOCUS}" disabled>Answer</button>
          <span class="flex-1"></span>
          <button type="button" class="dec-withdraw-btn h-7 px-2.5 rounded-md text-[11px] font-medium bg-zinc-900 hover:bg-red-950 active:bg-red-900 text-red-300 border border-red-900/60 transition disabled:opacity-40 disabled:cursor-not-allowed ${DEC_FOCUS}">Withdraw&hellip;</button>
        </div>
      </div>
    </form>`;
}

// Focus once a decision's detail is drawn: 'option' is its first option (else its first
// field), 'heading' its title, and anything else a selector in the detail; the title stands in
// for a target that is not there.
function focusDecisionPage(to) {
  let target = null;
  if (to === 'option') {
    const form = decisionsDetailEl.querySelector('.dec-answer-form');
    target = form && (form.querySelector('.dec-option-card') || form.querySelector('textarea'));
  } else if (to !== 'heading') {
    target = decisionsDetailEl.querySelector(to);
  }
  target = target || decisionsDetailEl.querySelector('.dec-title');
  if (target) target.focus();
}

function takePendingFocus(id) {
  if (!pendingFocus || pendingFocus.id !== id) return;
  focusDecisionPage(pendingFocus.to);
  pendingFocus = null;
}

function closedElsewhereText(node, data) {
  if (node.status === 'ANSWERED' && data.answer) return `${node.id} answered by ${data.answer.answered_by}`;
  return data.withdrawn_by ? `${node.id} withdrawn by ${data.withdrawn_by}` : `${node.id} withdrawn`;
}

// The toast's Show: the held decision's closed record, under the tab it now belongs to.
function showClosedDecision(decisionId) {
  if (heldOpenId === decisionId) heldOpenId = null;
  if (selectedDecisionId !== decisionId) return;
  const tab = decisionTabFor(decisionDetailLastStatus.get(decisionId));
  if (tab !== decisionsTab) setDecisionsTab(tab);
  else renderDecisionDetail(decisionId);
}

// The focused control in the detail as a selector that finds it again in a redraw: its tag, its
// first class (each control's own name in this pane) and the attribute naming its item.
const DETAIL_ITEM_ATTRS = ['data-option-key', 'data-task-id', 'data-asset', 'data-group-id', 'data-id'];
function focusedDetailSelector() {
  const el = document.activeElement;
  if (!el || el === decisionsDetailEl || !decisionsDetailEl.contains(el)) return null;
  const cls = (el.getAttribute('class') || '').trim().split(/\s+/)[0];
  const attr = DETAIL_ITEM_ATTRS.find(a => el.hasAttribute(a));
  return `${el.localName}${cls ? `.${CSS.escape(cls)}` : ''}${attr ? `[${attr}="${CSS.escape(el.getAttribute(attr))}"]` : ''}`;
}

// The detail's Retry reads the selected decision again; one stable function, as paneState keeps
// one handler per function.
function retryDecisionDetail() {
  if (selectedDecisionId) renderDecisionDetail(selectedDecisionId);
}

let detailLoadTimer = null;

function renderDecisionDetail(id) {
  if (renderedDecisionId !== id) {
    renderedDecisionId = null;
    if (heldOpenId !== id) heldOpenId = null;
    decisionsDetailEl.scrollTop = 0;
    if (decisionsNarrow()) decisionsPane.scrollTop = 0;
    // The loading state shows only once the read has been in flight LOADING_DELAY_MS, so a fast
    // answer never flashes it.
    decisionsDetailEl.innerHTML = `<div class="max-w-2xl mx-auto">${pageBarHtml(id)}</div>`;
    clearTimeout(detailLoadTimer);
    detailLoadTimer = setTimeout(() => {
      if (selectedDecisionId !== id || renderedDecisionId === id) return;
      decisionsDetailEl.innerHTML = `<div class="max-w-2xl mx-auto">${pageBarHtml(id)}${paneState('loading')}</div>`;
    }, LOADING_DELAY_MS);
  }
  fetchNodeDetail(id).then(detail => {
    if (selectedDecisionId !== id) return; // a later click superseded this fetch
    clearTimeout(detailLoadTimer);
    // The owner's own answer or withdrawal is in flight: its response picks what shows next.
    if (ownWrites.has(id) && renderedDecisionId === id) return;
    if (!detail) {
      renderedDecisionId = null;
      liftToasts(null);
      decisionsDetailEl.innerHTML = `<div class="max-w-2xl mx-auto">${pageBarHtml()}${paneState('empty', `${id} not found.`)}</div>`;
      return;
    }
    const node = detail.node;
    const data = (node.frontmatter && node.frontmatter.decision) || {};
    const isOpen = node.status === 'OPEN';
    const editable = canEdit();
    const attachments = (node.frontmatter && node.frontmatter.attachments) || [];
    const firstRender = renderedDecisionId !== id;

    // A decision seen OPEN on screen, now something else, was closed by another session while
    // the owner was looking: the form stays, disabled with its draft, and a toast offers the
    // closed record; nothing moves until the owner acts.
    const previousStatus = decisionDetailLastStatus.get(id);
    decisionDetailLastStatus.set(id, node.status);
    if (previousStatus === 'OPEN' && !isOpen && !firstRender && !ownWrites.has(id)) {
      heldOpenId = id;
      toast(closedElsewhereText(node, data), { action: { label: 'Show', run: () => showClosedDecision(id) } });
    }
    const held = heldOpenId === id;
    // A decision opened by its location (a link, Back, a reload) brings its own tab with it.
    if (firstRender && !held && decisionTabFor(node.status) !== decisionsTab) setDecisionsTab(decisionTabFor(node.status));
    const detailKey = `${JSON.stringify(detail)}${held ? ':held' : ''}`;
    if (!firstRender && detailKey === renderedDetailKey) {
      takePendingFocus(id);
      return;
    }
    const rerender = () => {
      renderedDetailKey = null;
      renderDecisionDetail(id);
    };

    const raisedByHtml = data.raised_by ? `
      <div class="dec-raised-by flex items-center gap-1.5 text-xs leading-4 text-zinc-400">Raised by ${idLink(data.raised_by)}</div>` : '';

    // §6.4: open decisions offer editing of blocked tasks. A withdrawn/answered decision only
    // ever shows the read-only list -- removing a block from one that already resolved
    // wouldn't change anything downstream, since the tasks it unblocked have already moved on.
    // Its group reads "Was blocking", each node with the status it holds now.
    const canEditBlocks = isOpen && editable;
    const waiting = detail.dependent_details || [];
    const waitingGroup = `${node.id}::waiting`;
    const waitingHtml = (waiting.length || canEditBlocks) ? `
      <div class="dec-waiting space-y-2">
        ${renderGroupHeader(waitingGroup, isOpen || held ? 'Waiting on this' : 'Was blocking', waiting.length, false)}
        <div class="space-y-2${groupCollapsed(waitingGroup, false) ? ' hidden' : ''}">
          ${waiting.map(t => waitingRowHtml(t, canEditBlocks)).join('')}
          ${canEditBlocks ? `<button type="button" class="dec-block-add h-7 px-2.5 inline-flex items-center gap-1 rounded-md text-[11px] font-medium text-emerald-400 hover:text-emerald-300 hover:bg-zinc-800 active:bg-zinc-700 border border-dashed border-zinc-700 transition ${DEC_FOCUS}">${renderIcon('plus', 'w-3 h-3')}Add task</button>` : ''}
        </div>
      </div>` : '';

    // The context section reads first and expanded, above the options -- not folded into the
    // generic collapsed "Sections" group every other section (rare on a decision) still uses.
    const allSections = detail.sections || [];
    const contextSection = allSections.find(s => s.key === 'context');
    const otherSections = allSections.filter(s => s.key !== 'context');
    const contextHtml = contextSection ? `
      <div class="space-y-2.5">
        <div class="text-[11px] leading-4 font-semibold text-zinc-400 uppercase tracking-[0.05em]">Context</div>
        <div class="dec-context-body px-4 py-3.5 space-y-2.5 rounded-lg bg-zinc-900/30 border border-zinc-800">${contextBodyHtml(contextSection.content)}</div>
      </div>` : '';
    const sectionsHtml = renderSections(otherSections, node.id);
    const attachmentsHtml = renderAttachments(node, attachments, editable);
    const actionHtml = (isOpen || held) ? (editable ? answerFormHtml(data, waiting.length) : '') : resolvedHtml(node, data, editable, waiting.length);
    // An attachment added moves focus to its name, one detached to Attach.
    const afterAttachmentChange = (added) => {
      pendingFocus = { id, to: added ? `.att-card[data-asset="${CSS.escape(added.asset)}"] .att-name` : '.att-add-btn' };
      rerender();
    };

    const scroll = [decisionsDetailEl.scrollTop, decisionsPane.scrollTop];
    const inPlace = renderedDecisionId === id;
    // The same decision drawn again keeps focus on the same control, found by what it is rather
    // than where it sits, since a row arriving above it moves it.
    const refocus = inPlace ? focusedDetailSelector() : null;
    // The header line: id, status icon, priority, then Attach and Copy ID. Below sm it is the page
    // bar, held under the toolbar while the page scrolls, where the priority shows only when it
    // is not 50; from sm up it sits 8px above the title (its -12px margin collapses with the
    // column's 20px).
    const priority = node.priority ?? 50;
    decisionsDetailEl.innerHTML = `
      <div class="max-w-2xl mx-auto space-y-5">
        <div class="dec-page-bar flex items-center gap-2 max-sm:sticky max-sm:top-0 max-sm:z-10 max-sm:-mx-4 max-sm:-mt-5 max-sm:px-4 max-sm:py-2 max-sm:bg-zinc-950 max-sm:border-b max-sm:border-zinc-800 sm:-mb-3">
          ${drawerButtonHtml()}
          <div class="dec-header-meta flex-1 min-w-0 flex flex-wrap items-center gap-x-2 gap-y-1.5">
            ${idLink(node.id, 'decision')}
            ${decisionStatusIcon(node.status)}
            <span class="inline-flex${priority === 50 ? ' max-sm:hidden' : ''}">${priorityPill(priority)}</span>
          </div>
          ${editable ? attachButtonHtml() : ''}
          ${copyIdButton(node.id)}
        </div>
        <div class="space-y-2">
          <h1 tabindex="-1" class="dec-title text-xl leading-7 font-bold text-white focus:outline-none">${esc(node.title)}</h1>
          ${raisedByHtml}
        </div>
        ${contextHtml}
        ${waitingHtml}
        ${sectionsHtml}
        ${actionHtml}
        ${attachmentsHtml}
      </div>
    `;
    renderedDecisionId = id;
    renderedDetailKey = detailKey;
    if (inPlace) [decisionsDetailEl.scrollTop, decisionsPane.scrollTop] = scroll;

    attachSectionToggleHandlers(decisionsDetailEl);
    attachGroupHeaderHandlers(decisionsDetailEl, rerender);
    attachCopyHandlers(decisionsDetailEl);
    wireAttachmentControls(decisionsDetailEl, node, attachments, editable, afterAttachmentChange);

    // Removing a node sends focus to + Add task; adding one sends it to the new row.
    decisionsDetailEl.querySelectorAll('.dec-block-remove').forEach(btn => {
      btn.addEventListener('click', () => {
        const taskId = btn.getAttribute('data-task-id');
        submitWrite(btn, { method: 'POST', path: `/api/decisions/${node.id}/blocks`, body: { remove: [taskId] } }).then(() => {
          pendingFocus = { id, to: '.dec-block-add' };
          toast(`${taskId} no longer waits on ${node.id}`, { tone: 'success' });
          refreshDecisionsData();
        });
      });
    });
    const addBlockBtn = decisionsDetailEl.querySelector('.dec-block-add');
    if (addBlockBtn) addBlockBtn.addEventListener('click', () => openTaskPicker(node.id));

    wireDecisionAnswerForm(decisionsDetailEl, node.id, data, waiting, held);
    if (!decisionsDetailEl.querySelector('.dec-answer-form')) liftToasts(null);

    const reopenBtn = decisionsDetailEl.querySelector('.dec-reopen-btn');
    if (reopenBtn) {
      // Reopen clears the answer record alone; the decision is open again under the Open tab,
      // with focus on its first option.
      reopenBtn.addEventListener('click', () => {
        submitWrite(reopenBtn, { method: 'POST', path: `/api/decisions/${node.id}/reopen` }).then(() => {
          pendingFocus = { id: node.id, to: 'option' };
          toast(`${node.id} reopened`, { tone: 'success' });
          if (decisionsTab === 'open') refreshDecisionsData();
          else setDecisionsTab('open');
        });
      });
    }

    const again = refocus && decisionsDetailEl.querySelector(refocus);
    if (again) again.focus();
    takePendingFocus(id);
  }).catch(e => {
    if (selectedDecisionId !== id) return;
    clearTimeout(detailLoadTimer);
    renderedDecisionId = null;
    liftToasts(null);
    // A real 404 already resolved to null above and took the "not found" branch; anything
    // reaching here is the request itself failing (network, 5xx), so it offers Retry.
    decisionsDetailEl.innerHTML = `<div class="max-w-2xl mx-auto">${pageBarHtml(id)}${paneState('error', e.message, retryDecisionDetail)}</div>`;
  });
}

// The answer form keeps its draft per decision. Picking an option keeps any custom text; typing
// a custom answer unchecks the option, since it is written instead of one. An abandon or defer
// effect names itself on Answer and is confirmed before it is sent.
function wireDecisionAnswerForm(root, decisionId, data, waiting, held) {
  const form = root.querySelector('.dec-answer-form');
  if (!form) return;
  const options = data.options || [];
  const submitBtn = form.querySelector('.dec-answer-submit');
  const pickLine = form.querySelector('.dec-answer-pick-line');
  const pick = form.querySelector('.dec-answer-pick');
  const cards = Array.from(form.querySelectorAll('.dec-option-card'));
  const customText = form.querySelector('.dec-custom-text');
  const rationale = form.querySelector('.dec-rationale');
  const draft = decisionDrafts.get(decisionId) || {};
  let chosenOption = draft.option || null;
  if (customText) customText.value = draft.text || '';
  rationale.value = draft.rationale || '';

  const chosen = () => options.find(o => o.key === chosenOption) || null;
  const customAnswer = () => (customText ? customText.value.trim() : '');
  function effect() {
    if (chosen()) return chosen().effect || 'none';
    return customAnswer() ? (data.custom_effect || 'none') : 'none';
  }
  function save() {
    decisionDrafts.set(decisionId, { option: chosenOption, text: customText ? customText.value : '', rationale: rationale.value });
  }

  function paint() {
    const picked = chosen() ? chosen().label : customAnswer();
    cards.forEach((c, i) => {
      const on = c.getAttribute('data-option-key') === chosenOption;
      c.setAttribute('aria-checked', String(on));
      c.setAttribute('tabindex', on || (!chosenOption && i === 0) ? '0' : '-1');
    });
    pickLine.classList.toggle('max-sm:flex', Boolean(picked));
    pick.textContent = picked;
    liftToasts(picked ? 'picked' : 'empty');
    // A write in flight owns the button's label (its spinner) until the response.
    if (submitBtn.getAttribute('aria-busy') === 'true') return;
    const fx = effect();
    const destructive = Boolean(picked) && DESTRUCTIVE_EFFECTS.includes(fx);
    submitBtn.disabled = held || !picked;
    submitBtn.textContent = destructive ? `Answer — ${fx} ${nodeCount(waiting.length)}` : 'Answer';
    ANSWER_PRIMARY.split(' ').forEach(c => submitBtn.classList.toggle(c, !destructive));
    ANSWER_DESTRUCTIVE.split(' ').forEach(c => submitBtn.classList.toggle(c, destructive));
  }

  function submit() {
    if (submitBtn.disabled) return;
    const at = decisionRowIndex(decisionId);
    const body = { option: chosenOption, text: customAnswer(), rationale: rationale.value.trim() };
    const message = `${decisionId} answered: ${chosen() ? chosen().label : body.text}`;
    const request = { method: 'POST', path: `/api/decisions/${decisionId}/answer`, body };
    const fx = effect();
    if (DESTRUCTIVE_EFFECTS.includes(fx)) {
      submitBtn.focus();
      confirmEffect(decisionId, fx, waiting, request, () => afterDecisionClosed(decisionId, message, at));
      return;
    }
    ownWrites.add(decisionId);
    submitWrite(submitBtn, request).then(() => afterDecisionClosed(decisionId, message, at));
  }

  cards.forEach((card, i) => {
    card.addEventListener('click', () => {
      chosenOption = card.getAttribute('data-option-key');
      paint();
      save();
    });
    // ←/→ move between options and check the one focus lands on (Space checks the focused
    // option for free -- a real <button> fires its own click on Space). ↑/↓ belong to the list.
    card.addEventListener('keydown', (e) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (!step) return;
      e.preventDefault();
      const next = cards[(i + step + cards.length) % cards.length];
      next.focus();
      next.click();
    });
  });
  if (customText) {
    customText.addEventListener('input', () => {
      if (customAnswer()) chosenOption = null;
      paint();
      save();
    });
  }
  rationale.addEventListener('input', save);
  form.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || !(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    submit();
  });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    submit();
  });
  form.querySelector('.dec-withdraw-btn').addEventListener('click', () => openWithdrawDialog(decisionId, waiting.length));
  paint();
  if (held) form.querySelectorAll('button, textarea').forEach(el => { el.disabled = true; });
}

function effectNodeHtml(t) {
  return `<div class="dec-effect-node flex items-center gap-2 px-2.5 py-2 rounded-lg bg-zinc-900/60 border border-zinc-800">${statusIcon(t.status || 'STALE')}${idLink(t.id, t.kind)}<span class="flex-1 min-w-0 truncate text-xs leading-4 text-zinc-400">${esc(t.title || '')}</span></div>`;
}

// An abandon or defer is confirmed before it is sent, and nothing undoes it after: the confirm
// lists the nodes it hits and opens on Cancel; Escape, Close and Cancel return to Answer.
function confirmEffect(decisionId, effect, waiting, request, done) {
  const verb = effect === 'abandon' ? 'Abandon' : 'Defer';
  const key = `effect:${decisionId}`;
  // Built fresh each time: what it lists and sends is the form's state now.
  dialogDrafts.delete(key);
  formDialog({
    key,
    title: `${verb} ${nodeCount(waiting.length)}?`,
    submitLabel: verb,
    destructive: true,
    bodyHtml: `<div class="space-y-2">${waiting.map(effectNodeHtml).join('')}</div>`,
    onSubmit: async (panel, close, write) => {
      ownWrites.add(decisionId);
      await write(request.method, request.path, request.body);
      close();
      await done();
    },
  });
}

// §6.4: a decision is withdrawn with a reason, so this is a full dialog (a text field) rather
// than confirmDialog's plain message-only shape. Every node waiting on it stops waiting, which
// its effect pill says.
function openWithdrawDialog(decisionId, waitingCount) {
  formDialog({
    title: `Withdraw ${decisionId}?`,
    submitLabel: 'Withdraw',
    destructive: true,
    bodyHtml: `
      <div class="flex">${effectPillHtml('drop_edge', waitingCount)}</div>
      ${fieldRow('Reason', `<textarea class="wd-reason ${TEXTAREA_CLS}" rows="2"></textarea>`)}
    `,
    onSubmit: async (panel, close, write) => {
      const reason = panel.querySelector('.wd-reason').value.trim();
      const at = decisionRowIndex(decisionId);
      ownWrites.add(decisionId);
      await write('POST', `/api/decisions/${decisionId}/withdraw`, { reason });
      close();
      await afterDecisionClosed(decisionId, `${decisionId} withdrawn`, at);
    }
  });
}

// + Add task's picker lists the tasks the server says can wait on the decision, read afresh on
// every open and filtered by id or title as typed. ↓ from the field enters the list, ↑/↓ move
// through it with the selection following focus, a click selects a row, and Enter or Add adds
// the selected one. The query is the dialog's draft, so Cancel keeps it.
const taskPickerLoads = new WeakMap();
// The open picker's read, which its error state's Retry runs again; one picker shows at a time.
let taskPickerLoad = null;
function retryTaskCandidates() {
  if (taskPickerLoad) taskPickerLoad();
}

// An option is one focus stop, so its status icon is named with the row instead of taking
// focus of its own.
function taskPickerRowHtml(t) {
  return `<div role="option" tabindex="-1" aria-selected="false" data-task-id="${esc(t.id)}" class="dbk-option group flex items-center gap-2 px-2.5 py-2 rounded-lg border cursor-pointer transition bg-zinc-900/60 border-zinc-800 hover:bg-zinc-900 hover:border-zinc-700 aria-selected:bg-zinc-800 aria-selected:border-zinc-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-emerald-400">${statusIcon(t.display).replace(' tabindex="0"', '')}<span class="flex-shrink-0 whitespace-nowrap font-mono ${ID_TEXT}">${esc(t.id)}</span><span class="flex-1 min-w-0 truncate text-xs leading-4 text-zinc-400 group-aria-selected:text-zinc-200">${esc(t.title)}</span></div>`;
}

function wireTaskPicker(panel, decisionId) {
  const query = panel.querySelector('.dbk-query');
  const list = panel.querySelector('.dbk-results');
  const add = panel.querySelector('.dlg-submit');
  const rows = () => Array.from(list.querySelectorAll('.dbk-option'));
  let candidates = null;
  let failure = null;
  let slow = false;
  let selected = null;
  let loadTimer = null;

  // The selected row is the list's one tab stop, the first row while none is; Add needs one.
  function select(taskId) {
    selected = taskId;
    rows().forEach((row, i) => {
      const on = row.getAttribute('data-task-id') === taskId;
      row.setAttribute('aria-selected', String(on));
      row.setAttribute('tabindex', on || (!taskId && i === 0) ? '0' : '-1');
    });
    add.disabled = !taskId;
  }
  function draw() {
    const typed = query.value.trim();
    const needle = typed.toLowerCase();
    if (failure) list.innerHTML = paneState('error', failure, retryTaskCandidates);
    else if (!candidates) list.innerHTML = slow ? paneState('loading') : '';
    else {
      const shown = candidates.filter(t => t.id.toLowerCase().includes(needle) || t.title.toLowerCase().includes(needle));
      list.innerHTML = shown.length ? shown.map(taskPickerRowHtml).join('')
        : paneState('empty', typed ? `No tasks match “${typed}”.` : 'No tasks to add.');
    }
    select(rows().some(r => r.getAttribute('data-task-id') === selected) ? selected : null);
  }
  function moveTo(row) {
    select(row.getAttribute('data-task-id'));
    row.focus();
  }
  function load() {
    candidates = null;
    failure = null;
    slow = false;
    draw();
    clearTimeout(loadTimer);
    loadTimer = setTimeout(() => {
      slow = true;
      draw();
    }, LOADING_DELAY_MS);
    api('GET', `/api/decisions/${decisionId}/candidates`).then((res) => {
      candidates = res.items;
    }, (e) => {
      failure = e.message;
    }).finally(() => {
      clearTimeout(loadTimer);
      draw();
    });
  }

  query.addEventListener('input', draw);
  query.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowDown' || !rows().length) return;
    e.preventDefault();
    moveTo(rows()[0]);
  });
  list.addEventListener('click', (e) => {
    const row = e.target.closest && e.target.closest('.dbk-option');
    if (row) moveTo(row);
  });
  // Tab into the list lands on its tab stop, which the selection follows.
  list.addEventListener('focusin', (e) => {
    if (e.target.classList.contains('dbk-option')) select(e.target.getAttribute('data-task-id'));
  });
  list.addEventListener('keydown', (e) => {
    const row = e.target.closest && e.target.closest('.dbk-option');
    if (!row) return;
    const all = rows();
    const at = all.indexOf(row);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const next = all[at + (e.key === 'ArrowDown' ? 1 : -1)];
      if (next) moveTo(next);
      else if (e.key === 'ArrowUp') query.focus();
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      moveTo(row);
      if (e.key === 'Enter') add.click();
    }
  });
  return load;
}

function openTaskPicker(decisionId) {
  const { panel } = formDialog({
    title: `Add task to ${decisionId}`,
    submitLabel: 'Add',
    bodyHtml: `
      ${fieldRow('Task', `<span class="relative block"><span class="absolute inset-y-0 left-0 pl-2.5 flex items-center pointer-events-none ${DEC_MUTED}">${renderIcon('search', 'w-3.5 h-3.5')}</span><input type="text" autocomplete="off" aria-controls="dbk-results" class="dbk-query ${INPUT_CLS} pl-8"></span>`)}
      <div id="dbk-results" role="listbox" aria-label="Tasks" class="dbk-results flex flex-col gap-1 max-h-56 overflow-y-auto pr-1"></div>
    `,
    onMount: (dialogPanel) => taskPickerLoads.set(dialogPanel, wireTaskPicker(dialogPanel, decisionId)),
    onSubmit: async (dialogPanel, close, write) => {
      const taskId = dialogPanel.querySelector('.dbk-option[aria-selected="true"]').getAttribute('data-task-id');
      await write('POST', `/api/decisions/${decisionId}/blocks`, { add: [taskId] });
      close();
      pendingFocus = { id: decisionId, to: `.dec-waiting-row a.id-link[data-id="${CSS.escape(taskId)}"]` };
      toast(`${taskId} now waits on ${decisionId}`, { tone: 'success' });
      refreshDecisionsData();
    },
  });
  taskPickerLoad = taskPickerLoads.get(panel);
  taskPickerLoad();
  panel.querySelector('.dbk-query').focus();
}

// The closed decision's place in its list, read when the write is sent: a push can take its row
// out before the response comes back.
function decisionRowIndex(decisionId) {
  return Math.max(0, visibleDecisionRows().findIndex(d => d.id === decisionId));
}

// Answer and Withdraw end the same way, from the write's response: the tab stays, the row that
// followed the closed one is selected (the one before it when it was last) with focus on its
// first option, and with no row left focus lands on the tab, or below sm on the Drawer button.
async function afterDecisionClosed(decisionId, message, at) {
  decisionDrafts.delete(decisionId);
  await refreshDecisionsData();
  const rows = visibleDecisionRows().filter(d => d.id !== decisionId);
  const next = rows[Math.min(at, rows.length - 1)];
  ownWrites.delete(decisionId);
  toast(message, { tone: 'success' });
  if (next) {
    pendingFocus = { id: next.id, to: 'option' };
    navigate({ view: window.VIEW_MODES.DECISIONS, id: next.id }, { replace: true });
    return;
  }
  navigate({ view: window.VIEW_MODES.DECISIONS }, { replace: true });
  const target = decisionsNarrow()
    ? decisionsDetailEl.querySelector('.dec-drawer-btn')
    : decisionsTabsEl.querySelector(`[data-tab="${decisionsTab}"]`);
  if (target) target.focus();
}

// A decision raised from the page opens under the Open tab.
function openCreatedDecision(decisionId) {
  if (decisionsTab === 'open') refreshDecisionsData();
  else setDecisionsTab('open');
  navigate({ view: window.VIEW_MODES.DECISIONS, id: decisionId });
}

// With nothing selected the detail is empty from sm up; below sm it is the page, which shows
// what the list shows in place of rows. A list refresh redraws it, and the control that had
// focus (the Drawer button, Retry) has it again.
function renderNoSelection() {
  renderedDecisionId = null;
  liftToasts(null);
  const state = decisionsListStateHtml();
  const refocus = focusedDetailSelector();
  decisionsDetailEl.innerHTML = `<div class="max-w-2xl mx-auto">${pageBarHtml()}${state ? `<div class="sm:hidden">${state}</div>` : ''}</div>`;
  const again = refocus && decisionsDetailEl.querySelector(refocus);
  if (again) again.focus();
}

function renderDecisionsView() {
  renderDecisionsTabs();
  renderDecisionsList();
  if (selectedDecisionId) {
    renderDecisionDetail(selectedDecisionId);
    return;
  }
  // /decisions names no decision: it resolves to the top row in place, so Back never returns
  // to the bare path. Below sm that is the top open decision; another tab in the drawer only
  // changes the drawer.
  const top = visibleDecisionRows()[0];
  if (top && decisionsLoaded && !decisionsLoadFailed && (decisionsTab === 'open' || !decisionsNarrow())) {
    navigate({ view: window.VIEW_MODES.DECISIONS, id: top.id }, { replace: true });
    return;
  }
  renderNoSelection();
}


// New decision dialog -----------------------------------------------------------------------

function decisionOptionRowHtml(index, key = '', label = '', description = '', recommended = false) {
  // The key/label/description inputs were named only by their placeholder, which is not an
  // accessible name (axe aria-input-field-name); a placeholder disappears the moment there is
  // a value, an aria-label never does.
  return `
    <div class="dec-opt-row space-y-1.5 p-2 rounded-lg border border-zinc-800" data-opt-index="${index}">
      <div class="flex items-center gap-2">
        <input type="text" class="dec-opt-key ${INPUT_CLS} font-mono" style="max-width:6rem" placeholder="key" aria-label="Option key" value="${esc(key)}">
        <input type="text" class="dec-opt-label ${INPUT_CLS}" placeholder="Label" aria-label="Option label" value="${esc(label)}">
        <label class="flex items-center gap-1 text-[10px] text-zinc-400 flex-shrink-0">
          <input type="radio" name="dec-opt-recommend" class="dec-opt-recommend" ${recommended ? 'checked' : ''}>Recommended
        </label>
        <button type="button" class="dec-opt-remove p-1 rounded text-zinc-500 hover:text-red-400 hover:bg-zinc-800 flex-shrink-0" aria-label="Remove option">${renderIcon('x', 'w-3 h-3')}</button>
      </div>
      <input type="text" class="dec-opt-desc ${INPUT_CLS}" placeholder="Description" aria-label="Option description" value="${esc(description)}">
    </div>
  `;
}

function openNewDecisionDialog() {
  let optIndex = 0;
  formDialog({
    title: 'New decision',
    submitLabel: 'Raise',
    bodyHtml: `
      ${fieldRow('Question', `<input type="text" required class="nd-question ${INPUT_CLS}">`)}
      ${fieldRow('Context', `<textarea class="nd-context ${TEXTAREA_CLS}" rows="3"></textarea>`)}
      <div class="space-y-2">
        <div class="flex items-center justify-between">
          <span class="text-[11px] font-semibold text-zinc-400 uppercase tracking-wider">Options</span>
          <button type="button" class="nd-opt-add text-[11px] text-emerald-400 hover:text-emerald-300">+ Add option</button>
        </div>
        <div class="nd-opt-rows space-y-2"></div>
      </div>
      <label class="flex items-center gap-2 text-xs text-zinc-300"><input type="checkbox" class="nd-allow-custom rounded border-zinc-600 bg-zinc-950 text-emerald-500 focus:ring-emerald-500" checked>Allow a custom answer</label>
      ${fieldRow('Blocks tasks', `<input type="text" class="nd-blocks ${INPUT_CLS} font-mono" list="nd-blocks-list"><datalist id="nd-blocks-list">${visibleTaskRows().map(t => `<option value="${esc(t.id)}">${esc(t.title)}</option>`).join('')}</datalist>`)}
    `,
    onMount: (panel) => {
      const rows = panel.querySelector('.nd-opt-rows');
      panel.querySelector('.nd-opt-add').addEventListener('click', () => {
        rows.insertAdjacentHTML('beforeend', decisionOptionRowHtml(optIndex++));
        wireOptionRemove(rows);
      });
      function wireOptionRemove(root) {
        root.querySelectorAll('.dec-opt-remove').forEach(btn => {
          btn.onclick = () => btn.closest('.dec-opt-row').remove();
        });
      }
    },
    onSubmit: async (panel, close) => {
      const question = panel.querySelector('.nd-question').value.trim();
      if (!question) throw new Error('Question is required.');
      const options = Array.from(panel.querySelectorAll('.dec-opt-row')).map(row => ({
        key: row.querySelector('.dec-opt-key').value.trim(),
        label: row.querySelector('.dec-opt-label').value.trim(),
        description: row.querySelector('.dec-opt-desc').value.trim(),
        recommend: row.querySelector('.dec-opt-recommend').checked,
      })).filter(o => o.key && o.label);
      const recommended = options.find(o => o.recommend);
      const res = await api('POST', '/api/decisions', {
        question,
        context: panel.querySelector('.nd-context').value.trim() || undefined,
        options: options.map(({ key, label, description }) => ({ key, label, description })),
        recommend: recommended ? recommended.key : undefined,
        allow_custom: panel.querySelector('.nd-allow-custom').checked,
        blocks: panel.querySelector('.nd-blocks').value.split(',').map(s => s.trim()).filter(Boolean),
      });
      toast(`${res.id} raised`, { tone: 'success' });
      close();
      openCreatedDecision(res.id);
    }
  });
}

// Patch the toolbar's "+ New" menu (edit.js) to add a Decision entry, and re-render it once
// so the button already on screen (edit.js calls renderNewMenu() at its own load time) picks
// it up immediately rather than waiting for the next unrelated re-render.
if (typeof renderNewMenu === 'function') {
  const previousRenderNewMenu = renderNewMenu;
  renderNewMenu = function () {
    previousRenderNewMenu();
    const pop = document.getElementById('new-menu-pop');
    if (!pop || pop.querySelector('[data-new-kind="decision"]')) return;
    const item = document.createElement('button');
    item.type = 'button';
    item.setAttribute('role', 'menuitem');
    item.setAttribute('data-new-kind', 'decision');
    item.className = 'w-full text-left px-2 py-1.5 rounded text-xs text-zinc-200 hover:bg-zinc-800';
    item.textContent = 'Decision';
    item.addEventListener('click', () => {
      pop.classList.add('hidden');
      const menuBtn = document.getElementById('new-menu-btn');
      if (menuBtn) menuBtn.setAttribute('aria-expanded', 'false');
      openNewDecisionDialog();
    });
    pop.appendChild(item);
  };
  renderNewMenu();
}
