// Filters, and the router that keeps them in the URL with the view and its selection.
// Visibility itself -- which rows this produces, which facet counts they add up to -- is the
// store's job (live: the server; static: store.js's own port of web/visibility.py); this file
// only owns the controls, the URL round trip, and forwarding the result to the store.
// NO_REPO/NO_SPEC/NO_PHASE are store.js's own sentinel constants (loaded before this file);
// declaring them again here would be a duplicate top-level const in the same script scope.
// Every dimension below is tri-state: absent from the Map = no opinion (neutral),
// 'include' or 'exclude' otherwise. Maps are mutated in place, never reassigned (except
// statusMode, read fresh every time and never captured by reference elsewhere) -- the
// popover controls below capture repoMode/modelMode/specMode by reference at init, so a
// fresh Map here would silently desync from them.
const filters = {
  statusMode: new Map(), phaseMode: new Map(), repoMode: new Map(), modelMode: new Map(),
  specMode: new Map(), scoreMin: null, scoreMax: null, q: ''
};

function anyFilterActive() {
  return filters.statusMode.size > 0 || filters.phaseMode.size > 0 || filters.repoMode.size > 0 ||
    filters.modelMode.size > 0 || filters.specMode.size > 0 ||
    filters.scoreMin !== null || filters.scoreMax !== null;
}

function readFilters(p) {
  filters.statusMode = new Map();
  (p.get('status') || '').split(',').filter(c => window.STATUS_THEMES[c]).forEach(c => filters.statusMode.set(c, 'include'));
  (p.get('xstatus') || '').split(',').filter(c => window.STATUS_THEMES[c]).forEach(c => filters.statusMode.set(c, 'exclude'));
  filters.phaseMode.clear();
  (p.get('phase') || '').split(',').filter(c => window.PHASE_THEMES[c]).forEach(c => filters.phaseMode.set(c, 'include'));
  (p.get('xphase') || '').split(',').filter(c => window.PHASE_THEMES[c]).forEach(c => filters.phaseMode.set(c, 'exclude'));
  filters.repoMode.clear();
  (p.get('repo') || '').split(',').filter(Boolean).forEach(v => filters.repoMode.set(v, 'include'));
  (p.get('xrepo') || '').split(',').filter(Boolean).forEach(v => filters.repoMode.set(v, 'exclude'));
  filters.modelMode.clear();
  (p.get('model') || '').split(',').filter(Boolean).forEach(v => filters.modelMode.set(v, 'include'));
  (p.get('xmodel') || '').split(',').filter(Boolean).forEach(v => filters.modelMode.set(v, 'exclude'));
  filters.specMode.clear();
  (p.get('spec') || '').split(',').filter(Boolean).forEach(v => filters.specMode.set(v, 'include'));
  (p.get('xspec') || '').split(',').filter(Boolean).forEach(v => filters.specMode.set(v, 'exclude'));
  const smin = p.get('smin');
  const smax = p.get('smax');
  filters.scoreMin = smin !== null && smin !== '' ? Number(smin) : null;
  filters.scoreMax = smax !== null && smax !== '' ? Number(smax) : null;
  filters.q = (p.get('q') || '').toLowerCase();
  searchBox.value = filters.q;
}

function modeEntries(modeMap) {
  const inc = [...modeMap.entries()].filter(([, m]) => m === 'include').map(([v]) => v);
  const exc = [...modeMap.entries()].filter(([, m]) => m === 'exclude').map(([v]) => v);
  return { inc, exc };
}

