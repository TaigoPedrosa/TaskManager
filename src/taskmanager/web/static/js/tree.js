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
  return `
    <div class="group-header flex items-center justify-between gap-2 cursor-pointer select-none" data-group-id="${groupId}">
      <div class="flex items-center gap-2 min-w-0">
        ${leadIcon}
        <span class="text-[11px] font-semibold uppercase tracking-wider text-zinc-400">${esc(label)} (${count})</span>
      </div>
      <button class="text-zinc-500 hover:text-white flex-shrink-0">${renderIcon(isCollapsed ? 'chevron-right' : 'chevron-down', 'w-3 h-3')}</button>
    </div>
  `;
}

// Sections default collapsed; expandedSections remembers, for this session only, which
// ones the user opened, so re-rendering after a filter change never re-collapses them.
// The "Sections (N)" group header is a level above that: it hides the whole row of
// <details> summaries at once, independent of expandedSections and of collapsedNodes.
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


// Sidebar Tree Rendering
function renderTree(nodes) {
  treeList.innerHTML = '';

  function createNodeRow(node, depth = 0, parentTextOk = false) {
    if (!nodeVisible(node, parentTextOk)) return;
    const textOk = textAccepts(node, parentTextOk);

    const effectiveStatus = node.virtual_status || node.status;
    const hasChildren = node.children && node.children.length > 0;
    const isCollapsed = collapsedNodes.has(node.id);

    const row = document.createElement('div');
    row.className = `px-2.5 py-1.5 rounded-lg cursor-pointer text-xs group transition ${selectedNodeId === node.id ? 'bg-zinc-800 text-white font-medium border border-zinc-700' : 'text-zinc-400 hover:bg-zinc-850 hover:text-zinc-200'}`;
    row.style.paddingLeft = `${depth * 14 + 8}px`;

    let chevron = `<span class="w-3.5 h-3.5 inline-block"></span>`;
    if (hasChildren) {
      chevron = `<button class="p-0.5 hover:text-white toggle-btn">${renderIcon(isCollapsed ? 'chevron-right' : 'chevron-down', 'w-3.5 h-3.5')}</button>`;
    }

    let progressHtml = '';
    if (node.progress && node.progress.total > 0) {
      const p = progressParts(node);
      progressHtml = `<span class="text-[10px] font-mono text-zinc-400 mr-1" title="${esc(progressText(node))}">${p.completed}/${p.total}</span>`;
    }

    const progressBarRow = progressHtml ? `<div class="pt-1.5">${progressBar(node, 'h-1')}</div>` : '';

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
        if (isCollapsed) collapsedNodes.delete(node.id);
        else collapsedNodes.add(node.id);
        renderTree(treeData);
        renderUnifiedDocument();
      };
    }

    row.onclick = () => selectNode(node.id);
    treeList.appendChild(row);

    if (hasChildren && !isCollapsed) {
      node.children.forEach(c => createNodeRow(c, depth + 1, textOk));
    }
  }

  nodes.forEach(n => createNodeRow(n));
}

searchBox.addEventListener('input', (e) => {
  filters.q = e.target.value.toLowerCase();
  renderAll();
});

// Select Node Action (Coordinates Tree, Document, and Graph)

function selectNode(nodeId) {
  selectedNodeId = nodeId;
  renderTree(treeData);

  if (currentMode === window.VIEW_MODES.DOCUMENT) {
    const targetEl = document.getElementById(`doc-node-${nodeId}`);
    if (targetEl) {
      // If inside a collapsed plan, expand it
      collapsedNodes.delete(nodeId);
      renderUnifiedDocument();
      const refreshedEl = document.getElementById(`doc-node-${nodeId}`);
      if (refreshedEl) {
        refreshedEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        refreshedEl.classList.add('node-highlighted');
        setTimeout(() => refreshedEl.classList.remove('node-highlighted'), 1600);
      }
    }
  } else if (currentMode === window.VIEW_MODES.GRAPH) {
    if (networkInstance) {
      networkInstance.selectNodes([nodeId]);
      networkInstance.focus(nodeId, { scale: 1.1, animation: true });
    }
    showGraphInspector(nodeId);
  }
}


