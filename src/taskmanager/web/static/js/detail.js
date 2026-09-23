// Depends-on list for the graph inspector. Only a task is offered "+ Add dependency" (the
// CLI's own `task depends` is task-scoped); an existing edge on any other kind still gets a
// remove button, since nothing in the schema actually forbids one.
function renderDependencies(details, status, node, editable) {
  const addControl = editable && node.kind === 'task'
    ? `<button type="button" class="add-dep-btn mt-1 h-7 px-2 rounded-md text-[11px] font-medium text-emerald-400 hover:text-emerald-300 hover:bg-zinc-800 border border-dashed border-zinc-700 transition">+ Add dependency</button>`
    : '';
  if (!details || details.length === 0) {
    return addControl ? `<div class="pt-2">${addControl}</div>` : '';
  }
  const unfinished = details.filter(d => !d.finished);
  const why = status === 'BLOCKED' && unfinished.length > 0
    ? `<div class="st-chip st-BLOCKED rounded-lg px-2.5 py-1.5 text-xs">Blocked by ${unfinished.map(d => esc(d.id)).join(', ')}: not completed or superseded yet.</div>`
    : '';
  const rows = details.map(d => `
    <div class="flex items-center gap-2 px-2 py-1.5 bg-zinc-950/60">
      ${d.status ? statusIcon(d.status) : '<span class="text-[10px] font-mono text-red-400">missing</span>'}
      <span class="font-mono text-[11px] text-zinc-300">${esc(d.id)}</span>
      <span class="truncate text-[11px] text-zinc-400 flex-1">${esc(d.title || '')}</span>
      ${editable ? `<button type="button" class="dep-remove-btn p-1 rounded text-zinc-500 hover:text-red-400 hover:bg-zinc-800 flex-shrink-0" data-dep-id="${esc(d.id)}" aria-label="Remove dependency ${esc(d.id)}">${renderIcon('x', 'w-3 h-3')}</button>` : ''}
    </div>
  `).join('');
  return `
    <div class="space-y-1.5 pt-2">
      <div class="font-semibold text-zinc-400 uppercase tracking-wider text-[10px]">Depends on (${details.length})</div>
      ${why}
      <div class="divide-y divide-zinc-800 rounded border border-zinc-800">${rows}</div>
      ${addControl}
    </div>
  `;
}

function wireDependencyControls(root, node) {
  root.querySelectorAll('.dep-remove-btn').forEach(btn => {
    btn.addEventListener('click', () => removeDependency(node, btn.getAttribute('data-dep-id')));
  });
  const addBtn = root.querySelector('.add-dep-btn');
  if (addBtn) addBtn.addEventListener('click', () => openAddDependencyDialog(node));
}


// Verifications: pass/fail run results and edit controls live on the same rows the read-only
// list already renders; a task with none still gets "+ Add verification" when editable.
function renderVerifications(node, verifications, editable) {
  const rows = (verifications || []).map(v => `
    <div class="p-2 bg-zinc-950/60 flex items-center gap-2 justify-between">
      <span class="text-emerald-400 flex-shrink-0" title="${esc(VERIFICATION_LABEL[v.verification_type] || v.verification_type)}">${renderIcon(VERIFICATION_ICON[v.verification_type] || 'check', 'w-3.5 h-3.5')}</span>
      <span class="text-zinc-300 truncate text-left flex-1 min-w-0">${esc(v.target_path)}</span>
      <span class="ver-result text-[10px] font-semibold flex-shrink-0" data-ver-id="${v.id}"></span>
      ${editable ? `<button type="button" class="ver-remove-btn p-1 rounded text-zinc-500 hover:text-red-400 hover:bg-zinc-800 flex-shrink-0" data-ver-id="${v.id}" aria-label="Remove verification ${v.id}">${renderIcon('x', 'w-3 h-3')}</button>` : ''}
    </div>
  `).join('');

  const controls = editable && node.kind === 'task' ? `
    <div class="flex items-center gap-2 pt-1.5">
      <button type="button" class="ver-add-btn h-7 px-2 rounded-md text-[11px] font-medium text-emerald-400 hover:text-emerald-300 hover:bg-zinc-800 border border-dashed border-zinc-700 transition">+ Add verification</button>
      ${verifications && verifications.length > 0 ? `<button type="button" class="ver-run-btn h-7 px-2 rounded-md text-[11px] font-medium text-zinc-300 hover:text-white hover:bg-zinc-800 border border-zinc-700 transition">Run all</button>` : ''}
    </div>
  ` : '';

  if (!verifications || verifications.length === 0) {
    return controls ? `<div class="pt-2 border-t border-zinc-800/60">${controls}</div>` : '';
  }

  return `
    <div class="space-y-1.5 pt-2">
      <div class="font-semibold text-zinc-400 uppercase tracking-wider text-[10px]">Verifications</div>
      <div class="divide-y divide-zinc-800 rounded border border-zinc-800 font-mono text-[11px]">${rows}</div>
      ${controls}
    </div>
  `;
}