// The same shape both navigate() (a URL) and setFilters() (the store's F, protocol §"Filters,
// visibility, facets and edges") need: only the keys with an opinion, values already strings.
function filtersToF() {
  const F = {};
  const status = modeEntries(filters.statusMode);
  if (status.inc.length) F.status = status.inc.join(',');
  if (status.exc.length) F.xstatus = status.exc.join(',');
  const phase = modeEntries(filters.phaseMode);
  if (phase.inc.length) F.phase = phase.inc.join(',');
  if (phase.exc.length) F.xphase = phase.exc.join(',');
  const repo = modeEntries(filters.repoMode);
  if (repo.inc.length) F.repo = repo.inc.join(',');
  if (repo.exc.length) F.xrepo = repo.exc.join(',');
  const model = modeEntries(filters.modelMode);
  if (model.inc.length) F.model = model.inc.join(',');
  if (model.exc.length) F.xmodel = model.exc.join(',');
  const spec = modeEntries(filters.specMode);
  if (spec.inc.length) F.spec = spec.inc.join(',');
  if (spec.exc.length) F.xspec = spec.exc.join(',');
  if (filters.scoreMin !== null) F.smin = String(filters.scoreMin);
  if (filters.scoreMax !== null) F.smax = String(filters.scoreMax);
  if (filters.q) F.q = filters.q;
  return F;
}

// The page's state is its URL: /<view>[/<id>] names the view and what it has selected, the
// query string holds the filters. A static export, opened from a file, has no paths of its own,
// so it carries the same path and query after the "#": #/decisions?status=OPEN.
function locationParts() {
  const hash = location.hash.slice(1);
  // Links from before the router carried their filters in the hash: /#status=REVIEWING.
  if (hash && !hash.startsWith('/')) return { path: isStaticMode ? '' : location.pathname, query: hash };
  if (!isStaticMode) return { path: location.pathname, query: location.search.slice(1) };
  const q = hash.indexOf('?');
  return q < 0 ? { path: hash, query: '' } : { path: hash.slice(0, q), query: hash.slice(q + 1) };
}

function decodeSegment(segment) {
  try {
    return decodeURIComponent(segment);
  } catch (e) {
    return segment;
  }
}

// Sibling order in Document and the tree: 'progress' (the default, absent from the query) or
// 'priority' (?sort=priority). Not a filter, so the store never sees it.
const SORTED_VIEWS = [window.VIEW_MODES.DOCUMENT, window.VIEW_MODES.GRAPH];
let sortOrder = 'progress';

function readLocation() {
  const { path, query } = locationParts();
  const [view, id] = path.split('/').filter(Boolean).map(decodeSegment);
  const known = Object.values(window.VIEW_MODES).includes(view) &&
    !(isStaticMode && view === window.VIEW_MODES.WAVES);
  const params = new URLSearchParams(query);
  const sort = params.get('sort') === 'priority' ? 'priority' : 'progress';
  params.delete('sort');
  return {
    view: known ? view : window.VIEW_MODES.DOCUMENT,
    id: known && id ? id : null,
    filters: Object.fromEntries(params),
    sort,
  };
}

function pathFor(view, id = null) {
  const path = `/${view}${id ? `/${encodeURIComponent(id)}` : ''}`;
  return isStaticMode ? `#${path}` : path;
}

// Brings the screen to a location, touching only what differs from what it shows now: a
// filter change re-renders without re-entering its view, and a location written by navigate()
// and read back by popstate changes nothing.
function applyLocation({ view, id, filters: F, sort }) {
  if (new URLSearchParams(F).toString() !== new URLSearchParams(filtersToF()).toString()) {
    readFilters(new URLSearchParams(F));
    window.tmStore.setFilters(filtersToF());
    scheduleRender();
    scheduleWavesRefetch();
  }
  if (view === window.VIEW_MODES.DECISIONS && id !== selectedDecisionId) {
    selectedDecisionId = id;
    if (currentMode === view) renderDecisionsView();
  }
  if (view !== currentMode) setViewMode(view);
  if (sort !== sortOrder) {
    sortOrder = sort;
    scheduleRender();
  }
  renderSortControl();
  applyNodeLocation(view, id);
}

