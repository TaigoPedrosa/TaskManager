// Filters (mirrored into the URL hash so a view is shareable)
const NO_REPO = '(none)';
const NO_SPEC = '(none)';
const NO_PHASE = '(none)';
// Every dimension below is tri-state: absent from the Map = no opinion (neutral),
// 'include' or 'exclude' otherwise. Maps are mutated in place, never reassigned (except
// statusMode, read fresh every time and never captured by reference elsewhere) -- the
// popover controls below capture repoMode/modelMode/specMode by reference at init, so a
// fresh Map here would silently desync from them.
const filters = {
  statusMode: new Map(), phaseMode: new Map(), repoMode: new Map(), modelMode: new Map(),
  specMode: new Map(), scoreMin: null, scoreMax: null, q: ''
};
let scoreBounds = { min: 0, max: 100 };

// A task passes a dimension if (no includes, or it matches at least one include) and it
// matches no exclude. `values` holds every value the task carries for that dimension --
// one for status/repo/spec, several for acceptable_models -- so "matching" means any overlap.
function dimensionPasses(modeMap, values) {
  if (values.some(v => modeMap.get(v) === 'exclude')) return false;
  const anyIncludes = [...modeMap.values()].some(m => m === 'include');
  return !anyIncludes || values.some(v => modeMap.get(v) === 'include');
}

function structuralFilterActive() {
  return filters.statusMode.size > 0 || filters.phaseMode.size > 0 || filters.repoMode.size > 0 ||
    filters.modelMode.size > 0 || filters.specMode.size > 0 ||
    filters.scoreMin !== null || filters.scoreMax !== null;
}

function taskPasses(t) {
  if (!dimensionPasses(filters.statusMode, [displayOf(t)])) return false;
  if (!dimensionPasses(filters.phaseMode, [t.phase || NO_PHASE])) return false;
  if (!dimensionPasses(filters.repoMode, [t.target_repo || NO_REPO])) return false;
  if (!dimensionPasses(filters.modelMode, t.acceptable_models || [])) return false;
  if (!dimensionPasses(filters.specMode, [t._specId])) return false;
  if (filters.scoreMin !== null && typeof t.score === 'number' && t.score < filters.scoreMin) return false;
  if (filters.scoreMax !== null && typeof t.score === 'number' && t.score > filters.scoreMax) return false;
  return true;
}

function textMatches(n) {
  return n.title.toLowerCase().includes(filters.q) || n.id.toLowerCase().includes(filters.q);
}

function textAccepts(n, parentTextOk) {
  return filters.q === '' || parentTextOk || textMatches(n);
}

function nodeVisible(n, parentTextOk = false) {
  const textOk = textAccepts(n, parentTextOk);
  if (n.kind === 'task') return textOk && taskPasses(n);
  return (n.children || []).some(c => nodeVisible(c, textOk)) || (!structuralFilterActive() && textOk);
}

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
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

function writeHash() {
  const p = new URLSearchParams();
  const status = modeEntries(filters.statusMode);
  if (status.inc.length) p.set('status', status.inc.join(','));
  if (status.exc.length) p.set('xstatus', status.exc.join(','));
  const phase = modeEntries(filters.phaseMode);
  if (phase.inc.length) p.set('phase', phase.inc.join(','));
  if (phase.exc.length) p.set('xphase', phase.exc.join(','));
  const repo = modeEntries(filters.repoMode);
  if (repo.inc.length) p.set('repo', repo.inc.join(','));
  if (repo.exc.length) p.set('xrepo', repo.exc.join(','));
  const model = modeEntries(filters.modelMode);
  if (model.inc.length) p.set('model', model.inc.join(','));
  if (model.exc.length) p.set('xmodel', model.exc.join(','));
  const spec = modeEntries(filters.specMode);
  if (spec.inc.length) p.set('spec', spec.inc.join(','));
  if (spec.exc.length) p.set('xspec', spec.exc.join(','));
  if (filters.scoreMin !== null) p.set('smin', String(filters.scoreMin));
  if (filters.scoreMax !== null) p.set('smax', String(filters.scoreMax));
  if (filters.q) p.set('q', filters.q);
  try {
    history.replaceState(null, '', p.toString() ? '#' + p : location.pathname + location.search);
  } catch (e) {
    console.error('Could not update the URL hash:', e);
  }
}