async function runVerifications(node) {
  try {
    const results = await api('POST', `/api/nodes/${node.id}/verify`);
    results.forEach(r => {
      const el = document.querySelector(`.ver-result[data-ver-id="${r.id}"]`);
      if (!el) return;
      el.textContent = r.passed ? 'PASS' : 'FAIL';
      el.className = `ver-result text-[10px] font-semibold flex-shrink-0 ${r.passed ? 'text-emerald-400' : 'text-red-400'}`;
      el.title = r.detail || '';
    });
    const passed = results.filter(r => r.passed).length;
    toast(`${passed}/${results.length} verifications passed.`, passed === results.length ? 'success' : 'error');
  } catch (e) {
    toast(e.message, 'error');
  }
}

function wireVerificationControls(root, node) {
  root.querySelectorAll('.ver-remove-btn').forEach(btn => {
    btn.addEventListener('click', () => removeVerification(node, Number(btn.getAttribute('data-ver-id'))));
  });
  const addBtn = root.querySelector('.ver-add-btn');
  if (addBtn) addBtn.addEventListener('click', () => openAddVerificationDialog(node));
  const runBtn = root.querySelector('.ver-run-btn');
  if (runBtn) runBtn.addEventListener('click', () => runVerifications(node));
}


// Sections render through tree.js's renderSections (shared with the document view); editing
// controls are layered on afterwards by walking the rendered <details> rather than
// duplicating that markup here.
function attachSectionEditControls(root, node, sections) {
  const byKey = new Map((sections || []).map(s => [s.key, s]));
  root.querySelectorAll('details[data-section-id]').forEach(details => {
    const key = details.getAttribute('data-section-id').split('::').slice(1).join('::');
    const section = byKey.get(key);
    const summary = details.querySelector('summary');
    if (!section || !summary) return;
    const controls = document.createElement('div');
    controls.className = 'flex items-center gap-1 flex-shrink-0';
    controls.innerHTML = `
      <button type="button" class="sec-edit-btn p-1 rounded text-zinc-400 hover:text-white hover:bg-zinc-800" aria-label="Edit section ${esc(key)}">${renderIcon('code', 'w-3 h-3')}</button>
      <button type="button" class="sec-delete-btn p-1 rounded text-zinc-400 hover:text-red-400 hover:bg-zinc-800" aria-label="Delete section ${esc(key)}">${renderIcon('x', 'w-3 h-3')}</button>
    `;
    controls.querySelector('.sec-edit-btn').addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      openSectionDialog(node, section);
    });
    controls.querySelector('.sec-delete-btn').addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      removeSection(node, key);
    });
    summary.insertBefore(controls, summary.lastElementChild);
  });

  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'sec-add-btn mt-2 h-7 px-2 rounded-md text-[11px] font-medium text-emerald-400 hover:text-emerald-300 hover:bg-zinc-800 border border-dashed border-zinc-700 transition';
  addBtn.textContent = '+ Add section';
  addBtn.addEventListener('click', () => openSectionDialog(node, null));

  const groupHeader = root.querySelector(`[data-group-id="${node.id}::sections"]`);
  if (groupHeader && groupHeader.closest('.space-y-2')) {
    groupHeader.closest('.space-y-2').appendChild(addBtn);
  } else {
    root.appendChild(addBtn);
  }
}


// Action bar: create/edit, status transitions, supersede/move (task only) and lease release
// (when one is held). A plan or spec only ever gets Edit and Status.
function renderActionBar(node, hasLease) {
  const btnCls = 'h-7 px-2.5 rounded-md text-[11px] font-medium bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700 transition';
  const dangerCls = 'h-7 px-2.5 rounded-md text-[11px] font-medium bg-zinc-900 hover:bg-red-950 text-red-300 border border-red-900/60 transition';
  const buttons = [
    `<button type="button" class="ab-edit ${btnCls}">Edit</button>`,
    `<button type="button" class="ab-complete ${btnCls}">Mark completed</button>`,
    `<button type="button" class="ab-defer ${btnCls}">Defer</button>`,
    `<button type="button" class="ab-reopen ${btnCls}">Reopen</button>`,
    `<button type="button" class="ab-abandon ${dangerCls}">Abandon</button>`,
    `<button type="button" class="ab-other-status ${btnCls}">Other status&hellip;</button>`,
  ];
  if (node.kind === 'task') {
    buttons.push(`<button type="button" class="ab-supersede ${dangerCls}">Supersede&hellip;</button>`);
    buttons.push(`<button type="button" class="ab-move ${btnCls}">Move to plan&hellip;</button>`);
  }
  if (hasLease) {
    buttons.push(`<button type="button" class="ab-release ${dangerCls}">Release lease</button>`);
  }
  return `<div class="ab-bar flex flex-wrap gap-1.5 pb-2 border-b border-zinc-800/80">${buttons.join('')}</div>`;
}

