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

// Select Node Action (Coordinates Tree and Graph)

function selectNode(nodeId) {
  selectedNodeId = nodeId;
  renderTree();

  if (currentMode === window.VIEW_MODES.GRAPH) {
    if (networkInstance) {
      networkInstance.selectNodes([nodeId]);
      networkInstance.focus(nodeId, { scale: 1.1, animation: true });
    }
    showGraphInspector(nodeId);
  }
}

// Verification kind -> icon, so the row reads at a glance instead of naming the enum value.
// Each one is its own icon rather than borrowing a status or toolbar icon's meaning
// (file-text/network/play already mean Waves view, Graph view and Implementing).

const VERIFICATION_ICON = {
  file_exists: 'file-check', file_absent: 'file-x', symbol_signature: 'code',
  ast_export: 'package', test_command: 'terminal', codegraph_query: 'database'
};
const VERIFICATION_LABEL = {
  file_exists: 'File exists', file_absent: 'File absent', symbol_signature: 'Symbol signature',
  ast_export: 'AST export', test_command: 'Test command', codegraph_query: 'Codegraph query'
};

// A ".group-header" row renders with a cursor-pointer and a chevron but wires no click or
// keyboard toggle of its own -- the decisions view's detail pane calls this after every
// render of renderSections()'s output, since re-rendering there is its own callback rather
// than a whole-page redraw.
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