function renderAll() {
  writeHash();
  updateStatsDigest();
  renderFilterControls();
  renderTree(treeData);
  renderUnifiedDocument();
  applyGraphFilter();
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


// Status Digest Bar
function passesOtherDimensions(t, exclude) {
  if (exclude !== 'status' && !dimensionPasses(filters.statusMode, [displayOf(t)])) return false;
  if (exclude !== 'phase' && !dimensionPasses(filters.phaseMode, [t.phase || NO_PHASE])) return false;
  if (exclude !== 'repo' && !dimensionPasses(filters.repoMode, [t.target_repo || NO_REPO])) return false;
  if (exclude !== 'model' && !dimensionPasses(filters.modelMode, t.acceptable_models || [])) return false;
  if (exclude !== 'spec' && !dimensionPasses(filters.specMode, [t._specId])) return false;
  if (filters.scoreMin !== null && typeof t.score === 'number' && t.score < filters.scoreMin) return false;
  if (filters.scoreMax !== null && typeof t.score === 'number' && t.score > filters.scoreMax) return false;
  if (filters.q !== '' && !textMatches(t)) return false;
  return true;
}

// Counts reflect every OTHER active filter (the dimension's own is excluded) so they read
// as "how many tasks would show", not a frozen snapshot. `valuesOf` returns the task's own
// value(s) for that dimension -- several for acceptable_models, one otherwise.
function computeDimensionCounts(dimension, valuesOf) {
  const counts = {};
  collectTasks(treeData).forEach(t => {
    if (!passesOtherDimensions(t, dimension)) return;
    valuesOf(t).forEach(v => { counts[v] = (counts[v] || 0) + 1; });
  });
  return counts;
}

function updateStatsDigest() {
  // A rebuild replaces every chip with a new DOM node, so the one that had focus (Enter on a
  // status chip is a normal way to apply a filter) would otherwise drop to BODY and a
  // following Shift+Enter would land on nothing. Re-find and refocus its replacement by the
  // status code it carries, __all__ standing in for the "All tasks" chip.
  const focusedCode = statsDigest.contains(document.activeElement)
    ? document.activeElement.dataset.statusCode
    : null;

  statsDigest.innerHTML = '';
  const counts = computeDimensionCounts('status', t => [displayOf(t)]);
  const total = collectTasks(treeData).filter(t => passesOtherDimensions(t, 'status')).length;
  const allActive = filters.statusMode.size === 0;
  const totalChip = document.createElement('button');
  totalChip.className = `flex items-center gap-1 px-2 py-1 rounded-md border text-xs transition focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 ${allActive ? 'bg-zinc-800 text-white border-zinc-700' : 'bg-zinc-900/60 text-zinc-400 border-zinc-800 hover:bg-zinc-800'}`;
  totalChip.title = 'All tasks';
  totalChip.dataset.statusCode = '__all__';
  totalChip.setAttribute('aria-label', `All tasks: ${total}`);
  totalChip.setAttribute('aria-pressed', String(allActive));
  totalChip.innerHTML = `${renderIcon('layers', 'w-3.5 h-3.5')}<strong>${total}</strong>`;
  totalChip.onclick = () => {
    filters.statusMode.clear();
    renderAll();
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
      chip.className = `st-toggle st-${code} ${modeClass} flex items-center gap-1 px-1.5 py-1 rounded-md text-xs transition hover:brightness-125 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400 ${count === 0 && !mode ? 'opacity-50' : ''}`;
      chip.title = `${theme.label} · ${triModeLabel(mode)}`;
      chip.dataset.statusCode = code;
      chip.setAttribute('aria-label', `Status ${theme.label}: ${triModeLabel(mode)}`);
      chip.innerHTML = `${renderIcon(theme.icon, 'w-3.5 h-3.5')}<strong>${count}</strong>`;
      triStateHandlers(chip, () => filters.statusMode.get(code), (mode) => {
        if (mode === null) filters.statusMode.delete(code); else filters.statusMode.set(code, mode);
        renderAll();
      });
      statsDigest.appendChild(chip);
    });
  });

  if (focusedCode) {
    const toFocus = statsDigest.querySelector(`[data-status-code="${CSS.escape(focusedCode)}"]`);
    if (toFocus) toFocus.focus();
  }
}


// Also stamps each task with the id/title of the spec it descends from (NO_SPEC when it
// hangs off a standalone plan), so the spec filter needs no server round trip.
function collectTasks(nodes, specCtx = null, out = []) {
  nodes.forEach(n => {
    if (n.kind === 'task') {
      n._specId = specCtx ? specCtx.id : NO_SPEC;
      n._specTitle = specCtx ? specCtx.title : 'No spec';
      out.push(n);
    }
    const nextCtx = n.kind === 'spec' ? { id: n.id, title: n.title } : specCtx;
    collectTasks(n.children || [], nextCtx, out);
  });
  return out;
}

