function stripRedundantLeadingHeading(content, label) {
  const text = content || '';
  const lines = text.split('\n');
  const headingMatch = lines[0] && lines[0].match(/^#{1,6}\s*(.*)$/);
  if (headingMatch && headingMatch[1].trim().toLowerCase() === label.trim().toLowerCase()) {
    let rest = lines.slice(1);
    while (rest.length && rest[0].trim() === '') rest.shift();
    return rest.join('\n');
  }
  return text;
}

function renderSectionBody(content) {
  const text = content || '';
  if (typeof marked === 'undefined') {
    return `<pre class="whitespace-pre-wrap font-sans">${esc(text)}</pre>`;
  }
  return marked.parse(text, { breaks: true });
}

// A group id's collapsed state relative to its own default. collapsedGroups holds only
// ids that were explicitly toggled away from default, so one set serves group types with
// opposite defaults without reseeding it on every render.
function groupCollapsed(groupId, defaultCollapsed) {
  return collapsedGroups.has(groupId) ? !defaultCollapsed : defaultCollapsed;
}

// `meta` is a value every row of the group shares, drawn once at the header's right.
function renderGroupHeader(groupId, label, count, defaultCollapsed, meta = '') {
  const header = disclosureHeader(label, count, !groupCollapsed(groupId, defaultCollapsed), groupId);
  if (!meta) return header;
  return `<div class="flex items-center gap-2">${header}<div class="flex items-center gap-1.5 flex-shrink-0">${meta}</div></div>`;
}

// Section rows start collapsed; expandedSections remembers, for this session only, which ones
// the user opened, so re-rendering after a filter change never re-collapses them. A row reads
// its key, and a leading heading in its content that repeats its header is dropped.
function sectionItemsHtml(list, ownerId, editable = false) {
  const items = list.map(s => {
    const id = `${ownerId}::${s.key}`;
    const isOpen = expandedSections.has(id);
    const label = (s.header || s.key).replace(/^#+\s*/, '');
    const body = stripRedundantLeadingHeading(s.content, label);
    const controls = editable ? `
      <button type="button" class="p-1 flex-shrink-0 rounded text-zinc-400 hover:text-white hover:bg-zinc-800 ${ROW_CONTROL} ${FOCUS_RING}" data-act="edit-section" data-key="${esc(s.key)}" aria-label="Edit section ${esc(s.key)}">${renderIcon('code', 'w-3 h-3')}</button>
      <button type="button" class="p-1 flex-shrink-0 rounded text-zinc-400 hover:text-red-400 hover:bg-zinc-800 ${ROW_CONTROL} ${FOCUS_RING}" data-act="delete-section" data-key="${esc(s.key)}" aria-label="Delete section ${esc(s.key)}">${renderIcon('x', 'w-3 h-3')}</button>` : '';
    return `
      <details ${isOpen ? 'open' : ''} data-section-id="${esc(id)}" class="rounded-lg border border-zinc-800 bg-zinc-950/60">
        <summary class="group cursor-pointer select-none px-3 py-1.5 flex items-center gap-1.5 min-w-0 rounded-lg ${FOCUS_RING}">
          ${renderIcon('chevron-right', 'w-3 h-3 flex-shrink-0 text-zinc-400 details-caret')}
          <span class="flex-1 min-w-0 truncate font-mono text-[11px] leading-4 text-zinc-300">${esc(s.key)}</span>
          ${controls}
        </summary>
        <div class="pl-[30px] pr-3 pb-3 text-xs leading-5 text-zinc-300">${renderSectionBody(body)}</div>
      </details>
    `;
  }).join('');
  return `<div class="space-y-1.5">${items}</div>`;
}

// The decisions view's own section list: the group starts collapsed there.
function renderSections(sections, ownerId) {
  const list = sections || [];
  if (list.length === 0) return '';
  const groupId = `${ownerId}::sections`;
  return `
    <div class="space-y-2 pt-2">
      ${renderGroupHeader(groupId, 'Sections', list.length, true)}
      <div class="${groupCollapsed(groupId, true) ? 'hidden' : ''}">${sectionItemsHtml(list, ownerId)}</div>
    </div>
  `;
}

function attachSectionToggleHandlers(root) {
  root.querySelectorAll('details[data-section-id]').forEach(details => {
    details.addEventListener('toggle', () => {
      const id = details.getAttribute('data-section-id');
      if (details.open) expandedSections.add(id);
      else expandedSections.delete(id);
      updateToggleSectionsButton();
    });
  });
}


// A body's sections arrive as a {key: {header, content, ordinal}} map (store.js's own
// normalizeBody()); renderSections() wants the list shape web/bodies.py sends, so this
// converts back, ordered the way the server ordered the section rows in the first place.
function sectionsListFrom(body) {
  return Object.entries((body && body.sections) || {})
    .map(([key, s]) => ({ key, header: s.header, content: s.content, ordinal: s.ordinal }))
    .sort((a, b) => a.ordinal - b.ordinal);
}

function bodyOf(id) {
  return window.tmStore.bodies.get(id) || null;
}

// Groups the store's current (already-visible) rows by parent, sorted the way the plan's
// parent context spells out for a visible set: ordinal, then id.
function visibleChildrenByParent() {
  const byParent = new Map();
  window.tmStore.rows.forEach(row => {
    if (!byParent.has(row.parent)) byParent.set(row.parent, []);
    byParent.get(row.parent).push(row);
  });
  byParent.forEach(list => list.sort((a, b) => a.ordinal - b.ordinal || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)));
  return byParent;
}

// A row's lease names the action and agent only, since a heartbeat pushes nothing; a watched
// body's lease adds the heartbeat the pulse reads its age from.
function leaseOf(row) {
  const body = bodyOf(row.id);
  return (body && body.lease) || row.lease || null;
}

// A node is expanded exactly when the store's open set carries it (its children, if any,
// are then in window.tmStore.rows) and its watch set carries it (its body, once it arrives,
// is then in window.tmStore.bodies). The store exposes no getter for either set, so this
// view keeps its own record of which ids it asked to expand -- only expandId() and
// toggleExpand() below mutate it.
function collapseWithDescendants(id) {
  const toClose = new Set([id]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const eid of expandedIds) {
      if (toClose.has(eid)) continue;
      let p = window.tmStore.rows.has(eid) ? window.tmStore.rows.get(eid).parent : null;
      while (p !== null && p !== undefined) {
        if (toClose.has(p)) { toClose.add(eid); changed = true; break; }
        p = window.tmStore.rows.has(p) ? window.tmStore.rows.get(p).parent : null;
      }
    }
  }
  toClose.forEach(x => expandedIds.delete(x));
  return [...toClose];
}

