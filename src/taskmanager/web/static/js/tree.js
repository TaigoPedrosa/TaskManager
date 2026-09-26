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
// opposite defaults (Tasks starts expanded, Sections starts collapsed) without reseeding
// it on every render.
function groupCollapsed(groupId, defaultCollapsed) {
  return collapsedGroups.has(groupId) ? !defaultCollapsed : defaultCollapsed;
}

function renderGroupHeader(groupId, label, count, defaultCollapsed, icon = null) {
  const isCollapsed = groupCollapsed(groupId, defaultCollapsed);
  const leadIcon = icon ? renderIcon(icon, 'w-3.5 h-3.5') : '';
  // role/tabindex/aria-expanded live on this row, not a nested control: the chevron below is
  // a <span> rather than a second <button>, so a screen reader sees one operable toggle, not
  // two nested interactive elements fighting over the same click.
  return `
    <div class="group-header flex items-center justify-between gap-2 cursor-pointer select-none focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-emerald-500 rounded" data-group-id="${groupId}" role="button" tabindex="0" aria-expanded="${!isCollapsed}" aria-label="${esc(label)} (${count})">
      <div class="flex items-center gap-2 min-w-0">
        ${leadIcon}
        <span class="text-[11px] font-semibold uppercase tracking-wider text-zinc-400">${esc(label)} (${count})</span>
      </div>
      <span class="text-zinc-500 flex-shrink-0">${renderIcon(isCollapsed ? 'chevron-right' : 'chevron-down', 'w-3 h-3')}</span>
    </div>
  `;
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
      <details ${isOpen ? 'open' : ''} data-section-id="${id}" class="rounded-lg border border-zinc-800 bg-zinc-950/60">
        <summary class="cursor-pointer select-none px-3 py-1.5 text-xs font-semibold text-zinc-300 flex items-center justify-between gap-2">
          <div class="flex items-center gap-2 min-w-0">
            <span>${esc(label)}</span>
            <span class="font-mono text-[10px] text-zinc-400 font-normal">${esc(s.key)}</span>
          </div>
          ${renderIcon('chevron-right', 'w-3 h-3 text-zinc-500 details-caret flex-shrink-0')}
        </summary>
        <div class="prose prose-invert max-w-none px-3 pb-3 text-xs leading-relaxed text-zinc-400">${renderSectionBody(body)}</div>
      </details>
    `;
  }).join('');
  return `
    <div class="space-y-2 pt-2">
      ${renderGroupHeader(groupId, 'Sections', list.length, true, 'file-text')}
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

// A lease banner reads the row's own lightweight lease (agent_id, action -- always present,
// no watch needed) until the fuller body.lease (branch_name too) arrives.
function leaseBannerHtml(row, body) {
  const lease = (body && body.lease) || row.lease;
  if (!lease) return '';
  const branch = lease.branch_name || `tm/${row.id}`;
  return `
    <div class="p-2.5 bg-blue-950/40 border border-blue-800/80 rounded-lg flex items-center justify-between text-xs mb-3">
      <div class="flex items-center gap-2">
        <span class="text-blue-400">${renderIcon('flame', 'w-3.5 h-3.5')}</span>
        <span class="text-blue-300 font-semibold">Active In-Flight Lease:</span>
        <span class="text-zinc-300 font-mono">${lease.agent_id}</span>
      </div>
      <span class="text-zinc-400 font-mono text-[11px]">${branch}</span>
    </div>
  `;
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


// Sidebar Tree Rendering
function renderTree() {
  treeList.innerHTML = '';
  const byParent = visibleChildrenByParent();

  function createNodeRow(node, depth = 0) {
    const effectiveStatus = displayOf(node);
    const hasChildren = node.child_count > 0;
    const isOpen = expandedIds.has(node.id);

    const row = document.createElement('div');
    // tabindex/role/keydown: the tree is the only detail panel a keyboard user can reach
    // through (vis-network's canvas nodes have no DOM presence to focus), so this row is
    // reachable and operable by keyboard, not only by row.onclick below.
    row.className = `px-2.5 py-1.5 rounded-lg cursor-pointer text-xs group transition focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-emerald-500 ${selectedNodeId === node.id ? 'bg-zinc-800 text-white font-medium border border-zinc-700' : 'text-zinc-400 hover:bg-zinc-850 hover:text-zinc-200'}`;
    row.style.paddingLeft = `${depth * 14 + 8}px`;
    row.setAttribute('role', 'treeitem');
    row.setAttribute('tabindex', '0');
    row.setAttribute('aria-selected', String(selectedNodeId === node.id));
    row.setAttribute('aria-label', `${node.id}: ${node.title}`);

    let chevron = `<span class="w-3.5 h-3.5 inline-block"></span>`;
    if (hasChildren) {
      chevron = `<button class="p-0.5 hover:text-white toggle-btn">${renderIcon(isOpen ? 'chevron-down' : 'chevron-right', 'w-3.5 h-3.5')}</button>`;
    }

    let progressHtml = '';
    let progressBarRow = '';
    if (node.kind !== 'task') {
      const counts = countsForRow(node);
      const p = progressParts(counts);
      if (p.total > 0) {
        progressHtml = `<span class="text-[10px] font-mono text-zinc-400 mr-1" title="${esc(progressText(counts))}">${p.completed}/${p.total}</span>`;
        progressBarRow = `<div class="pt-1.5">${progressBar(counts, 'h-1')}</div>`;
      }
    }

    row.innerHTML = `
      <div class="flex items-center justify-between gap-2">
        <div class="flex items-center gap-1.5 min-w-0 truncate">
          ${chevron}
          ${statusDot(effectiveStatus)}
          <span class="font-mono text-[10px] text-zinc-400 uppercase">${node.id}</span>
          <span class="truncate">${node.title}</span>
        </div>
        <div class="flex items-center gap-1 flex-shrink-0">
          ${progressHtml}
        </div>
      </div>
      ${progressBarRow}
    `;

    const toggleBtn = row.querySelector('.toggle-btn');
    if (toggleBtn) {
      toggleBtn.onclick = (e) => {
        e.stopPropagation();
        toggleExpand(node);
        scheduleRender();
      };
    }

    row.onclick = () => selectNode(node.id);
    row.addEventListener('keydown', (e) => {
      if (e.target !== row) return;  // let the nested toggle button handle its own Enter/Space
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

  (byParent.get(null) || []).forEach(n => createNodeRow(n));
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
    unifiedDocument.innerHTML = '<div class="text-zinc-400 text-sm italic py-12 text-center">No specs, plans or tasks match the current view.</div>';
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


function renderSpecCard(spec, byParent) {
  const specStatus = displayOf(spec);
  const isOpen = expandedIds.has(spec.id);
  const body = bodyOf(spec.id);
  const counts = countsForRow(spec);
  const children = byParent.get(spec.id) || [];

  let specPills = '';
  if (spec.priority) {
    specPills += `<span class="px-2 py-0.5 rounded bg-zinc-900 border border-zinc-800 text-zinc-300 font-mono text-xs">Prio: ${spec.priority}</span>`;
  }
  if (spec.target_repo) {
    specPills += `<span class="px-2 py-0.5 rounded bg-zinc-900 border border-zinc-800 text-cyan-400 font-mono text-xs">${spec.target_repo}</span>`;
  }

  let bodyHtml = '';
  let plansHtml = '';
  if (isOpen) {
    bodyHtml = body ? renderSections(sectionsListFrom(body), spec.id)
      : '<div class="text-xs text-zinc-500 italic py-2">Loading…</div>';
    if (children.length > 0) {
      plansHtml = `
        <div class="space-y-6 pt-4">
          <h2 class="text-xs font-semibold text-zinc-400 uppercase tracking-wider flex items-center gap-2">
            ${renderIcon('layers', 'w-4 h-4 text-emerald-400')}
            <span>Implementation Plans (${children.length})</span>
          </h2>
          <div class="space-y-4">
            ${children.map(plan => renderPlanCard(plan, byParent)).join('')}
          </div>
        </div>
      `;
    }
  }

  return `
    <article id="doc-node-${spec.id}" class="space-y-6 transition duration-200">
      <div class="border-b border-zinc-800 pb-6 space-y-3 cursor-pointer spec-header" data-node-id="${spec.id}">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <button class="text-zinc-400 hover:text-white flex-shrink-0">${renderIcon(isOpen ? 'chevron-down' : 'chevron-right', 'w-4 h-4')}</button>
            <span class="px-2 py-0.5 rounded text-[11px] font-mono uppercase bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">${spec.kind}</span>
            <span class="font-mono text-xs font-semibold text-zinc-400">${spec.id}</span>
            ${statusIcon(specStatus, 'w-4 h-4')}
          </div>
          <div class="flex items-center gap-2">
            ${specPills}
            ${copyIdButton(spec.id)}
          </div>
        </div>
        <h1 class="text-2xl font-bold tracking-tight text-white">${spec.title}</h1>
        <div class="space-y-1.5">
          ${progressBar(counts, 'h-2.5')}
          <div class="text-xs text-zinc-400">${esc(progressText(counts))}</div>
        </div>
      </div>
      ${bodyHtml}
      ${plansHtml}
    </article>
  `;
}


function renderPlanCard(plan, byParent) {
  const planStatus = displayOf(plan);
  const isOpen = expandedIds.has(plan.id);
  const body = bodyOf(plan.id);
  const counts = countsForRow(plan);
  const p = progressParts(counts);
  const tasks = byParent.get(plan.id) || [];
  const tasksGroupId = `${plan.id}::tasks`;
  const tasksGroupCollapsed = groupCollapsed(tasksGroupId, true);

  let innerBody = '';
  if (isOpen) {
    const planSectionsHtml = body ? renderSections(sectionsListFrom(body), plan.id)
      : '<div class="text-xs text-zinc-500 italic py-2">Loading…</div>';
    innerBody = `
      <h3 class="text-base font-semibold text-zinc-200">${plan.title}</h3>
      ${planSectionsHtml}
      <div class="space-y-3">
        ${renderGroupHeader(tasksGroupId, 'Tasks', tasks.length, true, 'check')}
        <div class="space-y-2.5 ${tasksGroupCollapsed ? 'hidden' : ''}">
          ${tasks.map(task => renderTaskCard(task)).join('')}
        </div>
      </div>
    `;
  }

  return `
    <div id="doc-node-${plan.id}" class="border border-zinc-800 rounded-xl bg-zinc-900/30 transition">
      <!-- Plan Header: status + id left, progress + counter right, collapse control last -->
      <div class="h-12 px-4 rounded-t-xl bg-zinc-900/95 backdrop-blur-sm border-b border-zinc-800 flex items-center justify-between cursor-pointer plan-header" data-node-id="${plan.id}">
        <div class="flex items-center gap-2.5 min-w-0 truncate">
          ${statusIcon(planStatus)}
          <span class="font-mono text-xs font-semibold text-emerald-400 flex-shrink-0">${plan.id}</span>
        </div>
        <div class="flex items-center gap-3 flex-shrink-0">
          <div class="flex items-center gap-2">
            <div class="w-40">${progressBar(counts, 'h-2')}</div>
            <span class="font-mono text-xs text-zinc-400" title="Completed of total tasks">${p.completed}/${p.total}</span>
          </div>
          ${copyIdButton(plan.id)}
          <button class="text-zinc-400 hover:text-white flex-shrink-0">${renderIcon(isOpen ? 'chevron-down' : 'chevron-right', 'w-4 h-4')}</button>
        </div>
      </div>

      <!-- Plan Body: title lives here, before sections, folding with everything else -->
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

// A table of tasks (id, status, title) behind a collapsible group header -- the shared
// shape for the task card's Blockers, Dependencies and Dependents sections.
function renderRelationTable(ownerId, key, label, icon, rows, defaultCollapsed) {
  if (!rows || rows.length === 0) return '';
  const groupId = `${ownerId}::${key}`;
  const isCollapsed = groupCollapsed(groupId, defaultCollapsed);
  const body = rows.map(d => `
    <div class="flex items-center gap-2 px-2 py-1.5 bg-zinc-950/60">
      ${d.status
        ? (d.kind === 'decision' && typeof decisionStatusIcon === 'function' ? decisionStatusIcon(d.status) : statusIcon(d.status))
        : '<span class="text-[10px] font-mono text-red-400">missing</span>'}
      <span class="font-mono text-[11px] text-zinc-300 flex-shrink-0">${esc(d.id)}</span>
      <span class="truncate text-[11px] text-zinc-400">${esc(d.title || '')}</span>
    </div>
  `).join('');
  return `
    <div class="space-y-1.5 pt-2">
      ${renderGroupHeader(groupId, label, rows.length, defaultCollapsed, icon)}
      <div class="divide-y divide-zinc-800 rounded border border-zinc-800 ${isCollapsed ? 'hidden' : ''}">${body}</div>
    </div>
  `;
}

function renderTaskCard(task) {
  const taskStatus = displayOf(task);
  const isOpen = expandedIds.has(task.id);
  const body = bodyOf(task.id);

  // Model pills
  let modelPills = '';
  if (task.acceptable_models && task.acceptable_models.length > 0) {
    modelPills = task.acceptable_models.map(m =>
      `<span class="px-1.5 py-0.5 rounded bg-purple-950/60 text-purple-300 border border-purple-800/80 font-mono text-[10px]">${m}</span>`
    ).join('');
  }

  const leaseBanner = leaseBannerHtml(task, body);

  let innerBody = '';
  if (isOpen) {
    if (!body) {
      innerBody = '<div class="text-xs text-zinc-500 italic py-2">Loading…</div>';
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
            ${renderGroupHeader(groupId, 'Verifications', body.verifications.length, true, 'shield-check')}
            <div class="rounded-lg border border-zinc-800 overflow-hidden divide-y divide-zinc-800 text-[11px] font-mono ${isGroupCollapsed ? 'hidden' : ''}">${rows}</div>
          </div>
        `;
      }

      const sectionsHtml = renderSections(sectionsListFrom(body), task.id);
      const unfinishedDeps = (body.dependency_details || []).filter(d => !d.finished);
      const blockersHtml = renderRelationTable(task.id, 'blockers', 'Blockers', 'ban', unfinishedDeps, true);
      const dependenciesHtml = renderRelationTable(task.id, 'deps', 'Dependencies', 'link', body.dependency_details, true);
      const dependentsHtml = renderRelationTable(task.id, 'dependents', 'Dependents', 'arrow-up-right', body.dependent_details, true);

      innerBody = `
        <h3 class="text-sm font-medium text-zinc-200">${task.title}</h3>
        ${leaseBanner}
        ${verificationsHtml}
        ${blockersHtml}
        ${dependenciesHtml}
        ${dependentsHtml}
        ${sectionsHtml}
      `;
    }
  }

  return `
    <div id="doc-node-${task.id}" class="border border-zinc-800/80 rounded-lg bg-zinc-950/40 hover:border-zinc-700 transition">
      <!-- Task Header: status + id left, badges + copy id + collapse control last -->
      <div class="h-10 px-3 rounded-t-lg flex items-center justify-between cursor-pointer task-header bg-zinc-900/90 backdrop-blur-sm hover:bg-zinc-900" data-node-id="${task.id}">
        <div class="flex items-center gap-2 min-w-0 truncate">
          ${statusIcon(taskStatus)}
          <span class="font-mono text-xs font-semibold text-emerald-400 flex-shrink-0">${task.id}</span>
        </div>
        <div class="flex items-center gap-2 flex-shrink-0">
          ${modelPills}
          <span class="px-1.5 py-0.5 rounded bg-zinc-900 border border-zinc-800 text-zinc-400 font-mono text-[10px]">P${task.priority || 50}</span>
          ${copyIdButton(task.id)}
          <button class="text-zinc-500 hover:text-white flex-shrink-0">${renderIcon(isOpen ? 'chevron-down' : 'chevron-right', 'w-3.5 h-3.5')}</button>
        </div>
      </div>

      <!-- Task Body: title lives here too, like the plan card, before everything else -->
      <div class="task-body ${isOpen ? '' : 'hidden'} p-3.5 bg-zinc-950/80 border-t border-zinc-800/60 space-y-2">
        ${innerBody}
      </div>
    </div>
  `;
}

// Shared by the document view (whole-page re-render on toggle) and any other view that embeds
// renderSections()'s output -- the decisions view's detail pane, which has its own re-render
// function rather than renderUnifiedDocument. Without this, a ".group-header" div (a "Sections
// (N)" row included) is inert outside the document view: it renders with a cursor-pointer and
// a chevron that never actually opens, and Enter does nothing because nothing is focusable.
function attachGroupHeaderHandlers(root, rerender) {
  root.querySelectorAll('.group-header').forEach(header => {
    const toggle = () => {
      const id = header.getAttribute('data-group-id');
      if (collapsedGroups.has(id)) collapsedGroups.delete(id);
      else collapsedGroups.add(id);
      rerender();
    };
    header.onclick = toggle;
    header.onkeydown = (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggle();
      }
    };
  });
}

function attachCollapsibleHandlers() {
  document.querySelectorAll('.spec-header, .plan-header, .task-header').forEach(header => {
    header.onclick = () => {
      const id = header.getAttribute('data-node-id');
      const row = window.tmStore.rows.get(id);
      if (row) {
        toggleExpand(row);
        scheduleRender();
      }
    };
  });

  attachGroupHeaderHandlers(document, renderUnifiedDocument);
}