// Render Unified Document View
function renderUnifiedDocument() {
  unifiedDocument.innerHTML = '';
  allSectionIds = [];

  if (treeData.length === 0) {
    unifiedDocument.innerHTML = '<div class="text-zinc-400 text-sm italic py-12 text-center">No specifications or tasks registered. Use CLI to add items.</div>';
    return;
  }

  treeData.filter(root => nodeVisible(root)).forEach(spec => {
    const specStatus = spec.virtual_status || spec.status;
    const specTextOk = textAccepts(spec, false);

    const specCard = document.createElement('article');
    specCard.id = `doc-node-${spec.id}`;
    specCard.className = 'space-y-6 transition duration-200';

    // Spec Header Banner
    let specPills = '';
    if (spec.priority) {
      specPills += `<span class="px-2 py-0.5 rounded bg-zinc-900 border border-zinc-800 text-zinc-300 font-mono text-xs">Prio: ${spec.priority}</span>`;
    }
    if (spec.target_repo) {
      specPills += `<span class="px-2 py-0.5 rounded bg-zinc-900 border border-zinc-800 text-cyan-400 font-mono text-xs">${spec.target_repo}</span>`;
    }

    const specSectionsHtml = renderSections(spec.sections, spec.id);

    // Render Plans
    let plansHtml = '';
    if (spec.children && spec.children.length > 0) {
      plansHtml = `
        <div class="space-y-6 pt-4">
          <h2 class="text-xs font-semibold text-zinc-400 uppercase tracking-wider flex items-center gap-2">
            ${renderIcon('layers', 'w-4 h-4 text-emerald-400')}
            <span>Implementation Plans (${spec.children.length})</span>
          </h2>
          <div class="space-y-4">
            ${spec.children.filter(plan => nodeVisible(plan, specTextOk)).map(plan => renderPlanCard(plan, specTextOk)).join('')}
          </div>
        </div>
      `;
    }

    specCard.innerHTML = `
      <div class="border-b border-zinc-800 pb-6 space-y-3">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
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
          ${progressBar(spec, 'h-2.5')}
          <div class="text-xs text-zinc-400">${esc(progressText(spec))}</div>
        </div>
      </div>
      ${specSectionsHtml}
      ${plansHtml}
    `;

    unifiedDocument.appendChild(specCard);
  });

  if (!unifiedDocument.hasChildNodes()) {
    unifiedDocument.innerHTML = '<div class="text-zinc-400 text-sm italic py-12 text-center">No tasks match the active filters.</div>';
  }

  attachCollapsibleHandlers();
  attachSectionToggleHandlers(unifiedDocument);
  attachCopyHandlers(unifiedDocument);
  updateToggleSectionsButton();
}