// A node whose body has not arrived LOADING_DELAY_MS after it was expanded shows its loading
// pane state; one that arrives sooner never does.
const slowReads = new Set();

function expandId(node) {
  if (expandedIds.has(node.id)) return;
  expandedIds.add(node.id);
  if (node.kind !== 'task') window.tmStore.open([node.id]);
  window.tmStore.watch([node.id]);
  slowReads.delete(node.id);
  setTimeout(() => {
    if (!expandedIds.has(node.id) || detailBody(node.id)) return;
    slowReads.add(node.id);
    scheduleRender();
  }, LOADING_DELAY_MS);
}

function toggleExpand(row) {
  if (expandedIds.has(row.id)) {
    const closed = collapseWithDescendants(row.id);
    window.tmStore.close(closed);
    window.tmStore.unwatch(closed);
  } else {
    expandId(row);
  }
}

// Before any row: loading until the store first answers, an error once the socket closed or
// the subscribe was refused without an answer, else the fact that nothing matches.
function retryConnection() {
  window.tmStore.resync();
}

function emptyPaneState() {
  if (!isStaticMode && socketLost) return paneState('error', 'Could not reach the server.', retryConnection);
  if (!storeAnswered && storeError) return paneState('error', storeError, retryConnection);
  if (!storeAnswered) return paneState('loading');
  return paneState('empty', 'No specs, plans or tasks match.');
}

function progressCount(p) {
  return `<span class="font-mono text-[11px] leading-4 text-zinc-400 flex-shrink-0" role="img" aria-label="${p.completed} of ${p.total} done">${p.completed}/${p.total}</span>`;
}