// A view switch is a new history entry; pass `replace` for a change within the view (a
// filter, a sort, a reset) so Back still leaves the view.
function navigate({ view = currentMode, id = null, filters: F = filtersToF(), sort = sortOrder } = {}, { replace = false } = {}) {
  const here = readLocation();
  const params = new URLSearchParams(F);
  if (sort === 'priority') params.set('sort', sort);
  const query = params.toString();
  // "/" and "/document" both open Document, so a write that keeps the view and its selection
  // keeps the path it found.
  const path = !isStaticMode && view === here.view && id === here.id ? location.pathname : pathFor(view, id);
  try {
    history[replace ? 'replaceState' : 'pushState'](null, '', query ? `${path}?${query}` : path);
  } catch (e) {
    // A browser can refuse a history write on a page opened from a file; the screen still
    // follows, only the address bar does not.
    console.error('Could not update the URL:', e);
  }
  applyLocation({ view, id, filters: F, sort });
}

// Rewritten in place as well as applied: an old hash link typed over the current page arrives
// here rather than as a load.
function reapplyLocation() {
  navigate(readLocation(), { replace: true });
}
window.addEventListener('popstate', reapplyLocation);
if (isStaticMode) window.addEventListener('hashchange', reapplyLocation);

// Every control below calls this, never renderAll() directly, on an actual filter change:
// the URL, the store's own filters and the DOM must all move together, exactly once.
function applyFilterChange() {
  navigate({ ...readLocation(), filters: filtersToF() }, { replace: true });
  window.tmStore.setFilters(filtersToF());
  scheduleRender();
  // Waves reads filters.specMode itself (waveSpecFilter) rather than taking it as an argument,
  // so a spec include/exclude change needs its own nudge -- the store's own patch never reaches
  // waves.js, since setFilters() (static: recomputeStatic, live: sendSubscribe) never reports
  // statusesChanged.
  scheduleWavesRefetch();
}

function renderAll() {
  updateStatsDigest();
  renderFilterControls();
  renderTree();
  renderUnifiedDocument();
}


// One gesture grammar for every tri-state control (status chips, and each row of the
// model/spec/repo popovers): click toggles neutral <-> include; double-click sets exclude
// directly. A double-click still fires two click events first, so a click is held for
// TRI_STATE_DBLCLICK_MS waiting for a second one before it commits. Keyboard: Enter/Space
// acts as a click, Shift+Enter excludes -- preventDefault always, so a real <button>'s own
// native Enter/Space-to-click synthesis never fires a second, duplicate toggle underneath.
const TRI_STATE_DBLCLICK_MS = 220;

function triModeLabel(mode) {
  return mode === 'include' ? 'included' : mode === 'exclude' ? 'excluded' : 'not filtered';
}

function triStateHandlers(el, getMode, setMode) {
  let pendingClick = null;

  function commitClick() {
    setMode(getMode() ? null : 'include');
  }

  el.addEventListener('click', () => {
    if (pendingClick) {
      clearTimeout(pendingClick);
      pendingClick = null;
      return;
    }
    pendingClick = setTimeout(() => {
      pendingClick = null;
      commitClick();
    }, TRI_STATE_DBLCLICK_MS);
  });

  el.addEventListener('dblclick', () => {
    if (pendingClick) {
      clearTimeout(pendingClick);
      pendingClick = null;
    }
    setMode('exclude');
  });

  el.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    if (e.key === 'Enter' && e.shiftKey) setMode('exclude');
    else commitClick();
  });
}