function wireActionBar(root, node) {
  const bar = root.querySelector('.ab-bar');
  if (!bar) return;
  const on = (selector, fn) => {
    const el = bar.querySelector(selector);
    if (el) el.addEventListener('click', fn);
  };
  on('.ab-edit', () => openEditNodeDialog(node));
  on('.ab-complete', () => changeStatus(node, 'COMPLETED'));
  on('.ab-defer', () => changeStatus(node, 'DEFERRED'));
  on('.ab-reopen', () => changeStatus(node, 'NOT_STARTED'));
  on('.ab-abandon', () => changeStatus(node, 'ABANDONED'));
  on('.ab-other-status', () => openOtherStatusDialog(node, node.status));
  on('.ab-supersede', () => openSupersedeDialog(node));
  on('.ab-move', () => openMoveDialog(node));
  on('.ab-release', () => releaseLease(node));
}


// Slide-over Node Inspector for Graph
async function showGraphInspector(nodeId) {
  selectedNodeId = nodeId;
  graphInspector.classList.remove('hidden');

  let detail = null;
  if (isStaticMode) {
    detail = (window.STATIC_DATA.details || {})[nodeId];
  } else {
    try {
      const res = await fetch(`/api/nodes/${nodeId}`);
      if (res.ok) detail = await res.json();
    } catch (e) {
      console.error('Failed to load node detail:', e);
    }
  }

  if (!detail) return;

  const n = detail.node;
  const status = detail.virtual_status || n.status;
  const editable = canEdit();

  document.getElementById('inspector-kind').textContent = n.kind;
  document.getElementById('inspector-id').textContent = n.id;
  document.getElementById('inspector-title').textContent = n.title;

  const body = document.getElementById('inspector-body');
  let leaseBanner = '';
  if (detail.lease) {
    leaseBanner = `
      <div class="p-2.5 bg-blue-950/40 border border-blue-800/80 rounded-lg text-xs space-y-1">
        <div class="text-blue-300 font-semibold flex items-center gap-1.5">
          ${renderIcon('flame', 'w-3.5 h-3.5')}
          <span>In-Flight Active Lease</span>
        </div>
        <div class="text-zinc-400 font-mono">Agent: ${detail.lease.agent_id}</div>
        <div class="text-zinc-400 font-mono text-[11px]">${detail.lease.branch_name}</div>
      </div>
    `;
  }

  body.innerHTML = `
    ${editable ? renderActionBar(n, !!detail.lease) : ''}
    <div class="flex items-center gap-2">
      ${statusIcon(status, 'w-4 h-4')}
      <span class="px-2 py-0.5 rounded bg-zinc-950 border border-zinc-800 font-mono text-zinc-400 text-xs">Prio: ${n.priority || 50}</span>
    </div>
    ${leaseBanner}
    ${renderVerifications(n, detail.verifications, editable)}
    ${renderDependencies(detail.dependency_details, status, n, editable)}
    ${renderSections(detail.sections, n.id)}
  `;
  attachSectionToggleHandlers(body);
  attachInspectorGroupToggleHandlers(body, nodeId);
  if (editable) {
    wireActionBar(body, n);
    wireVerificationControls(body, n);
    wireDependencyControls(body, n);
    attachSectionEditControls(body, n, detail.sections);
  }
}

// tree.js's attachCollapsibleHandlers() wires .group-header clicks only for the document
// view (it queries the whole document, but only after renderUnifiedDocument() runs, and
// showGraphInspector() rebuilds this body afterwards) -- so the inspector's own "Sections"
// and "Verifications" group headers need the same collapsedGroups toggle wired here.
function attachInspectorGroupToggleHandlers(root, nodeId) {
  root.querySelectorAll('.group-header').forEach(header => {
    header.onclick = () => {
      const id = header.getAttribute('data-group-id');
      if (collapsedGroups.has(id)) collapsedGroups.delete(id);
      else collapsedGroups.add(id);
      showGraphInspector(nodeId);
    };
  });
}