// A plan's or spec's own review, fix and landing, once started, is the last row of its own list
// and one of the counts tree's units; null while it has not started.
function ownStepTitle(row) {
  if (!row || row.kind === 'task' || !countsAsWork(row)) return null;
  return row.kind === 'spec' ? 'Spec review and landing' : 'Plan review and landing';
}

function ownStepCells(row, titleHtml) {
  return `${statusIcon(displayOf(row))}${idLink(row.id, row.kind)}${titleHtml}${leasePulse(leaseOf(row))}`;
}

function ownStepCardHtml(row) {
  const title = ownStepTitle(row);
  return `<div class="own-step h-10 px-3 rounded-lg border border-zinc-800/80 bg-zinc-900/60 hover:bg-zinc-900 flex items-center gap-2 min-w-0 cursor-pointer" data-step-of="${esc(row.id)}"><span class="w-3.5 flex-shrink-0"></span>${ownStepCells(row, `<span class="flex-1 min-w-0 truncate text-[13px] text-zinc-200" title="${esc(title)}">${esc(title)}</span>`)}</div>`;
}

// The id link and the tooltips keep their own clicks; anywhere else on the row opens its container.
document.addEventListener('click', (e) => {
  const step = e.target.closest && e.target.closest('[data-step-of]');
  if (step && !e.target.closest('a, [data-tip]')) openNode(step.getAttribute('data-step-of'));
});


// Sidebar Tree Rendering
function renderTree() {
  treeList.innerHTML = '';
  const byParent = visibleChildrenByParent();
  const roots = byParent.get(null) || [];
  if (roots.length === 0) {
    treeList.innerHTML = emptyPaneState();
    return;
  }

  function createNodeRow(node, depth = 0) {
    const effectiveStatus = displayOf(node);
    const hasChildren = node.child_count > 0;
    const isOpen = expandedIds.has(node.id);
    const isSelected = selectedNodeId === node.id;

    const row = document.createElement('div');
    // tabindex/role/keydown: the tree is the only detail panel a keyboard user can reach
    // through (vis-network's canvas nodes have no DOM presence to focus), so this row is
    // reachable and operable by keyboard, not only by row.onclick below.
    row.className = `flex items-center gap-1.5 h-7 px-2 rounded-lg border cursor-pointer text-xs transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400 ${isSelected ? 'bg-zinc-800 border-zinc-700 text-white' : 'border-transparent text-zinc-200 hover:bg-zinc-800/60'}`;
    row.style.paddingLeft = `${depth * 14 + 8}px`;
    row.setAttribute('role', 'treeitem');
    row.setAttribute('tabindex', '0');
    row.setAttribute('aria-selected', String(isSelected));
    row.setAttribute('aria-label', `${node.id}: ${node.title}`);
    row.setAttribute('data-node-id', node.id);
    if (hasChildren) row.setAttribute('aria-expanded', String(isOpen));

    const chevron = hasChildren
      ? `<button type="button" class="toggle-btn flex-shrink-0 rounded-sm text-zinc-400 hover:text-white ${FOCUS_RING}" aria-expanded="${isOpen}" aria-label="${esc(node.id)}">${renderIcon(isOpen ? 'chevron-down' : 'chevron-right', 'w-3.5 h-3.5')}</button>`
      : '<span class="w-3.5 h-3.5 flex-shrink-0"></span>';

    let progressHtml = '';
    if (node.kind !== 'task') {
      const p = progressParts(countsForRow(node));
      if (p.total > 0) progressHtml = progressCount(p);
    }

    row.innerHTML = `
      ${chevron}
      ${statusIcon(effectiveStatus)}
      ${idLink(node.id, node.kind)}
      <span class="flex-1 min-w-0 truncate" title="${esc(node.title)}">${esc(node.title)}</span>
      ${progressHtml}
      ${leasePulse(leaseOf(node))}
    `;

    const toggleBtn = row.querySelector('.toggle-btn');
    if (toggleBtn) {
      toggleBtn.onclick = (e) => {
        e.stopPropagation();
        toggleExpand(node);
        scheduleRender();
      };
    }

    row.onclick = (e) => {
      if (e.target.closest('a, button')) return;
      selectNode(node.id);
    };
    row.addEventListener('keydown', (e) => {
      if (e.target !== row) return;  // let the nested controls handle their own Enter/Space
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        selectNode(node.id);
      }
    });
    treeList.appendChild(row);

    if (hasChildren && isOpen) {
      (byParent.get(node.id) || []).forEach(c => createNodeRow(c, depth + 1));
      if (ownStepTitle(node)) createStepRow(node, depth + 1);
    }
  }

  function createStepRow(node, depth) {
    const title = ownStepTitle(node);
    const row = document.createElement('div');
    row.className = `flex items-center gap-1.5 h-7 px-2 rounded-lg border border-transparent cursor-pointer text-xs text-zinc-200 hover:bg-zinc-800/60 transition ${FOCUS_RING}`;
    row.style.paddingLeft = `${depth * 14 + 8}px`;
    row.setAttribute('role', 'treeitem');
    row.setAttribute('tabindex', '0');
    row.setAttribute('aria-label', `${node.id}: ${title}`);
    row.setAttribute('data-step-of', node.id);
    row.innerHTML = `<span class="w-3.5 h-3.5 flex-shrink-0"></span>${ownStepCells(node, `<span class="flex-1 min-w-0 truncate" title="${esc(title)}">${esc(title)}</span>`)}`;
    row.addEventListener('keydown', (e) => {
      if (e.target !== row || (e.key !== 'Enter' && e.key !== ' ')) return;
      e.preventDefault();
      selectNode(node.id);
    });
    treeList.appendChild(row);
  }

  roots.forEach(n => createNodeRow(n));
  finishReveal(false);
}