// Status Digest Bar: counts come from the store's facets (every other active filter already
// applied, one dimension at a time -- see web/visibility.py's `facets`), never recomputed here.
function updateStatsDigest() {
  // A rebuild replaces every chip with a new DOM node, so the one that had focus (Enter on a
  // status chip is a normal way to apply a filter) would otherwise drop to BODY and a
  // following Shift+Enter would land on nothing. Re-find and refocus its replacement by the
  // status code it carries, __all__ standing in for the "All work" chip.
  const focusedCode = statsDigest.contains(document.activeElement)
    ? document.activeElement.dataset.statusCode
    : null;

  statsDigest.innerHTML = '';
  const counts = window.tmStore.facets.status || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const allActive = filters.statusMode.size === 0;
  const totalChip = document.createElement('button');
  totalChip.className = `flex items-center gap-1.5 px-1.5 py-1 rounded-full border text-xs leading-4 transition ${FOCUS_RING} ${allActive ? 'bg-zinc-800 text-white border-zinc-700' : 'bg-zinc-900/60 text-zinc-400 border-zinc-800 hover:bg-zinc-800'}`;
  totalChip.dataset.statusCode = '__all__';
  totalChip.setAttribute('aria-label', `All work: ${total}`);
  totalChip.setAttribute('aria-pressed', String(allActive));
  totalChip.innerHTML = `${renderIcon('layers', 'w-3 h-3')}<span class="font-medium${allActive ? ' text-zinc-200' : ''}">All work</span><strong class="font-mono">${total}</strong>`;
  totalChip.onclick = () => {
    filters.statusMode.clear();
    applyFilterChange();
  };
  statsDigest.appendChild(totalChip);

  window.STATUS_GROUPS.forEach((group, groupIndex) => {
    if (groupIndex > 0) {
      const divider = document.createElement('div');
      divider.className = 'w-px h-4 bg-zinc-800 mx-0.5 flex-shrink-0';
      statsDigest.appendChild(divider);
    }
    Object.keys(window.STATUS_THEMES).filter(code => window.STATUS_THEMES[code].group === group.code).forEach(code => {
      const theme = getTheme(code);
      const count = counts[code] || 0;
      const mode = filters.statusMode.get(code);
      const chip = document.createElement('button');
      const modeClass = mode === 'include' ? 'st-mode-include' : mode === 'exclude' ? 'st-mode-exclude' : '';
      // A zero count keeps its icon's colour and drops only the fill, so it never dims below AA.
      const zero = count === 0 && !mode;
      chip.className = `st-toggle st-${code} ${modeClass} ${zero ? 'st-zero' : ''} flex items-center gap-1 px-1.5 py-1 rounded-full text-xs leading-4 transition hover:brightness-125 ${FOCUS_RING}`;
      chip.title = `${theme.label} · ${triModeLabel(mode)}`;
      chip.dataset.statusCode = code;
      chip.setAttribute('aria-label', `Status ${theme.label}: ${triModeLabel(mode)}`);
      chip.innerHTML = `${renderIcon(theme.icon, 'w-3 h-3')}<strong class="font-mono ${zero ? 'text-zinc-400' : ''}">${count}</strong>`;
      triStateHandlers(chip, () => filters.statusMode.get(code), (mode) => {
        if (mode === null) filters.statusMode.delete(code); else filters.statusMode.set(code, mode);
        applyFilterChange();
      });
      statsDigest.appendChild(chip);
    });
  });

  if (focusedCode) {
    const toFocus = statsDigest.querySelector(`[data-status-code="${CSS.escape(focusedCode)}"]`);
    if (toFocus) toFocus.focus();
  }
}


// Every non-task node the tree currently holds (a dependency can target a spec, plan or
// task) -- used by the dependency picker's <datalist>, never by the filters above.
function collectAllNodes(nodes, out = []) {
  nodes.forEach(n => {
    out.push(n);
    collectAllNodes(n.children || [], out);
  });
  return out;
}

