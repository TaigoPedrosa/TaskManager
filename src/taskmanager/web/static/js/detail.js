// Depends-on list for the graph inspector. Only a task is offered "+ Add dependency" (the
// CLI's own `task depends` is task-scoped); an existing edge on any other kind still gets a
// remove button, since nothing in the schema actually forbids one. A row a container or a
// migration chain imposes has none: removing it belongs to that container or that chain.
function renderDependencies(details, status, node, editable) {
  const addControl = editable && node.kind === 'task'
    ? `
      <div class="flex items-center gap-2 mt-1">
        <button type="button" class="add-dep-btn h-7 px-2 rounded-md text-[11px] font-medium text-emerald-400 hover:text-emerald-300 hover:bg-zinc-800 border border-dashed border-zinc-700 transition">+ Add dependency</button>
        <button type="button" class="add-decision-dep-btn h-7 px-2 rounded-md text-[11px] font-medium text-amber-400 hover:text-amber-300 hover:bg-zinc-800 border border-dashed border-zinc-700 transition">+ Wait on decision</button>
      </div>`
    : '';
  if (!details || details.length === 0) {
    return addControl ? `<div class="pt-2">${addControl}</div>` : '';
  }
  const unfinished = details.filter(d => !d.finished);
  const why = status === 'BLOCKED_BY_TASK' && unfinished.length > 0
    ? `<div class="st-chip st-BLOCKED_BY_TASK rounded-lg px-2.5 py-1.5 text-xs">Waits for ${unfinished.map(d => esc(d.id)).join(', ')} to land where this node builds.</div>`
    : '';
  const rows = details.map(d => `
    <div class="flex items-center gap-2 px-2 py-1.5 bg-zinc-950/60">
      ${d.status ? statusIcon(d.status) : '<span class="text-[10px] font-mono text-red-400">missing</span>'}
      <span class="font-mono text-[11px] text-zinc-300">${esc(d.id)}</span>
      <span class="truncate text-[11px] text-zinc-400 flex-1">${esc(d.title || '')}</span>
      ${d.inherited_from ? `<span class="text-[10px] text-zinc-500 flex-shrink-0">via ${esc(d.inherited_from)}</span>` : ''}
      ${d.migration_chain ? `<span class="text-[10px] text-zinc-500 flex-shrink-0">${esc(d.migration_chain)} migration chain</span>` : ''}
      ${editable && !d.inherited_from && !d.migration_chain ? `<button type="button" class="dep-remove-btn p-1 rounded text-zinc-500 hover:text-red-400 hover:bg-zinc-800 flex-shrink-0" data-dep-id="${esc(d.id)}" aria-label="Remove dependency ${esc(d.id)}">${renderIcon('x', 'w-3 h-3')}</button>` : ''}
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
  const addDecisionBtn = root.querySelector('.add-decision-dep-btn');
  if (addDecisionBtn) addDecisionBtn.addEventListener('click', () => openAddDependencyDialog(node, true));
}


// Verifications: pass/fail run results and edit controls live on the same rows the read-only
// list already renders; a task with none still gets "+ Add verification" when editable.
function renderVerifications(node, verifications, editable) {
  const rows = (verifications || []).map(v => `
    <div class="p-2 bg-zinc-950/60 flex items-center gap-2 justify-between">
      <span class="text-emerald-400 flex-shrink-0" title="${esc(VERIFICATION_LABEL[v.verification_type] || v.verification_type)}">${renderIcon(VERIFICATION_ICON[v.verification_type] || 'check', 'w-3.5 h-3.5')}</span>
      <span class="text-zinc-300 truncate text-left flex-1 min-w-0">${esc(v.target_path)}</span>
      <span class="ver-result text-[10px] font-semibold flex-shrink-0" data-ver-id="${v.id}"></span>
      ${editable ? `<button type="button" class="ver-remove-btn p-1 rounded text-zinc-500 hover:text-red-400 hover:bg-zinc-800 flex-shrink-0" data-ver-id="${v.id}" data-ver-target="${esc(v.target_path)}" aria-label="Remove verification ${esc(v.target_path)}">${renderIcon('x', 'w-3 h-3')}</button>` : ''}
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
    btn.addEventListener('click', () => removeVerification(
      node, Number(btn.getAttribute('data-ver-id')), btn.getAttribute('data-ver-target')
    ));
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


// Verbs, never a status picker: each button is a transition the stored status allows, and
// every one of them asks for the note it records.
const REOPENABLE = ['FAILED', 'DEFERRED', 'ABANDONED'];
const SETTABLE_ASIDE = ['READY', 'IMPLEMENTED', 'REVIEWED', 'FIXED', 'LANDED', 'FAILED'];

function renderActionBar(node, hasLease) {
  const btnCls = 'h-7 px-2.5 rounded-md text-[11px] font-medium bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700 transition';
  const dangerCls = 'h-7 px-2.5 rounded-md text-[11px] font-medium bg-zinc-900 hover:bg-red-950 text-red-300 border border-red-900/60 transition';
  const buttons = [
    `<button type="button" class="ab-edit ${btnCls}">Edit</button>`,
  ];
  if (node.kind !== 'decision') {
    buttons.push(`<button type="button" class="ab-flags ${btnCls}">Flags&hellip;</button>`);
  }
  if (REOPENABLE.includes(node.status)) {
    buttons.push(`<button type="button" class="ab-reopen ${btnCls}">Reopen&hellip;</button>`);
  }
  if (!hasLease) {
    buttons.push(`<button type="button" class="ab-reset ${btnCls}">Reset&hellip;</button>`);
  }
  if (!hasLease && SETTABLE_ASIDE.includes(node.status)) {
    buttons.push(`<button type="button" class="ab-defer ${btnCls}">Defer&hellip;</button>`);
    buttons.push(`<button type="button" class="ab-abandon ${dangerCls}">Abandon&hellip;</button>`);
  }
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
  on('.ab-flags', () => openFlagsDialog(node));
  on('.ab-reopen', () => openVerbDialog(node, 'reopen'));
  on('.ab-reset', () => openResetDialog(node));
  on('.ab-defer', () => openVerbDialog(node, 'defer'));
  on('.ab-abandon', () => openVerbDialog(node, 'abandon'));
  on('.ab-supersede', () => openSupersedeDialog(node));
  on('.ab-move', () => openMoveDialog(node));
  on('.ab-release', () => releaseLease(node));
}


// Where a node lands, read from its base chain: "on tm/P; waits for P → main".
function landingChainText(node) {
  const base = (node.base_chain || []).map(id => (id === 'MAIN' ? 'main' : id));
  if (base.length === 0) return '';
  if (base[0] === 'main') return 'lands on main';
  return `on tm/${esc(base[0])}; waits for ${base.map(esc).join(' → ')}`;
}

function renderLifecycle(detail, editable) {
  const n = detail.node;
  if (n.kind === 'decision') return '';
  const row = (label, value) => `
    <div class="flex gap-2"><dt class="w-28 flex-shrink-0 text-zinc-500">${esc(label)}</dt><dd class="text-zinc-300 min-w-0 break-words">${value}</dd></div>`;
  const rows = [
    row('Stored status', `${esc(n.status)} ${phaseChip(detail.phase)}`),
    row('Outcome', esc(n.outcome || '-')),
    row('Verdict', esc(n.verdict || '-')),
    row('Flags', `review ${n.review ? 'on' : 'off'} · fix ${n.fix ? 'on' : 'off'}`),
    row('Lands', `${esc(n.merge)} · ${landingChainText(n)}`),
    row('Counters', `reviews ${n.review_cycles} · merge attempts ${n.merge_attempts} · step failures ${n.step_failures}`),
    row('Requires', esc((n.requires || []).join(', ') || '-')),
  ];
  if (n.land_order && n.land_order.length) rows.push(row('Land order', esc(n.land_order.join(' → '))));
  const conditions = (detail.conditions || []).map(c => `
    <div class="flex items-center gap-2 px-2 py-1.5 bg-zinc-950/60">
      <span class="text-[11px] text-zinc-300 flex-1 min-w-0 truncate">${esc(c.needs)}</span>
      <span class="text-[10px] uppercase text-zinc-500">${esc(c.stage)}</span>
      <code class="text-[10px] text-zinc-400 truncate max-w-[40%]">${esc(c.command)}</code>
      <span class="text-[10px] ${c.last_result === 0 ? 'text-emerald-400' : 'text-amber-400'}">${c.last_result === null || c.last_result === undefined ? 'not run' : (c.last_result === 0 ? 'holds' : `exit ${c.last_result}`)}</span>
      ${editable ? `<button type="button" class="cond-remove-btn p-1 rounded text-zinc-500 hover:text-red-400 hover:bg-zinc-800" data-idx="${c.idx}" data-needs="${esc(c.needs)}" aria-label="Remove condition ${esc(c.needs)}">${renderIcon('x', 'w-3 h-3')}</button>` : ''}
    </div>`).join('');
  const jobs = (detail.jobs || []).map(j => `
    <div class="px-2 py-1.5 bg-zinc-950/60 text-[11px] text-zinc-300">${esc(j.kind)} ${esc(j.repo || '')} → ${esc(j.target || '')}: <strong>${esc(j.state)}</strong>${j.step ? ` at ${esc(j.step)}` : ''}</div>`).join('');
  return `
    <div class="lc-panel space-y-2 pt-2 text-xs">
      <dl class="space-y-1">${rows.join('')}</dl>
      <div class="font-semibold text-zinc-400 uppercase tracking-wider text-[10px]">Conditions (${(detail.conditions || []).length})</div>
      ${conditions ? `<div class="divide-y divide-zinc-800 rounded border border-zinc-800">${conditions}</div>` : ''}
      ${editable ? '<button type="button" class="cond-add-btn h-7 px-2 rounded-md text-[11px] font-medium text-emerald-400 hover:text-emerald-300 hover:bg-zinc-800 border border-dashed border-zinc-700 transition">+ Add condition</button>' : ''}
      ${jobs ? `<div class="font-semibold text-zinc-400 uppercase tracking-wider text-[10px]">Jobs</div><div class="divide-y divide-zinc-800 rounded border border-zinc-800">${jobs}</div>` : ''}
    </div>
  `;
}

function wireLifecycleControls(root, node) {
  root.querySelectorAll('.cond-remove-btn').forEach(btn => {
    btn.addEventListener('click', () => removeCondition(node, Number(btn.dataset.idx), btn.dataset.needs));
  });
  const add = root.querySelector('.cond-add-btn');
  if (add) add.addEventListener('click', () => openAddConditionDialog(node));
}


// Slide-over Node Inspector for Graph: opening it watches the node through the store (the
// same watch tree.js's own expand uses) and every later render reads straight off
// window.tmStore.rows/bodies, re-run by the onChange listener below -- no fetch, no reload,
// ever, on either open or a write made from inside it.
let inspectorNodeId = null;

// A node still expanded in the tree/document keeps its own reason to stay watched; only
// drop the watch here when closing or switching leaves nothing else asking for it.
function releaseInspectorWatch(id) {
  if (id && !expandedIds.has(id)) window.tmStore.unwatch([id]);
}

async function showGraphInspector(nodeId) {
  if (inspectorNodeId && inspectorNodeId !== nodeId) releaseInspectorWatch(inspectorNodeId);
  inspectorNodeId = nodeId;
  selectedNodeId = nodeId;
  graphInspector.classList.remove('hidden');
  window.tmStore.watch([nodeId]);
  renderGraphInspector(nodeId);
}

inspectorCloseBtn.addEventListener('click', () => {
  releaseInspectorWatch(inspectorNodeId);
  inspectorNodeId = null;
});

function renderGraphInspector(nodeId) {
  const row = window.tmStore.rows.get(nodeId);
  const body = window.tmStore.bodies.get(nodeId);
  const kindEl = document.getElementById('inspector-kind');
  const idEl = document.getElementById('inspector-id');
  const titleEl = document.getElementById('inspector-title');
  const bodyEl = document.getElementById('inspector-body');

  if (!row && !body) {
    kindEl.textContent = '';
    idEl.textContent = nodeId;
    titleEl.textContent = '';
    bodyEl.innerHTML = '<div class="text-xs text-red-400 italic py-6 text-center">Node not found.</div>';
    return;
  }

  const n = (body && body.node) || row;
  kindEl.textContent = n.kind;
  idEl.textContent = n.id;
  titleEl.textContent = n.title;

  if (!body) {
    bodyEl.innerHTML = '<div class="text-xs text-zinc-500 italic py-6 text-center">Loading&hellip;</div>';
    return;
  }

  const detail = {
    node: body.node,
    dependency_details: body.dependency_details,
    dependent_details: body.dependent_details,
    sections: sectionsListFrom(body),
    verifications: body.verifications,
    conditions: body.conditions,
    jobs: body.jobs,
    lease: body.lease,
    phase: row ? row.phase : null,
    display: row ? row.display : null,
  };
  const status = detail.display || n.status;
  const editable = canEdit();

  let leaseBanner = '';
  if (detail.lease) {
    leaseBanner = `
      <div class="p-2.5 bg-blue-950/40 border border-blue-800/80 rounded-lg text-xs space-y-1">
        <div class="text-blue-300 font-semibold flex items-center gap-1.5">
          ${renderIcon('bot', 'w-3.5 h-3.5')}
          <span>Live lease: ${esc(detail.lease.action || 'step')}</span>
        </div>
        <div class="text-zinc-400 font-mono">Agent: ${esc(detail.lease.agent_id)}</div>
        <div class="text-zinc-400 font-mono text-[11px]">${esc(detail.lease.branch_name)}</div>
      </div>
    `;
  }

  const attachments = (n.frontmatter && n.frontmatter.attachments) || [];

  bodyEl.innerHTML = `
    ${editable ? renderActionBar(n, !!detail.lease) : ''}
    <div class="flex items-center gap-2">
      ${statusIcon(status, 'w-4 h-4')}
      <span class="px-2 py-0.5 rounded bg-zinc-950 border border-zinc-800 font-mono text-zinc-400 text-xs">Prio: ${n.priority || 50}</span>
    </div>
    ${leaseBanner}
    ${renderLifecycle(detail, editable)}
    ${renderVerifications(n, detail.verifications, editable)}
    ${renderDependencies(detail.dependency_details, status, n, editable)}
    ${renderAttachments(n, attachments, editable)}
    ${renderSections(detail.sections, n.id)}
  `;
  attachSectionToggleHandlers(bodyEl);
  attachInspectorGroupToggleHandlers(bodyEl, nodeId);
  wireAttachmentControls(bodyEl, n, attachments, editable);
  if (editable) {
    wireActionBar(bodyEl, n);
    wireLifecycleControls(bodyEl, n);
    wireVerificationControls(bodyEl, n);
    wireDependencyControls(bodyEl, n);
    attachSectionEditControls(bodyEl, n, detail.sections);
  }
}

// A write from inside the inspector (a section edit, a verb, a dependency change...) never
// reloads anything -- its effect lands as a `row`/`body`/`section` item on this same
// subscription, and this is what turns that into a re-render.
window.tmStore.onChange((patch) => {
  if (!inspectorNodeId || graphInspector.classList.contains('hidden')) return;
  if (patch.rowIds.includes(inspectorNodeId) || patch.bodyIds.includes(inspectorNodeId)) {
    renderGraphInspector(inspectorNodeId);
  }
});


// Attachments (§4): a gallery with lightbox, source/age/staleness badges and Re-check, plus
// the Attach-file action and detach -- shared by the graph inspector above and the decisions
// view (decisions.js calls renderAttachments/wireAttachmentControls the same way).

function attachmentAssetUrl(entry) {
  // Live mode always has a server to ask; a static export only has what static_export.py
  // chose to embed (images <=2MB), so anything else stays a name-only, unopenable entry.
  return isStaticMode ? (entry.data_uri || null) : `/assets/${entry.asset}`;
}

function ageFromNow(iso) {
  if (!iso) return 'unknown age';
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms)) return 'unknown age';
  if (ms < 0) return 'just now';
  const mins = Math.floor(ms / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return mins === 1 ? '1 min ago' : `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours === 1 ? '1 hour ago' : `${hours} hours ago`;
  const days = Math.floor(hours / 24);
  return days === 1 ? '1 day ago' : `${days} days ago`;
}

// `size_bytes` is absent on an attachment recorded before the API started reporting it, so
// null/undefined both mean "unknown" and render nothing rather than "NaN B".
function humanBytes(n) {
  if (typeof n !== 'number' || !Number.isFinite(n) || n < 0) return null;
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = n / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value >= 10 ? Math.round(value) : Math.round(value * 10) / 10} ${units[unit]}`;
}

const SOURCE_BADGE = {
  fresh: { cls: 'text-emerald-300 bg-emerald-950/60 border-emerald-800/80', label: 'Fresh' },
  stale: { cls: 'text-amber-300 bg-amber-950/60 border-amber-800/80', label: 'Stale' },
  missing: { cls: 'text-red-300 bg-red-950/60 border-red-800/80', label: 'Missing' },
  unverifiable: { cls: 'text-zinc-400 bg-zinc-900 border-zinc-700', label: 'Unverified' },
};

function sourceBadgeHtml(source) {
  const s = source || {};
  const state = s.state || 'unverifiable';
  const badge = SOURCE_BADGE[state] || SOURCE_BADGE.unverifiable;
  const age = ageFromNow(s.checked_at || s.captured_at);
  const label = state === 'unverifiable' ? `Unverified since ${age}` : `${badge.label} · ${age}`;
  return `<span class="att-source-badge px-1.5 py-0.5 rounded border text-[10px] font-medium ${badge.cls}" title="${esc(s.uri || 'no recorded source')}">${esc(label)}</span>`;
}

function renderAttachments(node, attachments, editable) {
  const list = attachments || [];
  const cards = list.map(entry => {
    const url = attachmentAssetUrl(entry);
    const isImage = (entry.mime || '').startsWith('image/');
    const thumb = isImage && url
      ? `<button type="button" class="att-open-btn block w-full aspect-video rounded-md overflow-hidden bg-zinc-900 border border-zinc-800 hover:border-zinc-600 transition" data-asset="${esc(entry.asset)}" aria-label="Open ${esc(entry.name)} full size"><img src="${esc(url)}" alt="${esc(entry.caption || entry.name)}" class="w-full h-full object-cover"></button>`
      : `<div class="flex items-center justify-center aspect-video rounded-md bg-zinc-900 border border-zinc-800 text-zinc-500">${renderIcon(isImage ? 'file-x' : 'file-text', 'w-6 h-6')}</div>`;
    const nameEl = !isImage && url
      ? `<a href="${esc(url)}" download="${esc(entry.name)}" class="text-emerald-400 hover:text-emerald-300 underline decoration-dotted">${esc(entry.name)}</a>`
      : `<span>${esc(entry.caption || entry.name)}</span>`;
    const sizeLabel = humanBytes(entry.size_bytes);
    const uri = entry.source && entry.source.uri;
    return `
      <div class="att-card space-y-1.5" data-asset="${esc(entry.asset)}">
        ${thumb}
        <div class="flex items-center justify-between gap-1.5 text-[11px] text-zinc-300">
          <span class="truncate min-w-0" title="${esc(entry.name)}">${nameEl}</span>
          <span class="flex items-center gap-1 flex-shrink-0">
            ${sizeLabel ? `<span class="text-zinc-500">${sizeLabel}</span>` : ''}
            ${editable ? `<button type="button" class="att-detach-btn p-1 rounded text-zinc-500 hover:text-red-400 hover:bg-zinc-800" data-asset="${esc(entry.asset)}" aria-label="Detach ${esc(entry.name)}">${renderIcon('x', 'w-3 h-3')}</button>` : ''}
          </span>
        </div>
        ${uri ? `<div class="truncate text-[10px] font-mono text-zinc-500" title="${esc(uri)}">${esc(uri)}</div>` : ''}
        <div class="flex items-center flex-wrap gap-1">${sourceBadgeHtml(entry.source)}</div>
      </div>
    `;
  }).join('');

  const controls = editable ? `
    <div class="flex items-center gap-2 pt-1.5">
      <label tabindex="0" class="att-add-btn h-7 px-2 flex items-center rounded-md text-[11px] font-medium text-emerald-400 hover:text-emerald-300 hover:bg-zinc-800 border border-dashed border-zinc-700 transition cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500">
        <span>+ Attach file</span>
        <input type="file" class="att-file-input hidden" aria-label="Attach a file" tabindex="-1">
      </label>
      ${list.length > 0 ? `<button type="button" class="att-recheck-btn h-7 px-2 rounded-md text-[11px] font-medium text-zinc-300 hover:text-white hover:bg-zinc-800 border border-zinc-700 transition">${renderIcon('rotate-cw', 'w-3 h-3 inline -mt-0.5 mr-1')}Re-check</button>` : ''}
    </div>
  ` : '';

  if (list.length === 0 && !controls) return '';

  return `
    <div class="space-y-1.5 pt-2 border-t border-zinc-800/60">
      <div class="font-semibold text-zinc-400 uppercase tracking-wider text-[10px]">Attachments${list.length ? ` (${list.length})` : ''}</div>
      ${list.length ? `<div class="grid grid-cols-2 gap-2">${cards}</div>` : ''}
      ${controls}
    </div>
  `;
}

function openLightbox(url, alt) {
  const trigger = document.activeElement;
  const overlay = document.createElement('div');
  overlay.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-label', alt || 'Image');
  overlay.innerHTML = `
    <button type="button" class="lb-close absolute top-4 right-4 p-2 rounded text-zinc-300 hover:text-white hover:bg-zinc-800" aria-label="Close image">${renderIcon('x', 'w-5 h-5')}</button>
    <img src="${esc(url)}" alt="${esc(alt || '')}" class="max-w-full max-h-full rounded-lg shadow-2xl">
  `;
  function close() {
    document.removeEventListener('keydown', onKeydown);
    overlay.remove();
    if (trigger && typeof trigger.focus === 'function' && trigger.isConnected) trigger.focus();
  }
  // The close button is the lightbox's only focusable element, so trapping focus is just
  // keeping it there -- Tab used to fall through to the toolbar buttons behind the overlay.
  function onKeydown(e) {
    if (e.key === 'Escape') {
      close();
      return;
    }
    if (e.key === 'Tab') {
      e.preventDefault();
      closeBtn.focus();
    }
  }
  document.addEventListener('keydown', onKeydown);
  overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) close(); });
  const closeBtn = overlay.querySelector('.lb-close');
  closeBtn.addEventListener('click', close);
  dialogRoot.appendChild(overlay);
  closeBtn.focus();
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || '');
      const comma = result.indexOf(',');
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(new Error('Could not read the file.'));
    reader.readAsDataURL(file);
  });
}