searchBox.addEventListener('input', (e) => {
  filters.q = e.target.value.toLowerCase();
  applyFilterChange();
});

// Selection lives in the location: a tree row, an id link or a graph node opens /<view>/<id>,
// and applyNodeLocation brings the screen to it.
function selectNode(nodeId) {
  openNode(nodeId);
}

// The node the location names is drawn selected by the tree row, the Document card and the
// Graph node. setSelectedNode is selectedNodeId's one writer.
let pendingReveal = null;
// What Document shows above its cards for a location's node it cannot show: { kind, message }
// for paneState, an id nothing has or a lookup that failed.
let revealState = null;
let appliedNodeLocation = null;

function setSelectedNode(id) {
  if (selectedNodeId === id) return;
  const prev = selectedNodeId;
  selectedNodeId = id;
  pendingReveal = null;
  revealState = null;
  if (visNodesDS) syncGraphNodes([prev, id].filter(Boolean));
  if (!id && networkInstance) networkInstance.unselectAll();
  scheduleRender();
}

// Document reveals the node; Graph opens the drawer on it and reveals it in the tree; Waves
// opens the drawer. A location already applied changes nothing, so a filter change keeps it.
function applyNodeLocation(view, id) {
  if (view === window.VIEW_MODES.DECISIONS) {
    appliedNodeLocation = null;
    return;
  }
  const key = `${view}/${id || ''}`;
  if (key === appliedNodeLocation) return;
  appliedNodeLocation = key;
  setSelectedNode(id);
  if (!id) {
    closeDetailDrawer();
    return;
  }
  if (view === window.VIEW_MODES.DOCUMENT) {
    if (inspectorNodeId !== id) closeDetailDrawer();
  } else {
    openDrawer(id);
  }
  if (view === window.VIEW_MODES.WAVES) return;
  if (view === window.VIEW_MODES.GRAPH && networkInstance && window.tmStore.rows.has(id)) {
    networkInstance.selectNodes([id]);
    networkInstance.focus(id, { scale: 1.1, animation: true });
  }
  revealNode(id);
}

// A row the store holds, or, in a static export, any row the export carries.
function heldRow(id) {
  return window.tmStore.rows.get(id) || (isStaticMode ? (window.STATIC_DATA.rows || {})[id] : undefined);
}