// Generic tri-state popover: a button ("Label" or "Label +2 −1") opening a list of
// value rows. Each row follows the same click/double-click gesture as a status chip on its
// own label, plus a three-icon segmented control (plus/minus/circle) that sets a mode
// directly; whichever icon matches the row's current mode is shown filled.
// `getOptions` is re-read on every render so it always reflects the store's latest facets,
// never a stale snapshot.
function createTriStatePopover(container, { label, dimension, getOptions, modeMap, onChange }) {
  let open = false;

  function summary() {
    const inc = [...modeMap.values()].filter(m => m === 'include').length;
    const exc = [...modeMap.values()].filter(m => m === 'exclude').length;
    if (!inc && !exc) return label;
    const parts = [];
    if (inc) parts.push(`+${inc}`);
    if (exc) parts.push(`−${exc}`);
    return `${label} ${parts.join(' ')}`;
  }

  function setValueMode(value, mode) {
    if (mode === null) modeMap.delete(value); else modeMap.set(value, mode);
    onChange();
  }

  // §6.2: exactly two toggle buttons, no third neutral one -- clicking the already-selected
  // one is how a value returns to neutral. Plus is green only when included, minus is red
  // only when excluded; unselected is always the same gray, never the other's colour.
  function triBtn(mode, iconName, isActive, activeClasses, title) {
    return `
      <button type="button" data-mode="${mode}" title="${title}" aria-label="${title}" aria-pressed="${isActive}"
        class="w-5 h-5 flex items-center justify-center rounded transition hover:bg-zinc-700 hover:text-zinc-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 ${isActive ? activeClasses : 'text-zinc-500'}">
        ${renderIcon(iconName, 'w-3 h-3')}
      </button>`;
  }

  function renderOptions() {
    const options = getOptions();
    const pop = container.querySelector('.tri-pop');
    pop.innerHTML = options.length === 0
      ? '<div class="px-2 py-1.5 text-zinc-400 text-xs">No options</div>'
      : options.map(o => {
        const mode = modeMap.get(o.value);
        return `
          <div class="tri-row flex items-center justify-between gap-2 px-2 py-1 rounded hover:bg-zinc-800 cursor-pointer text-xs text-zinc-200"
            data-value="${esc(o.value)}"
            title="${esc(dimension)} ${esc(o.label)} · ${triModeLabel(mode)}">
            <span class="truncate min-w-0 flex-1">${esc(o.label)}</span>
            <span class="text-zinc-400 flex-shrink-0">(${o.count})</span>
            <span class="flex items-center gap-0.5 flex-shrink-0">
              ${triBtn('include', 'plus', mode === 'include', 'bg-emerald-600 text-white hover:bg-emerald-500', 'Include ' + o.label)}
              ${triBtn('exclude', 'minus', mode === 'exclude', 'bg-red-600 text-white hover:bg-red-500', 'Exclude ' + o.label)}
            </span>
          </div>
        `;
      }).join('');
    pop.querySelectorAll('.tri-row').forEach(row => {
      const value = row.getAttribute('data-value');
      row.querySelectorAll('button[data-mode]').forEach(btn => {
        btn.addEventListener('click', (e) => {
          // Stopped so the row's own click/dblclick gesture (below) never fires a second,
          // conflicting toggle underneath a button click.
          e.stopPropagation();
          // Clicking the selected one clears it; clicking the other one selects it and
          // deselects whichever was selected before (there is only ever one mode per value).
          const current = modeMap.get(value);
          setValueMode(value, current === btn.dataset.mode ? null : btn.dataset.mode);
        });
        btn.addEventListener('dblclick', (e) => e.stopPropagation());
      });
      // The row's own label area follows the same click/double-click gesture as a status
      // chip; the two buttons are its keyboard path, since a row that were itself a focusable
      // button-role widget wrapping real buttons would be a nested-interactive axe violation.
      triStateHandlers(row, () => modeMap.get(value), (mode) => setValueMode(value, mode));
    });
  }

  function close(returnFocus) {
    if (!open) return;
    open = false;
    render();
    if (returnFocus) container.querySelector('.tri-btn-main').focus();
  }

  function render() {
    const active = modeMap.size > 0;
    const btn = container.querySelector('.tri-btn-main');
    btn.querySelector('.tri-label').textContent = summary();
    btn.className = `tri-btn-main h-8 min-w-[6.5rem] flex items-center justify-between gap-1 px-2.5 rounded-lg border text-xs transition focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 ${active ? 'bg-zinc-800 text-white border-emerald-600' : 'bg-zinc-950 text-zinc-300 border-zinc-800 hover:bg-zinc-900'}`;
    btn.setAttribute('aria-expanded', String(open));
    const pop = container.querySelector('.tri-pop');
    pop.classList.toggle('hidden', !open);
    renderOptions();
    if (open) clampToViewport(pop);
  }

  container.innerHTML = `
    <button type="button" class="tri-btn-main" aria-haspopup="true">
      <span class="tri-label">${esc(label)}</span>
      ${renderIcon('chevron-down', 'w-3 h-3')}
    </button>
    <div class="tri-pop absolute z-30 mt-1 w-56 max-w-[calc(100vw-1rem)] max-h-64 overflow-y-auto rounded-lg border border-zinc-700 bg-zinc-900 shadow-2xl p-1 hidden" role="group" aria-label="${esc(dimension)} values"></div>
  `;
  container.querySelector('.tri-btn-main').addEventListener('click', (e) => {
    e.stopPropagation();
    open = !open;
    render();
  });
  document.addEventListener('click', (e) => {
    if (open && !container.contains(e.target)) close(false);
  });
  container.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && open) {
      e.preventDefault();
      close(true);
    }
  });

  render();
  return { render };
}

