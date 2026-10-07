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

// Sections default collapsed; expandedSections remembers, for this session only, which
// ones the user opened, so re-rendering after a filter change never re-collapses them.
// The "Sections (N)" group header is a level above that: it hides the whole row of
// <details> summaries at once, independent of expandedSections and of a node's own expand.
function renderSections(sections, ownerId) {
  const list = sections || [];
  if (list.length === 0) return '';
  const groupId = `${ownerId}::sections`;
  const isGroupCollapsed = groupCollapsed(groupId, true);
  const items = list.map(s => {
    const id = `${ownerId}::${s.key}`;
    allSectionIds.push(id);
    const isOpen = expandedSections.has(id);
    const label = (s.header || s.key).replace(/^#+\s*/, '');
    const body = stripRedundantLeadingHeading(s.content, label);
    return `
      <details ${isOpen ? 'open' : ''} data-section-id="${esc(id)}" class="rounded-lg border border-zinc-800 bg-zinc-950/60">
        <summary class="cursor-pointer select-none px-3 py-1.5 text-xs font-semibold text-zinc-300 flex items-center justify-between gap-2">
          <div class="flex items-center gap-2 min-w-0">
            <span>${esc(label)}</span>
            <span class="font-mono text-[10px] text-zinc-400 font-normal">${esc(s.key)}</span>
          </div>
          ${renderIcon('chevron-right', 'w-3 h-3 text-zinc-400 details-caret flex-shrink-0')}
        </summary>
        <div class="prose prose-invert max-w-none px-3 pb-3 text-xs leading-relaxed text-zinc-400">${renderSectionBody(body)}</div>
      </details>
    `;
  }).join('');
  return `
    <div class="space-y-2 pt-2">
      ${renderGroupHeader(groupId, 'Sections', list.length, true)}
      <div class="space-y-2 ${isGroupCollapsed ? 'hidden' : ''}">${items}</div>
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
// view keeps its own record of which ids it asked to expand -- the only thing that mutates
// them is toggleExpand() below.
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

function toggleExpand(row) {
  if (expandedIds.has(row.id)) {
    const closed = collapseWithDescendants(row.id);
    window.tmStore.close(closed);
    window.tmStore.unwatch(closed);
  } else {
    expandedIds.add(row.id);
    if (row.kind !== 'task') window.tmStore.open([row.id]);
    window.tmStore.watch([row.id]);
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
  return `<span class="font-mono text-[11px] leading-4 text-zinc-400 flex-shrink-0">${p.completed}/${p.total}</span>`;
}


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
    }
  }

  roots.forEach(n => createNodeRow(n));
}

searchBox.addEventListener('input', (e) => {
  filters.q = e.target.value.toLowerCase();
  applyFilterChange();
});

// Select Node Action (Coordinates Tree, Document, and Graph)

function selectNode(nodeId) {
  selectedNodeId = nodeId;
  renderTree();

  if (currentMode === window.VIEW_MODES.DOCUMENT) {
    const row = window.tmStore.rows.get(nodeId);
    if (row && !expandedIds.has(nodeId)) {
      toggleExpand(row);
      scheduleRender();
    }
    const targetEl = document.getElementById(`doc-node-${nodeId}`);
    if (targetEl) {
      targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
      targetEl.classList.add('node-highlighted');
      setTimeout(() => targetEl.classList.remove('node-highlighted'), 1600);
    }
  } else if (currentMode === window.VIEW_MODES.GRAPH) {
    if (networkInstance) {
      networkInstance.selectNodes([nodeId]);
      networkInstance.focus(nodeId, { scale: 1.1, animation: true });
    }
    showGraphInspector(nodeId);
  }
}


// Render Unified Document View: one card per root the store currently holds (a spec; a
// plan or task with no container -- the "Rows" section's own definition of a root), walked
// by parent/ordinal/id exactly like the sidebar tree above.
function renderUnifiedDocument() {
  unifiedDocument.innerHTML = '';
  allSectionIds = [];
  const byParent = visibleChildrenByParent();
  const roots = byParent.get(null) || [];

  if (roots.length === 0) {
    unifiedDocument.innerHTML = emptyPaneState();
    return;
  }

  const cardHtml = roots.map(root => {
    if (root.kind === 'spec') return renderSpecCard(root, byParent);
    if (root.kind === 'plan') return renderPlanCard(root, byParent);
    return renderTaskCard(root);
  }).join('');
  unifiedDocument.innerHTML = cardHtml;

  attachCollapsibleHandlers();
  attachSectionToggleHandlers(unifiedDocument);
  attachCopyHandlers(unifiedDocument);
  updateToggleSectionsButton();
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

// Below sm a row keeps its id and title and drops its pills and copy button.
function rowPills(html) {
  return `<span class="hidden sm:inline-flex items-center gap-1.5 flex-shrink-0">${html}</span>`;
}

function renderSpecCard(spec, byParent) {
  const specStatus = displayOf(spec);
  const isOpen = expandedIds.has(spec.id);
  const body = bodyOf(spec.id);
  const counts = countsForRow(spec);
  const children = byParent.get(spec.id) || [];

  const pills = priorityPill(spec.priority) + (spec.target_repo ? repoPill(spec.target_repo) : '');

  let bodyHtml = '';
  let childrenHtml = '';
  if (isOpen) {
    bodyHtml = body ? renderSections(sectionsListFrom(body), spec.id) : paneState('loading');
    if (children.length > 0) {
      const groupId = `${spec.id}::children`;
      const kinds = new Set(children.map(c => c.kind));
      const label = kinds.size > 1 ? 'Plans and tasks' : kinds.has('plan') ? 'Plans' : 'Tasks';
      const shared = sharedMeta(children);
      childrenHtml = `
        <div class="space-y-3">
          ${renderGroupHeader(groupId, label, children.length, false, sharedMetaHtml(shared))}
          <div class="space-y-4 ${groupCollapsed(groupId, false) ? 'hidden' : ''}">
            ${children.map(c => (c.kind === 'plan' ? renderPlanCard(c, byParent, shared) : renderTaskCard(c, shared))).join('')}
          </div>
        </div>
      `;
    }
  }

  return `
    <article id="doc-node-${esc(spec.id)}" class="space-y-6 transition duration-200">
      <div class="border-b border-zinc-800 pb-6 space-y-3">
        <div class="relative flex items-center gap-2 min-w-0">
          ${nodeToggle(spec, isOpen)}
          ${statusIcon(specStatus)}
          ${kindBadge(spec.kind)}
          ${idLink(spec.id, spec.kind)}
          <span class="flex-1"></span>
          ${leasePulse(leaseOf(spec))}
          ${rowPills(pills + copyIdButton(spec.id))}
        </div>
        <h1 class="text-2xl font-bold tracking-tight text-white">${esc(spec.title)}</h1>
        <div class="space-y-1.5">
          ${progressBar(counts, 'h-2.5')}
          <div class="text-xs text-zinc-400">${esc(progressText(counts))}</div>
        </div>
      </div>
      ${bodyHtml}
      ${childrenHtml}
    </article>
  `;
}


function renderPlanCard(plan, byParent, shared = SHOW_EVERY_META) {
  const planStatus = displayOf(plan);
  const isOpen = expandedIds.has(plan.id);
  const body = bodyOf(plan.id);
  const counts = countsForRow(plan);
  const p = progressParts(counts);
  const tasks = byParent.get(plan.id) || [];
  const tasksGroupId = `${plan.id}::tasks`;
  const tasksGroupCollapsed = groupCollapsed(tasksGroupId, true);
  const taskMeta = sharedMeta(tasks);

  let innerBody = '';
  if (isOpen) {
    const planSectionsHtml = body ? renderSections(sectionsListFrom(body), plan.id) : paneState('loading');
    innerBody = `
      ${planSectionsHtml}
      <div class="space-y-3">
        ${renderGroupHeader(tasksGroupId, 'Tasks', tasks.length, true, sharedMetaHtml(taskMeta))}
        <div class="space-y-2.5 ${tasksGroupCollapsed ? 'hidden' : ''}">
          ${tasks.map(task => renderTaskCard(task, taskMeta)).join('')}
        </div>
      </div>
    `;
  }

  return `
    <div id="doc-node-${esc(plan.id)}" class="border border-zinc-800 rounded-xl bg-zinc-900/30 transition">
      <div class="plan-line relative h-12 px-4 rounded-t-xl bg-zinc-900/95 backdrop-blur-sm border-b border-zinc-800 flex items-center gap-2.5 min-w-0">
        ${nodeToggle(plan, isOpen)}
        ${statusIcon(planStatus)}
        ${kindBadge(plan.kind)}
        ${idLink(plan.id, plan.kind)}
        <span class="flex-1 min-w-0 truncate text-sm font-semibold text-zinc-200" title="${esc(plan.title)}">${esc(plan.title)}</span>
        ${p.total > 0 ? `<div class="hidden sm:block w-40 flex-shrink min-w-0">${progressBar(counts, 'h-2')}</div>${progressCount(p)}` : ''}
        ${leasePulse(leaseOf(plan))}
        ${rowPills((shared.priority === null ? priorityPill(plan.priority) : '') + copyIdButton(plan.id))}
      </div>

      <div class="plan-body ${isOpen ? '' : 'hidden'} p-4 space-y-4">
        ${innerBody}
      </div>
    </div>
  `;
}

// Verification kind -> icon, so the row reads at a glance instead of naming the enum value.
// Each one is its own icon rather than borrowing a status or toolbar icon's meaning
// (file-text/network/play already mean Document view, Graph view and Implementing).

const VERIFICATION_ICON = {
  file_exists: 'file-check', file_absent: 'file-x', symbol_signature: 'code',
  ast_export: 'package', test_command: 'terminal', codegraph_query: 'database'
};
const VERIFICATION_LABEL = {
  file_exists: 'File exists', file_absent: 'File absent', symbol_signature: 'Symbol signature',
  ast_export: 'AST export', test_command: 'Test command', codegraph_query: 'Codegraph query'
};

// A table of nodes (status, id, title) behind a collapsible group header -- the shared
// shape for the task card's Blockers, Dependencies and Dependents sections.
function renderRelationTable(ownerId, key, label, rows, defaultCollapsed) {
  if (!rows || rows.length === 0) return '';
  const groupId = `${ownerId}::${key}`;
  const isCollapsed = groupCollapsed(groupId, defaultCollapsed);
  const body = rows.map(d => `
    <div class="flex items-center gap-2 px-2 py-1.5 bg-zinc-950/60">
      ${d.status
        ? (d.kind === 'decision' && typeof decisionStatusIcon === 'function' ? decisionStatusIcon(d.status) : statusIcon(d.status))
        : '<span class="text-[10px] font-mono text-red-400">missing</span>'}
      ${idLink(d.id, d.kind)}
      <span class="truncate text-[11px] text-zinc-400">${esc(d.title || '')}</span>
    </div>
  `).join('');
  return `
    <div class="space-y-1.5 pt-2">
      ${renderGroupHeader(groupId, label, rows.length, defaultCollapsed)}
      <div class="divide-y divide-zinc-800 rounded-lg border border-zinc-800 ${isCollapsed ? 'hidden' : ''}">${body}</div>
    </div>
  `;
}

function renderTaskCard(task, shared = SHOW_EVERY_META) {
  const taskStatus = displayOf(task);
  const isOpen = expandedIds.has(task.id);
  const body = bodyOf(task.id);
  const lease = leaseOf(task);

  let pills = '';
  if (shared.models === null) pills += (task.acceptable_models || []).map(modelPill).join('');
  if (shared.priority === null) pills += priorityPill(task.priority);

  let innerBody = '';
  if (isOpen) {
    if (!body) {
      innerBody = paneState('loading');
    } else {
      // Verifications: icon names the kind (hover for the word), row stays justified
      // (icon pinned left, target pinned right) but the target text itself reads left-aligned;
      // a real gap keeps the two from ever touching regardless of either one's length.
      let verificationsHtml = '';
      if (body.verifications && body.verifications.length > 0) {
        const groupId = `${task.id}::verifications`;
        const isGroupCollapsed = groupCollapsed(groupId, true);
        const rows = body.verifications.map(v => `
          <div class="p-2 bg-zinc-950/60 flex items-center gap-3 justify-between">
            <span class="text-emerald-400 flex-shrink-0" title="${esc(VERIFICATION_LABEL[v.verification_type] || v.verification_type)}">${renderIcon(VERIFICATION_ICON[v.verification_type] || 'check', 'w-3.5 h-3.5')}</span>
            <span class="text-zinc-300 truncate text-left flex-1 min-w-0">${esc(v.target_path)}</span>
          </div>
        `).join('');
        verificationsHtml = `
          <div class="space-y-1.5 pt-2 border-t border-zinc-800/60 mb-3">
            ${renderGroupHeader(groupId, 'Verifications', body.verifications.length, true)}
            <div class="rounded-lg border border-zinc-800 overflow-hidden divide-y divide-zinc-800 text-[11px] font-mono ${isGroupCollapsed ? 'hidden' : ''}">${rows}</div>
          </div>
        `;
      }

      const sectionsHtml = renderSections(sectionsListFrom(body), task.id);
      const unfinishedDeps = (body.dependency_details || []).filter(d => !d.finished);
      const blockersHtml = renderRelationTable(task.id, 'blockers', 'Blockers', unfinishedDeps, true);
      const dependenciesHtml = renderRelationTable(task.id, 'deps', 'Dependencies', body.dependency_details, true);
      const dependentsHtml = renderRelationTable(task.id, 'dependents', 'Dependents', body.dependent_details, true);
      const leaseFact = lease
        ? `<div class="flex items-center gap-1.5 text-[11px] leading-4"><span class="text-zinc-400">Lease</span>${leaseBadge(lease)}</div>`
        : '';

      innerBody = `
        ${leaseFact}
        ${verificationsHtml}
        ${blockersHtml}
        ${dependenciesHtml}
        ${dependentsHtml}
        ${sectionsHtml}
      `;
    }
  }

  return `
    <div id="doc-node-${esc(task.id)}" class="border border-zinc-800/80 rounded-lg bg-zinc-950/40 hover:border-zinc-700 transition">
      <div class="task-line relative h-10 px-3 rounded-lg flex items-center gap-2 min-w-0 bg-zinc-900/90 backdrop-blur-sm hover:bg-zinc-900">
        ${nodeToggle(task, isOpen)}
        ${statusIcon(taskStatus)}
        ${idLink(task.id, task.kind)}
        <span class="flex-1 min-w-0 truncate text-[13px] text-zinc-200" title="${esc(task.title)}">${esc(task.title)}</span>
        ${leasePulse(lease)}
        ${rowPills(pills + copyIdButton(task.id))}
      </div>

      <div class="task-body ${isOpen ? '' : 'hidden'} p-3.5 bg-zinc-950/80 border-t border-zinc-800/60 space-y-2">
        ${innerBody}
      </div>
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