// The node and its containers, outermost first, from held rows alone: undefined once a link is
// not held (live: ask the server), null when a static export has no such node.
function heldChain(id) {
  const chain = [];
  for (let at = id; at;) {
    const row = heldRow(at);
    if (!row) return isStaticMode ? null : undefined;
    chain.unshift(row);
    at = row.parent;
  }
  return chain;
}

// The same chain, reading each link the store does not hold (a deep link, a node under a
// collapsed container) from /api/nodes?ids=; null for an id nothing has.
async function fetchChain(id) {
  const chain = [];
  for (let at = id; at;) {
    let row = heldRow(at);
    if (!row) {
      const page = await api('GET', `/api/nodes?ids=${encodeURIComponent(at)}`);
      row = (page.items || [])[0];
    }
    if (!row) return null;
    chain.unshift(row);
    at = row.parent;
  }
  return chain;
}

// Opens every container above the node (its card, and its children group) and, in Document, the
// node itself; finishReveal scrolls to it once its card or tree row is drawn.
function showReveal(id, chain) {
  if (!chain) {
    revealState = { kind: 'empty', message: `${id} not found` };
    scheduleRender();
    return;
  }
  chain.slice(0, -1).forEach((ancestor) => {
    expandId(ancestor);
    // A children group starts collapsed, so its id in collapsedGroups means open.
    collapsedGroups.add(`${ancestor.id}::children`);
  });
  if (currentMode === window.VIEW_MODES.DOCUMENT) expandId(chain[chain.length - 1]);
  pendingReveal = id;
  scheduleRender();
}

function revealNode(id) {
  revealState = null;
  const held = heldChain(id);
  if (held !== undefined) {
    showReveal(id, held);
    return;
  }
  fetchChain(id).then((chain) => {
    if (selectedNodeId === id) showReveal(id, chain);
  }, (err) => {
    if (selectedNodeId !== id) return;
    revealState = { kind: 'error', message: err.message };
    scheduleRender();
  });
}

function retryReveal() {
  if (selectedNodeId) revealNode(selectedNodeId);
}

// `inDocument`: the pane just drawn, Document or the tree; only the current view's own pane
// finishes a reveal.
function finishReveal(inDocument) {
  if (!pendingReveal || inDocument !== (currentMode === window.VIEW_MODES.DOCUMENT)) return;
  const el = inDocument
    ? document.getElementById(`doc-node-${pendingReveal}`)
    : treeList.querySelector(`[data-node-id="${CSS.escape(pendingReveal)}"]`);
  if (!el) return;
  pendingReveal = null;
  el.scrollIntoView({ block: 'center' });
  if (inDocument) el.focus({ preventScroll: true });
}


// Render Unified Document View: one card per root the store currently holds (a spec; a
// plan or task with no container -- the "Rows" section's own definition of a root), walked
// by parent/ordinal/id exactly like the sidebar tree above.
function renderUnifiedDocument() {
  const byParent = visibleChildrenByParent();
  const roots = byParent.get(null) || [];
  const lookup = revealState
    ? paneState(revealState.kind, revealState.message, revealState.kind === 'error' ? retryReveal : null)
    : '';
  const cards = roots.map(root => {
    if (root.kind === 'spec') return renderSpecCard(root, byParent);
    if (root.kind === 'plan') return renderPlanCard(root, byParent);
    return renderTaskCard(root);
  }).join('');
  // A revealed card keeps its focus when a later store change draws the document again.
  const focusedCard = unifiedDocument.contains(document.activeElement) && document.activeElement.hasAttribute('data-detail-root')
    ? document.activeElement.id : '';
  unifiedDocument.innerHTML = lookup + (roots.length ? cards : emptyPaneState());
  const refocus = focusedCard && document.getElementById(focusedCard);
  if (refocus) refocus.focus({ preventScroll: true });
  allSectionIds = [...unifiedDocument.querySelectorAll('details[data-section-id]')].map(d => d.getAttribute('data-section-id'));

  attachCollapsibleHandlers();
  attachSectionToggleHandlers(unifiedDocument);
  updateToggleSectionsButton();
  finishReveal(true);
}