// Score filter: a button ("Score: min-max") opening a dual-handle range slider (two
// overlapping <input type=range>, thumbs only are clickable -- the standard CSS shape
// for a two-sided slider with no extra dependency).

function createScoreFilter(container) {
  let open = false;

  function scoreBounds() {
    return window.tmStore.facets.score || { min: 0, max: 100 };
  }

  function bounds() {
    const b = scoreBounds();
    const min = filters.scoreMin ?? b.min;
    const max = filters.scoreMax ?? b.max;
    return { min, max };
  }

  function render() {
    const { min, max } = bounds();
    const b = scoreBounds();
    const active = filters.scoreMin !== null || filters.scoreMax !== null;
    const btn = container.querySelector('.sf-btn');
    btn.querySelector('.sf-label').textContent = `Score: ${Math.round(min)}-${Math.round(max)}`;
    btn.className = `sf-btn h-8 min-w-[6.5rem] flex items-center justify-between gap-1 px-2.5 rounded-lg border text-xs transition ${active ? 'bg-zinc-800 text-white border-emerald-600' : 'bg-zinc-950 text-zinc-300 border-zinc-800 hover:bg-zinc-900'}`;
    btn.setAttribute('aria-expanded', String(open));
    container.querySelector('.sf-pop').classList.toggle('hidden', !open);
    const lo = container.querySelector('.sf-lo');
    const hi = container.querySelector('.sf-hi');
    lo.min = hi.min = b.min;
    lo.max = hi.max = b.max;
    lo.value = min;
    hi.value = max;
    container.querySelector('.sf-lo-val').textContent = Math.round(min);
    container.querySelector('.sf-hi-val').textContent = Math.round(max);
  }

  container.innerHTML = `
    <button type="button" class="sf-btn" aria-haspopup="true">
      <span class="sf-label">Score</span>
      ${renderIcon('chevron-down', 'w-3 h-3')}
    </button>
    <div class="sf-pop absolute z-30 mt-1 w-56 rounded-lg border border-zinc-700 bg-zinc-900 shadow-2xl p-3 space-y-2 hidden">
      <div class="flex items-center justify-between text-[11px] text-zinc-400">
        <span>Min: <span class="sf-lo-val font-mono text-zinc-200"></span></span>
        <span>Max: <span class="sf-hi-val font-mono text-zinc-200"></span></span>
      </div>
      <div class="relative h-4">
        <input type="range" class="sf-lo absolute inset-x-0 top-1/2 -translate-y-1/2 w-full" step="0.01">
        <input type="range" class="sf-hi absolute inset-x-0 top-1/2 -translate-y-1/2 w-full" step="0.01">
      </div>
      <button type="button" class="sf-reset text-[11px] text-zinc-400 underline hover:text-white">Reset</button>
    </div>
  `;
  container.querySelector('.sf-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    open = !open;
    render();
  });
  container.querySelector('.sf-lo').addEventListener('input', (e) => {
    filters.scoreMin = Math.min(Number(e.target.value), filters.scoreMax ?? scoreBounds().max);
    applyFilterChange();
  });
  container.querySelector('.sf-hi').addEventListener('input', (e) => {
    filters.scoreMax = Math.max(Number(e.target.value), filters.scoreMin ?? scoreBounds().min);
    applyFilterChange();
  });
  container.querySelector('.sf-reset').addEventListener('click', () => {
    filters.scoreMin = null;
    filters.scoreMax = null;
    applyFilterChange();
  });
  document.addEventListener('click', (e) => {
    if (open && !container.contains(e.target)) {
      open = false;
      render();
    }
  });

  render();
  return { render };
}