async function attachFile(node, file, afterChange) {
  try {
    const content_base64 = await readFileAsBase64(file);
    await api('POST', `/api/nodes/${node.id}/attachments`, { filename: file.name, content_base64 });
    toast(`Attached ${file.name}.`, 'success');
    if (afterChange) await afterChange();
  } catch (e) {
    toast(e.message, 'error');
  }
}

function detachAttachment(node, asset, afterChange, name) {
  confirmDialog({
    title: `Detach ${name || asset}?`,
    message: `The attachment will be removed from ${node.id}.`,
    confirmLabel: 'Detach',
    onConfirm: async () => {
      await api('DELETE', `/api/nodes/${node.id}/attachments/${encodeURIComponent(asset)}`);
      toast('Attachment detached.', 'success');
      if (afterChange) await afterChange();
    }
  });
}

async function recheckAttachments(node, afterChange) {
  try {
    await api('POST', `/api/nodes/${node.id}/attachments/check`);
    toast('Attachment sources re-checked.', 'success');
    if (afterChange) await afterChange();
  } catch (e) {
    toast(e.message, 'error');
  }
}

function wireAttachmentControls(root, node, attachments, editable, afterChange) {
  root.querySelectorAll('.att-open-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const asset = btn.getAttribute('data-asset');
      const entry = (attachments || []).find(a => a.asset === asset);
      const url = entry && attachmentAssetUrl(entry);
      if (url) openLightbox(url, entry.caption || entry.name);
    });
  });
  if (!editable) return;
  const fileInput = root.querySelector('.att-file-input');
  if (fileInput) {
    fileInput.addEventListener('change', async () => {
      const file = fileInput.files && fileInput.files[0];
      if (file) await attachFile(node, file, afterChange);
      fileInput.value = '';
    });
    // The input itself is display:none (tabindex="-1", out of the tab order), so its
    // wrapping <label> is the tab stop -- but a <label> has no native keyboard activation
    // the way a <button> or the mouse's own click-through-label behaviour does.
    const addBtn = root.querySelector('.att-add-btn');
    if (addBtn) {
      addBtn.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          fileInput.click();
        }
      });
    }
  }
  root.querySelectorAll('.att-detach-btn').forEach(btn => {
    const asset = btn.getAttribute('data-asset');
    const entry = (attachments || []).find(a => a.asset === asset);
    btn.addEventListener('click', () => detachAttachment(node, asset, afterChange, entry && entry.name));
  });
  const recheckBtn = root.querySelector('.att-recheck-btn');
  if (recheckBtn) recheckBtn.addEventListener('click', () => recheckAttachments(node, afterChange));
}