// The header line's toggle: the chevron is the button, and its overlay makes the whole line
// a click target; the line's own controls sit above the overlay.
function nodeToggle(row, isOpen) {
  return `<button type="button" class="node-toggle flex-shrink-0 rounded-sm text-zinc-400 hover:text-white after:absolute after:inset-0 after:content-[''] ${FOCUS_RING}" data-node-id="${esc(row.id)}" aria-expanded="${isOpen}" aria-label="${esc(row.id)}">${renderIcon(isOpen ? 'chevron-down' : 'chevron-right', 'w-3.5 h-3.5')}</button>`;
}

// A value every row of a list shares moves to the list's header, once.
function sharedMeta(rows) {
  const tasks = rows.filter(r => r.kind === 'task');
  const models = new Set(tasks.map(r => (r.acceptable_models || []).join('\n')));
  const priorities = new Set(rows.map(r => r.priority || 50));
  return {
    models: tasks.length > 0 && models.size === 1 ? (tasks[0].acceptable_models || []) : null,
    priority: rows.length > 0 && priorities.size === 1 ? [...priorities][0] : null,
  };
}

function sharedMetaHtml(shared) {
  return (shared.models || []).map(modelPill).join('') + (shared.priority !== null ? priorityPill(shared.priority) : '');
}

const SHOW_EVERY_META = { models: null, priority: null };

// Below sm a row keeps its id and title and drops its pills.
function rowPills(html) {
  return html ? `<span class="hidden sm:inline-flex items-center gap-1.5 flex-shrink-0">${html}</span>` : '';
}

function selectedAttr(id) {
  return selectedNodeId === id ? ' aria-selected="true"' : '';
}

function cardBorder(id, normal) {
  return selectedNodeId === id ? 'border-emerald-400 ring-1 ring-emerald-400' : normal;
}

function cardActionsHtml(row) {
  return actionsMenuHtml(detailNode(row.id), leaseOf(row));
}

// An expanded card's details once its body has arrived. Below sm the Actions menu leads them,
// so the header line keeps its room for the id and title.
function cardBodyHtml(row, byParent, actionsBelowSm = true) {
  const body = detailBody(row.id);
  if (!body) return slowReads.has(row.id) ? paneState('loading') : '';
  const actions = actionsBelowSm ? `<div class="sm:hidden">${cardActionsHtml(row)}</div>` : '';
  return actions + nodeDetailHtml(body.node || row, body, row, { surface: 'card', byParent });
}

function renderSpecCard(spec, byParent) {
  const isOpen = expandedIds.has(spec.id);
  const counts = countsForRow(spec);
  const pills = priorityPill(spec.priority) + (spec.target_repo && spec.target_repo !== '.' ? repoPill(spec.target_repo) : '');

  return `
    <article id="doc-node-${esc(spec.id)}" data-detail-root="${esc(spec.id)}" tabindex="-1"${selectedAttr(spec.id)} class="space-y-6 rounded-xl border transition focus:outline-none ${cardBorder(spec.id, 'border-transparent')}">
      <div class="border-b border-zinc-800 pb-6 space-y-3">
        <div class="relative flex flex-wrap items-center gap-2 min-w-0">
          ${nodeToggle(spec, isOpen)}
          ${statusIcon(displayOf(spec))}
          ${kindBadge(spec.kind)}
          ${idLink(spec.id, spec.kind)}
          <span class="flex-1"></span>
          ${leasePulse(leaseOf(spec))}
          <span class="inline-flex items-center gap-1.5 flex-shrink-0">${pills}</span>
          ${isOpen ? cardActionsHtml(spec) : ''}
        </div>
        <h1 class="text-2xl font-bold tracking-tight text-white">${esc(spec.title)}</h1>
        <div class="space-y-1.5">
          ${progressBar(counts, 'h-2.5')}
          <div class="text-xs text-zinc-400">${esc(progressText(counts))}</div>
        </div>
      </div>
      ${isOpen ? cardBodyHtml(spec, byParent, false) : ''}
    </article>
  `;
}