function renderPlanCard(plan, parentTextOk = false) {
  const planStatus = plan.virtual_status || plan.status;
  const isCollapsed = collapsedNodes.has(plan.id);

  const tasks = plan.children || [];
  const p = progressParts(plan);
  const planTextOk = textAccepts(plan, parentTextOk);
  const visibleTasks = tasks.filter(t => nodeVisible(t, planTextOk));
  const planSectionsHtml = renderSections(plan.sections, plan.id);
  const tasksGroupId = `${plan.id}::tasks`;
  const tasksGroupCollapsed = groupCollapsed(tasksGroupId, true);

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
            <div class="w-40">${progressBar(plan, 'h-2')}</div>
            <span class="font-mono text-xs text-zinc-400" title="Completed of total tasks">${p.completed}/${p.total}</span>
          </div>
          ${copyIdButton(plan.id)}
          <button class="text-zinc-400 hover:text-white flex-shrink-0">${renderIcon(isCollapsed ? 'chevron-right' : 'chevron-down', 'w-4 h-4')}</button>
        </div>
      </div>

      <!-- Plan Body: title lives here, before sections, folding with everything else -->
      <div class="plan-body ${isCollapsed ? 'hidden' : ''} p-4 space-y-4">
        <h3 class="text-base font-semibold text-zinc-200">${plan.title}</h3>
        ${planSectionsHtml}
        <div class="space-y-3">
          ${renderGroupHeader(tasksGroupId, 'Tasks', visibleTasks.length, true, 'check')}
          <div class="space-y-2.5 ${tasksGroupCollapsed ? 'hidden' : ''}">
            ${visibleTasks.map(task => renderTaskCard(task)).join('')}
          </div>
        </div>
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
      ${d.status ? statusIcon(d.status) : '<span class="text-[10px] font-mono text-red-400">missing</span>'}
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
  const taskStatus = task.virtual_status || task.status;
  const isCollapsed = collapsedNodes.has(task.id);

  // Model pills
  let modelPills = '';
  if (task.acceptable_models && task.acceptable_models.length > 0) {
    modelPills = task.acceptable_models.map(m =>
      `<span class="px-1.5 py-0.5 rounded bg-purple-950/60 text-purple-300 border border-purple-800/80 font-mono text-[10px]">${m}</span>`
    ).join('');
  }

  // Lease banner
  let leaseBanner = '';
  if (task.lease) {
    leaseBanner = `
      <div class="p-2.5 bg-blue-950/40 border border-blue-800/80 rounded-lg flex items-center justify-between text-xs mb-3">
        <div class="flex items-center gap-2">
          <span class="text-blue-400">${renderIcon('flame', 'w-3.5 h-3.5')}</span>
          <span class="text-blue-300 font-semibold">Active In-Flight Lease:</span>
          <span class="text-zinc-300 font-mono">${task.lease.agent_id}</span>
        </div>
        <span class="text-zinc-400 font-mono text-[11px]">${task.lease.branch_name}</span>
      </div>
    `;
  }

  // Verifications: icon names the kind (hover for the word), row stays justified
  // (icon pinned left, target pinned right) but the target text itself reads left-aligned;
  // a real gap keeps the two from ever touching regardless of either one's length.
  let verificationsHtml = '';
  if (task.verifications && task.verifications.length > 0) {
    const groupId = `${task.id}::verifications`;
    const isGroupCollapsed = groupCollapsed(groupId, true);
    const rows = task.verifications.map(v => `
      <div class="p-2 bg-zinc-950/60 flex items-center gap-3 justify-between">
        <span class="text-emerald-400 flex-shrink-0" title="${esc(VERIFICATION_LABEL[v.type] || v.type)}">${renderIcon(VERIFICATION_ICON[v.type] || 'check', 'w-3.5 h-3.5')}</span>
        <span class="text-zinc-300 truncate text-left flex-1 min-w-0">${esc(v.target)}</span>
      </div>
    `).join('');
    verificationsHtml = `
      <div class="space-y-1.5 pt-2 border-t border-zinc-800/60 mb-3">
        ${renderGroupHeader(groupId, 'Verifications', task.verifications.length, true, 'shield-check')}
        <div class="rounded-lg border border-zinc-800 overflow-hidden divide-y divide-zinc-800 text-[11px] font-mono ${isGroupCollapsed ? 'hidden' : ''}">${rows}</div>
      </div>
    `;
  }

  const sectionsHtml = renderSections(task.sections, task.id);
  const unfinishedDeps = (task.dependency_details || []).filter(d => !d.finished);
  const blockersHtml = renderRelationTable(task.id, 'blockers', 'Blockers', 'ban', unfinishedDeps, true);
  const dependenciesHtml = renderRelationTable(task.id, 'deps', 'Dependencies', 'link', task.dependency_details, true);
  const dependentsHtml = renderRelationTable(task.id, 'dependents', 'Dependents', 'arrow-up-right', task.dependent_details, true);

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
          <button class="text-zinc-500 hover:text-white flex-shrink-0">${renderIcon(isCollapsed ? 'chevron-right' : 'chevron-down', 'w-3.5 h-3.5')}</button>
        </div>
      </div>

      <!-- Task Body: title lives here too, like the plan card, before everything else -->
      <div class="task-body ${isCollapsed ? 'hidden' : ''} p-3.5 bg-zinc-950/80 border-t border-zinc-800/60 space-y-2">
        <h3 class="text-sm font-medium text-zinc-200">${task.title}</h3>
        ${leaseBanner}
        ${verificationsHtml}
        ${blockersHtml}
        ${dependenciesHtml}
        ${dependentsHtml}
        ${sectionsHtml}
      </div>
    </div>
  `;
}

function attachCollapsibleHandlers() {
  document.querySelectorAll('.plan-header').forEach(header => {
    header.onclick = () => {
      const id = header.getAttribute('data-node-id');
      if (collapsedNodes.has(id)) collapsedNodes.delete(id);
      else collapsedNodes.add(id);
      renderUnifiedDocument();
      renderTree(treeData);
    };
  });

  document.querySelectorAll('.task-header').forEach(header => {
    header.onclick = () => {
      const id = header.getAttribute('data-node-id');
      if (collapsedNodes.has(id)) collapsedNodes.delete(id);
      else collapsedNodes.add(id);
      renderUnifiedDocument();
    };
  });

  document.querySelectorAll('.group-header').forEach(header => {
    header.onclick = () => {
      const id = header.getAttribute('data-group-id');
      if (collapsedGroups.has(id)) collapsedGroups.delete(id);
      else collapsedGroups.add(id);
      renderUnifiedDocument();
    };
  });
}