// Markdown image rewriting (§4): a section's own `<img>` sources should not expose the
// project's filesystem layout, so anything not already http(s)/data/assets is served
// through /api/file, which only serves an image whose resolved real path sits under the
// project root. Wrapping the global tree.js/edit.js function is the same pattern edit.js
// itself uses for DOMPurify (see the top of edit.js) -- reassigning it here runs both.
// A static export has no server to rewrite toward, so it is left exactly as authored.
if (typeof renderSectionBody === 'function') {
  const previousRenderSectionBody = renderSectionBody;
  renderSectionBody = function (content) {
    const html = previousRenderSectionBody(content);
    if (isStaticMode || typeof DOMParser === 'undefined') return html;
    // DOMParser output is inert: no image request fires and no script runs while we walk it.
    const doc = new DOMParser().parseFromString(html, 'text/html');
    doc.querySelectorAll('img[src]').forEach(img => {
      const src = img.getAttribute('src');
      if (src && !/^(https?:|data:)/i.test(src) && !src.startsWith('/assets/')) {
        img.setAttribute('src', `/api/file?path=${encodeURIComponent(src)}`);
      }
    });
    return doc.body.innerHTML;
  };
}


// AWAITING_DECISION banner (§6.4) in the document view: renderUnifiedDocument is tree.js's
// own function, wrapped rather than edited there -- same reassignment pattern as above, so
// the document view's own markup never has to know decisions exist.

