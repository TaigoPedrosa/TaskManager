// Filters (mirrored into the URL hash so a view is shareable)
const NO_REPO = '(none)';
const NO_SPEC = '(none)';
// statusMode: code -> 'include' | 'exclude'. Absent = no opinion (neutral).
const filters = {
  statusMode: new Map(), repo: '', models: new Set(), specs: new Set(),
  scoreMin: null, scoreMax: null, q: ''
};
let scoreBounds = { min: 0, max: 100 };

function structuralFilterActive() {
  return filters.statusMode.size > 0 || filters.repo !== '' || filters.models.size > 0 ||
    filters.specs.size > 0 || filters.scoreMin !== null || filters.scoreMax !== null;
}

function taskPasses(t) {
  const status = t.virtual_status || t.status;
  const mode = filters.statusMode.get(status);
  if (mode === 'exclude') return false;
  const anyIncludes = [...filters.statusMode.values()].some(m => m === 'include');
  if (anyIncludes && mode !== 'include') return false;
  if (filters.repo !== '' && (t.target_repo || NO_REPO) !== filters.repo) return false;
  if (filters.models.size > 0 && !(t.acceptable_models || []).some(m => filters.models.has(m))) return false;
  if (filters.specs.size > 0 && !filters.specs.has(t._specId)) return false;
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
  filters.repo = p.get('repo') || '';
  // Mutated in place, never reassigned: the multiselect controls captured these Sets
  // by reference at init, so a fresh Set here would silently desync from them.
  filters.models.clear();
  (p.get('model') || '').split(',').filter(Boolean).forEach(v => filters.models.add(v));
  filters.specs.clear();
  (p.get('spec') || '').split(',').filter(Boolean).forEach(v => filters.specs.add(v));
  const smin = p.get('smin');
  const smax = p.get('smax');
  filters.scoreMin = smin !== null && smin !== '' ? Number(smin) : null;
  filters.scoreMax = smax !== null && smax !== '' ? Number(smax) : null;
  filters.q = (p.get('q') || '').toLowerCase();
  searchBox.value = filters.q;
}

function writeHash() {
  const p = new URLSearchParams();
  const inc = [...filters.statusMode.entries()].filter(([, m]) => m === 'include').map(([c]) => c);
  const exc = [...filters.statusMode.entries()].filter(([, m]) => m === 'exclude').map(([c]) => c);
  if (inc.length) p.set('status', inc.join(','));
  if (exc.length) p.set('xstatus', exc.join(','));
  if (filters.repo) p.set('repo', filters.repo);
  if (filters.models.size) p.set('model', [...filters.models].join(','));
  if (filters.specs.size) p.set('spec', [...filters.specs].join(','));
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


// Status Digest Bar
// Three-state toggle per status: neutral (colour fill, no border) -> include (colour fill,
// coloured border) -> exclude (no colour, dim border) -> back to neutral. Counts reflect
// every OTHER active filter (repo/model/spec/score/text) so they read as "how many of this
// status would show", not a frozen snapshot.
function computeFilteredStatusCounts() {
  const tasks = collectTasks(treeData);
  const counts = {};
  let total = 0;
  tasks.forEach(t => {
    if (filters.repo !== '' && (t.target_repo || NO_REPO) !== filters.repo) return;
    if (filters.models.size > 0 && !(t.acceptable_models || []).some(m => filters.models.has(m))) return;
    if (filters.specs.size > 0 && !filters.specs.has(t._specId)) return;
    if (filters.scoreMin !== null && typeof t.score === 'number' && t.score < filters.scoreMin) return;
    if (filters.scoreMax !== null && typeof t.score === 'number' && t.score > filters.scoreMax) return;
    if (filters.q !== '' && !textMatches(t)) return;
    const status = t.virtual_status || t.status;
    counts[status] = (counts[status] || 0) + 1;
    total += 1;
  });
  return { total, counts };
}

function cycleStatusMode(code) {
  const mode = filters.statusMode.get(code);
  if (mode === undefined) filters.statusMode.set(code, 'include');
  else if (mode === 'include') filters.statusMode.set(code, 'exclude');
  else filters.statusMode.delete(code);
}

function updateStatsDigest() {
  statsDigest.innerHTML = '';
  const { total, counts } = computeFilteredStatusCounts();
  const allActive = filters.statusMode.size === 0;
  const totalChip = document.createElement('button');
  totalChip.className = `flex items-center gap-1 px-2 py-1 rounded-md border text-xs transition ${allActive ? 'bg-zinc-800 text-white border-zinc-700' : 'bg-zinc-900/60 text-zinc-400 border-zinc-800 hover:bg-zinc-800'}`;
  totalChip.title = 'All tasks';
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
      chip.className = `st-toggle st-${code} ${modeClass} flex items-center gap-1 px-1.5 py-1 rounded-md text-xs transition hover:brightness-125 ${count === 0 && !mode ? 'opacity-50' : ''}`;
      chip.title = `${theme.label} -- click to include, click again to exclude`;
      chip.setAttribute('aria-label', `${theme.label}: ${count}`);
      chip.setAttribute('aria-pressed', String(mode === 'include'));
      chip.innerHTML = `${renderIcon(theme.icon, 'w-3.5 h-3.5')}<strong>${count}</strong>`;
      chip.onclick = () => {
        cycleStatusMode(code);
        renderAll();
      };
      statsDigest.appendChild(chip);
    });
  });
}