const repoTriState = createTriStatePopover(repoFilter, {
  label: 'Repo',
  dimension: 'Repo',
  modeMap: filters.repoMode,
  onChange: applyFilterChange,
  getOptions: () => Object.entries(window.tmStore.facets.repo || {})
    .map(([value, count]) => ({ value, label: value, count }))
    .sort((a, b) => a.label.localeCompare(b.label)),
});
const modelTriState = createTriStatePopover(modelFilterEl, {
  label: 'Model',
  dimension: 'Model',
  modeMap: filters.modelMode,
  onChange: applyFilterChange,
  // acceptable_models is a list: the store's own facets already count a task once per model
  // it accepts, so this is a plain value/count read, not a per-task overlap check.
  getOptions: () => Object.entries(window.tmStore.facets.model || {})
    .map(([value, count]) => ({ value, label: value, count }))
    .sort((a, b) => a.label.localeCompare(b.label)),
});
const specTriState = createTriStatePopover(specFilterEl, {
  label: 'Spec',
  dimension: 'Spec',
  modeMap: filters.specMode,
  onChange: applyFilterChange,
  getOptions: () => Object.entries(window.tmStore.facets.spec || {})
    .map(([value, count]) => {
      const row = window.tmStore.rows.get(value);
      const label = value === NO_SPEC ? 'No spec' : (row ? row.title : value);
      return { value, label, count };
    })
    .sort((a, b) => a.label.localeCompare(b.label)),
});
const phaseTriState = createTriStatePopover(phaseFilterEl, {
  label: 'Phase',
  dimension: 'Phase',
  modeMap: filters.phaseMode,
  onChange: applyFilterChange,
  getOptions: () => {
    const counts = window.tmStore.facets.phase || {};
    return Object.keys(window.PHASE_THEMES).map(code => ({
      value: code, label: window.PHASE_THEMES[code].label, count: counts[code] || 0,
    }));
  },
});
const scoreFilter = createScoreFilter(scoreFilterEl);


// Below `sm` the filter controls stay off the toolbar's one row until this button opens them;
// at `sm` and up `filter-controls-group`'s own `sm:flex` shows them regardless of this state,
// which is what keeps 768/1440 unchanged.
let filtersPanelOpen = false;

function activeFilterCount() {
  return filters.phaseMode.size + filters.repoMode.size + filters.modelMode.size +
    filters.specMode.size + (filters.scoreMin !== null || filters.scoreMax !== null ? 1 : 0);
}

function renderFiltersToggle() {
  const count = activeFilterCount();
  filtersToggleBtn.querySelector('.filters-toggle-label').textContent = count ? `Filters (${count})` : 'Filters';
  filtersToggleBtn.setAttribute('aria-expanded', String(filtersPanelOpen));
  filterControlsGroup.classList.toggle('hidden', !filtersPanelOpen);
}

filtersToggleBtn.addEventListener('click', () => {
  filtersPanelOpen = !filtersPanelOpen;
  renderFiltersToggle();
});