function decorateAwaitingDecisionBanners() {
  // Rows are flat (no `.children`), and dependency_details lives on a watched body, not the
  // row itself -- a task not yet expanded has no body to read the banner's links from, and
  // it reappears here on its own once opening the card starts watching it.
  window.tmStore.rows.forEach(task => {
    if (task.kind !== 'task' || displayOf(task) !== 'AWAITING_DECISION') return;
    const el = document.getElementById(`doc-node-${task.id}`);
    const cardBody = el && el.querySelector('.task-body');
    if (!cardBody || cardBody.querySelector('.awaiting-decision-banner')) return;
    const body = window.tmStore.bodies.get(task.id);
    const waitingOn = ((body && body.dependency_details) || []).filter(d => !d.finished);
    const links = waitingOn.map(d =>
      `<button type="button" class="awaiting-decision-link underline decoration-dotted text-amber-200 hover:text-amber-100" data-decision-id="${esc(d.id)}">${esc(d.id)}${d.title ? `: ${esc(d.title)}` : ''}</button>`
    ).join(', ');
    const banner = document.createElement('div');
    banner.className = 'awaiting-decision-banner p-2.5 bg-amber-950/40 border border-amber-800/80 rounded-lg flex items-center gap-2 text-xs mb-3';
    banner.innerHTML = `${renderIcon('help-circle', 'w-3.5 h-3.5 text-amber-400 flex-shrink-0')}<span class="text-amber-200">Awaiting decision: ${links || 'unknown'}</span>`;
    cardBody.insertBefore(banner, cardBody.firstChild);
  });
}

document.addEventListener('click', (e) => {
  const link = e.target.closest('.awaiting-decision-link');
  if (!link) return;
  e.preventDefault();
  if (typeof goToDecision === 'function') goToDecision(link.getAttribute('data-decision-id'));
});

if (typeof renderUnifiedDocument === 'function') {
  const previousRenderUnifiedDocument = renderUnifiedDocument;
  renderUnifiedDocument = function () {
    previousRenderUnifiedDocument();
    decorateAwaitingDecisionBanners();
  };
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
      renderGraphInspector(nodeId);
    };
  });
}