// Every non-task node too (a dependency can target a spec, plan or task) -- used by the
// dependency picker's <datalist>, never by the task-only filters above.
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
// `getOptions` is re-read on every render so it always reflects the live tree, never a
// stale snapshot.
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
      ? '<div class="px-2 py-1.5 text-zinc-500 text-xs">No options</div>'
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

  function bounds() {
    const min = filters.scoreMin ?? scoreBounds.min;
    const max = filters.scoreMax ?? scoreBounds.max;
    return { min, max };
  }

  function render() {
    const { min, max } = bounds();
    const active = filters.scoreMin !== null || filters.scoreMax !== null;
    const btn = container.querySelector('.sf-btn');
    btn.querySelector('.sf-label').textContent = `Score: ${Math.round(min)}-${Math.round(max)}`;
    btn.className = `sf-btn h-8 min-w-[6.5rem] flex items-center justify-between gap-1 px-2.5 rounded-lg border text-xs transition ${active ? 'bg-zinc-800 text-white border-emerald-600' : 'bg-zinc-950 text-zinc-300 border-zinc-800 hover:bg-zinc-900'}`;
    btn.setAttribute('aria-expanded', String(open));
    container.querySelector('.sf-pop').classList.toggle('hidden', !open);
    const lo = container.querySelector('.sf-lo');
    const hi = container.querySelector('.sf-hi');
    lo.min = hi.min = scoreBounds.min;
    lo.max = hi.max = scoreBounds.max;
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
    filters.scoreMin = Math.min(Number(e.target.value), filters.scoreMax ?? scoreBounds.max);
    renderAll();
  });
  container.querySelector('.sf-hi').addEventListener('input', (e) => {
    filters.scoreMax = Math.max(Number(e.target.value), filters.scoreMin ?? scoreBounds.min);
    renderAll();
  });
  container.querySelector('.sf-reset').addEventListener('click', () => {
    filters.scoreMin = null;
    filters.scoreMax = null;
    renderAll();
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
  onChange: renderAll,
  getOptions: () => {
    const counts = computeDimensionCounts('repo', t => [t.target_repo || NO_REPO]);
    return [...new Set(collectTasks(treeData).map(t => t.target_repo || NO_REPO))]
      .sort().map(v => ({ value: v, label: v, count: counts[v] || 0 }));
  },
});
const modelTriState = createTriStatePopover(modelFilterEl, {
  label: 'Model',
  dimension: 'Model',
  modeMap: filters.modelMode,
  onChange: renderAll,
  getOptions: () => {
    const counts = computeDimensionCounts('model', t => (t.acceptable_models && t.acceptable_models.length ? t.acceptable_models : []));
    return [...new Set(collectTasks(treeData).flatMap(t => t.acceptable_models || []))]
      .sort().map(v => ({ value: v, label: v, count: counts[v] || 0 }));
  },
});
const specTriState = createTriStatePopover(specFilterEl, {
  label: 'Spec',
  dimension: 'Spec',
  modeMap: filters.specMode,
  onChange: renderAll,
  getOptions: () => {
    const counts = computeDimensionCounts('spec', t => [t._specId]);
    const byId = new Map();
    collectTasks(treeData).forEach(t => byId.set(t._specId, t._specTitle));
    return [...byId.entries()].sort((a, b) => a[1].localeCompare(b[1])).map(([value, label]) => ({ value, label, count: counts[value] || 0 }));
  },
});
const phaseTriState = createTriStatePopover(phaseFilterEl, {
  label: 'Phase',
  dimension: 'Phase',
  modeMap: filters.phaseMode,
  onChange: renderAll,
  getOptions: () => {
    const counts = computeDimensionCounts('phase', t => [t.phase || NO_PHASE]);
    return Object.keys(window.PHASE_THEMES).map(code => ({
      value: code, label: window.PHASE_THEMES[code].label, count: counts[code] || 0,
    }));
  },
});
const scoreFilter = createScoreFilter(scoreFilterEl);


function populateFilterOptions() {
  const tasks = collectTasks(treeData);
  const scores = tasks.map(t => t.score).filter(s => typeof s === 'number');
  scoreBounds = scores.length ? { min: Math.min(...scores), max: Math.max(...scores) } : { min: 0, max: 100 };
}

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
  clearFiltersBtn.classList.toggle('hidden', !(structuralFilterActive() || filters.q !== ''));
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
  renderAll();
});


// Legend
function renderLegend() {
  const statusRows = window.STATUS_GROUPS.map(group => {
    const rows = Object.values(window.STATUS_THEMES).filter(t => t.group === group.code).map(t => `
      <div class="flex items-start gap-2 py-1">
        <div class="w-36 flex-shrink-0">${statusChip(t.code)}</div>
        <p class="text-xs text-zinc-300">${esc(t.description)}</p>
      </div>
    `).join('');
    return `<div><div class="text-[10px] uppercase tracking-wider text-zinc-400 mt-2">${esc(group.label)}</div>${rows}</div>`;
  }).join('');
  const phaseRows = Object.values(window.PHASE_THEMES).map(t => `
    <div class="flex items-start gap-2 py-1">
      <div class="w-36 flex-shrink-0">${phaseChip(t.code)}</div>
      <p class="text-xs text-zinc-300">${esc(t.description)}</p>
    </div>
  `).join('');
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


function applyGraphFilter() {
  if (!visNodesDS) return;
  visNodesDS.update(graphData.nodes.map(n => {
    const dim = n.kind === 'task' && !(taskPasses(n) && textAccepts(n, false));
    return { id: n.id, opacity: dim ? 0.2 : 1 };
  }));
}