function fillSelect(select, values, current, allLabel) {
  const options = [...new Set([...values, ...(current ? [current] : [])])].sort();
  select.innerHTML = `<option value="">${allLabel}</option>` + options.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
  select.value = current;
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

// Generic multiselect: a button ("Label (n)") opening a checkbox popover. `getOptions`
// is re-read on every render so it always reflects the live tree, never a stale snapshot.

function createMultiSelect(container, { label, getOptions, selected, onChange }) {
  let open = false;

  function renderOptions() {
    const options = getOptions();
    const pop = container.querySelector('.ms-pop');
    pop.innerHTML = options.length === 0
      ? '<div class="px-2 py-1.5 text-zinc-500 text-xs">No options</div>'
      : options.map(o => `
        <label class="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-zinc-800 cursor-pointer text-xs text-zinc-200">
          <input type="checkbox" data-value="${esc(o.value)}" ${selected.has(o.value) ? 'checked' : ''} class="rounded border-zinc-600 bg-zinc-950 text-emerald-500 focus:ring-emerald-500">
          <span class="truncate">${esc(o.label)}</span>
        </label>
      `).join('');
    pop.querySelectorAll('input[type=checkbox]').forEach(cb => {
      cb.onchange = () => {
        const v = cb.getAttribute('data-value');
        if (cb.checked) selected.add(v); else selected.delete(v);
        onChange();
      };
    });
  }

  function render() {
    const n = selected.size;
    const btn = container.querySelector('.ms-btn');
    const active = n > 0;
    btn.querySelector('.ms-label').textContent = n > 0 ? `${label} (${n})` : label;
    btn.className = `ms-btn h-8 min-w-[6.5rem] flex items-center justify-between gap-1 px-2.5 rounded-lg border text-xs transition ${active ? 'bg-zinc-800 text-white border-emerald-600' : 'bg-zinc-950 text-zinc-300 border-zinc-800 hover:bg-zinc-900'}`;
    btn.setAttribute('aria-expanded', String(open));
    container.querySelector('.ms-pop').classList.toggle('hidden', !open);
    renderOptions();
  }

  container.innerHTML = `
    <button type="button" class="ms-btn" aria-haspopup="listbox">
      <span class="ms-label">${esc(label)}</span>
      ${renderIcon('chevron-down', 'w-3 h-3')}
    </button>
    <div class="ms-pop absolute z-30 mt-1 w-56 max-h-64 overflow-y-auto rounded-lg border border-zinc-700 bg-zinc-900 shadow-2xl p-1 hidden" role="listbox"></div>
  `;
  container.querySelector('.ms-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    open = !open;
    render();
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


const modelMultiSelect = createMultiSelect(modelFilterEl, {
  label: 'Model',
  selected: filters.models,
  onChange: renderAll,
  getOptions: () => [...new Set(collectTasks(treeData).flatMap(t => t.acceptable_models || []))]
    .sort().map(v => ({ value: v, label: v })),
});
const specMultiSelect = createMultiSelect(specFilterEl, {
  label: 'Spec',
  selected: filters.specs,
  onChange: renderAll,
  getOptions: () => {
    const byId = new Map();
    collectTasks(treeData).forEach(t => byId.set(t._specId, t._specTitle));
    return [...byId.entries()].sort((a, b) => a[1].localeCompare(b[1])).map(([value, label]) => ({ value, label }));
  },
});
const scoreFilter = createScoreFilter(scoreFilterEl);


function populateFilterOptions() {
  const tasks = collectTasks(treeData);
  fillSelect(repoFilter, tasks.map(t => t.target_repo || NO_REPO), filters.repo, 'All repos');
  const scores = tasks.map(t => t.score).filter(s => typeof s === 'number');
  scoreBounds = scores.length ? { min: Math.min(...scores), max: Math.max(...scores) } : { min: 0, max: 100 };
}

function renderFilterControls() {
  repoFilter.value = filters.repo;
  modelMultiSelect.render();
  specMultiSelect.render();
  scoreFilter.render();
  clearFiltersBtn.classList.toggle('hidden', !(structuralFilterActive() || filters.q !== ''));
}

repoFilter.addEventListener('change', () => {
  filters.repo = repoFilter.value;
  renderAll();
});
clearFiltersBtn.addEventListener('click', () => {
  filters.statusMode.clear();
  filters.repo = '';
  filters.models.clear();
  filters.specs.clear();
  filters.scoreMin = null;
  filters.scoreMax = null;
  filters.q = '';
  searchBox.value = '';
  renderAll();
});


// Legend
function renderLegend() {
  legendBody.innerHTML = window.STATUS_GROUPS.map(group => {
    const rows = Object.values(window.STATUS_THEMES).filter(t => t.group === group.code).map(t => `
      <div class="flex items-start gap-2 py-1">
        <div class="w-36 flex-shrink-0">${statusChip(t.code)}</div>
        <p class="text-xs text-zinc-300">${esc(t.description)}</p>
      </div>
    `).join('');
    return `<div><div class="text-[10px] uppercase tracking-wider text-zinc-400 mt-2">${esc(group.label)}</div>${rows}</div>`;
  }).join('');
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