function renderPlanCard(plan, byParent, shared = SHOW_EVERY_META) {
  const isOpen = expandedIds.has(plan.id);
  const counts = countsForRow(plan);
  const p = progressParts(counts);

  return `
    <div id="doc-node-${esc(plan.id)}" data-detail-root="${esc(plan.id)}" tabindex="-1"${selectedAttr(plan.id)} class="border rounded-xl bg-zinc-900/30 transition focus:outline-none ${cardBorder(plan.id, 'border-zinc-800')}">
      <div class="plan-line relative h-12 px-4 rounded-t-xl bg-zinc-900/95 ${isOpen ? 'border-b border-zinc-800' : 'rounded-b-xl'} flex items-center gap-2.5 min-w-0">
        ${nodeToggle(plan, isOpen)}
        ${statusIcon(displayOf(plan))}
        ${kindBadge(plan.kind)}
        ${idLink(plan.id, plan.kind)}
        <span class="flex-1 min-w-0 truncate text-sm font-semibold text-zinc-200" title="${esc(plan.title)}">${esc(plan.title)}</span>
        ${p.total > 0 ? `<div class="hidden sm:block w-40 flex-shrink min-w-0">${progressBar(counts, 'h-2')}</div>${progressCount(p)}` : ''}
        ${leasePulse(leaseOf(plan))}
        ${rowPills(shared.priority === null ? priorityPill(plan.priority) : '')}
        ${isOpen ? `<span class="hidden sm:inline-flex">${cardActionsHtml(plan)}</span>` : ''}
      </div>
      ${isOpen ? `<div class="plan-body p-4 space-y-3">${cardBodyHtml(plan, byParent)}</div>` : ''}
    </div>
  `;
}

function renderTaskCard(task, shared = SHOW_EVERY_META) {
  const isOpen = expandedIds.has(task.id);
  let pills = '';
  if (shared.models === null) pills += (task.acceptable_models || []).map(modelPill).join('');
  if (shared.priority === null) pills += priorityPill(task.priority);

  return `
    <div id="doc-node-${esc(task.id)}" data-detail-root="${esc(task.id)}" tabindex="-1"${selectedAttr(task.id)} class="border rounded-lg bg-zinc-950/40 transition focus:outline-none ${cardBorder(task.id, 'border-zinc-800/80 hover:border-zinc-700')}">
      <div class="task-line relative h-10 px-3 rounded-lg flex items-center gap-2 min-w-0 bg-zinc-900/90 hover:bg-zinc-900">
        ${nodeToggle(task, isOpen)}
        ${statusIcon(displayOf(task))}
        ${idLink(task.id, task.kind)}
        <span class="flex-1 min-w-0 truncate text-[13px] text-zinc-200" title="${esc(task.title)}">${esc(task.title)}</span>
        ${leasePulse(leaseOf(task))}
        ${rowPills(pills)}
        ${isOpen ? `<span class="hidden sm:inline-flex">${cardActionsHtml(task)}</span>` : ''}
      </div>
      ${isOpen ? `<div class="task-body p-3.5 rounded-b-lg bg-zinc-950/80 border-t border-zinc-800/60 space-y-3">${cardBodyHtml(task)}</div>` : ''}
    </div>
  `;
}

// Shared by the document view (whole-page re-render on toggle) and any other view that embeds
// renderSections()'s output -- the decisions view's detail pane, which has its own re-render
// function rather than renderUnifiedDocument. A group header is a real <button>, so Enter and
// Space reach this same click.
function attachGroupHeaderHandlers(root, rerender) {
  root.querySelectorAll('.disclosure[data-group-id]').forEach(header => {
    header.onclick = () => {
      const id = header.getAttribute('data-group-id');
      if (collapsedGroups.has(id)) collapsedGroups.delete(id);
      else collapsedGroups.add(id);
      rerender();
    };
  });
}

function attachCollapsibleHandlers() {
  unifiedDocument.querySelectorAll('.node-toggle').forEach(toggle => {
    toggle.onclick = () => {
      const row = window.tmStore.rows.get(toggle.getAttribute('data-node-id'));
      if (row) {
        toggleExpand(row);
        scheduleRender();
      }
    };
  });

  attachGroupHeaderHandlers(unifiedDocument, renderUnifiedDocument);
}