function renderFilterControls() {
  repoTriState.render();
  modelTriState.render();
  specTriState.render();
  phaseTriState.render();
  scoreFilter.render();
  clearFiltersBtn.classList.toggle('hidden', !(anyFilterActive() || filters.q !== ''));
  renderFiltersToggle();
}

clearFiltersBtn.addEventListener('click', () => {
  filters.statusMode.clear();
  filters.phaseMode.clear();
  filters.repoMode.clear();
  filters.modelMode.clear();
  filters.specMode.clear();
  filters.scoreMin = null;
  filters.scoreMax = null;
  filters.q = '';
  searchBox.value = '';
  applyFilterChange();
});


// The sort control, drawn like the view switcher; only Document and Graph, the two lists it
// orders, show it. A segment shows itself picked from the click, ahead of the re-sort.
const sortControl = document.getElementById('sort-control');
const SORT_BTN_BASE = `h-full px-2.5 flex items-center rounded-md font-medium transition ${FOCUS_RING}`;

function renderSortControl() {
  sortControl.classList.toggle('hidden', !SORTED_VIEWS.includes(currentMode));
  sortControl.querySelectorAll('[data-sort]').forEach(btn => {
    const picked = btn.dataset.sort === sortOrder;
    btn.setAttribute('aria-pressed', String(picked));
    btn.className = `${SORT_BTN_BASE} ${picked ? 'bg-zinc-800 text-white shadow-sm' : 'text-zinc-400 hover:text-white'}`;
  });
}

// Below sm (Tailwind's 640px) the control ends the Filters panel's controls, so it adds no row
// to the toolbar there; from sm up it leads the actions group.
const SM_MIN_PX = 640;
const sortActionsGroup = sortControl.parentNode;

function placeSortControl() {
  const home = window.innerWidth >= SM_MIN_PX ? sortActionsGroup : filterControlsGroup;
  if (sortControl.parentNode !== home) home.insertBefore(sortControl, home === sortActionsGroup ? home.firstChild : null);
}

window.addEventListener('resize', placeSortControl);
placeSortControl();

// The picked segment again resets the lists: Progress, from the top.
sortControl.querySelectorAll('[data-sort]').forEach(btn => btn.addEventListener('click', () => {
  const again = btn.dataset.sort === sortOrder;
  if (again) {
    documentPane.scrollTop = 0;
    treeList.scrollTop = 0;
  }
  navigate({ ...readLocation(), sort: again ? 'progress' : btn.dataset.sort }, { replace: true });
}));


// Legend: each status and phase icon beside its name.
function legendRow(colourCls, theme) {
  return `<div class="flex items-center gap-2 py-1"><span class="${colourCls} inline-flex flex-shrink-0">${renderIcon(theme.icon, 'w-3.5 h-3.5')}</span><span class="text-xs text-zinc-300">${esc(theme.label)}</span></div>`;
}

function renderLegend() {
  const statusRows = window.STATUS_GROUPS.map(group => {
    const rows = Object.values(window.STATUS_THEMES).filter(t => t.group === group.code)
      .map(t => legendRow(`st-text st-${t.code}`, t)).join('');
    return `<div><div class="text-[10px] uppercase tracking-wider text-zinc-400 mt-2">${esc(group.label)}</div>${rows}</div>`;
  }).join('');
  const phaseRows = Object.values(window.PHASE_THEMES).map(t => legendRow(`st-text ph-${t.code}`, t)).join('');
  legendBody.innerHTML = `${statusRows}<div><div class="text-[10px] uppercase tracking-wider text-zinc-400 mt-2">Phases</div>${phaseRows}</div>`;
}

function setLegendOpen(open) {
  legendPanel.classList.toggle('hidden', !open);
  legendBtn.setAttribute('aria-expanded', String(open));
}

legendBtn.addEventListener('click', () => setLegendOpen(legendPanel.classList.contains('hidden')));
legendCloseBtn.addEventListener('click', () => setLegendOpen(false));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') setLegendOpen(false);
});
