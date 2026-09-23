// Editing: a dialog primitive, the toolbar's + New menu, and every write-side form and
// action the detail panel wires buttons to. Nothing here renders unless canEdit() -- a
// static export never even inlines these DOM ids' listeners because renderNewMenu() and
// every call site in detail.js check it first.

// The section preview (marked.parse) is the only place user-authored markdown becomes
// DOM; sections render everywhere via tree.js's renderSectionBody, so that function is
// wrapped here rather than duplicated. Reassigning a global `function` declared in an
// earlier <script> block works: script tags share one global scope, executed in order.
if (typeof DOMPurify !== 'undefined' && typeof renderSectionBody === 'function') {
  const unsafeRenderSectionBody = renderSectionBody;
  renderSectionBody = function (content) {
    return DOMPurify.sanitize(unsafeRenderSectionBody(content));
  };
}

// Only the statuses a node can actually be set to (NodeStatus). window.STATUS_THEMES also
// carries the virtual ones (READY, BLOCKED, BLOCKED_BY_LEASE, IN_FLIGHT) a write would 422 on.
// NOT_STARTED is left out (§3.2a: the web never displays it) -- Reopen is that transition's
// own button, and a dependency gated on it would be satisfied by every node immediately.
const REAL_NODE_STATUSES = [
  'IMPLEMENTING', 'WAITING_REVIEW', 'REVIEWING', 'WAITING_FIXES', 'FIXING',
  'WAITING_MERGE', 'MERGING', 'COMPLETED', 'SUPERSEDED', 'ABANDONED', 'DEFERRED'
];

const INPUT_CLS = 'w-full h-8 px-2.5 rounded-lg bg-zinc-950 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500';
const SELECT_CLS = `${INPUT_CLS} appearance-none`;
const TEXTAREA_CLS = 'w-full px-2.5 py-2 rounded-lg bg-zinc-950 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 font-mono leading-relaxed focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500';

function fieldRow(labelText, innerHtml) {
  return `
    <label class="block space-y-1">
      ${labelText ? `<span class="block text-[11px] font-semibold text-zinc-400 uppercase tracking-wider">${esc(labelText)}</span>` : ''}
      ${innerHtml}
    </label>
  `;
}


// Dialog primitive -----------------------------------------------------------------------
// Focus trap (Tab/Shift+Tab wraps inside the panel), Esc closes, focus returns to whatever
// triggered it. onSubmit throwing shows the error as a toast rather than closing the dialog,
// so a refusal (400/404/409 from OperationError) leaves the form open to correct and retry.

function openDialog({ title, bodyHtml, onMount, onSubmit, submitLabel = 'Save', cancelLabel = 'Cancel', destructive = false }) {
  const trigger = document.activeElement;
  const overlay = document.createElement('div');
  overlay.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4';

  const panel = document.createElement('div');
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  panel.setAttribute('aria-labelledby', 'dlg-title');
  panel.className = 'w-full max-w-md rounded-xl border border-zinc-700 bg-zinc-900 shadow-2xl p-4 space-y-4 max-h-[85vh] overflow-y-auto';
  panel.innerHTML = `
    <div class="flex items-center justify-between gap-2">
      <h2 id="dlg-title" class="text-sm font-semibold text-zinc-100">${esc(title)}</h2>
      <button type="button" class="dlg-close p-1 rounded text-zinc-400 hover:text-white hover:bg-zinc-800" aria-label="Close dialog">${renderIcon('x', 'w-4 h-4')}</button>
    </div>
    <form class="dlg-form space-y-3" novalidate>
      ${bodyHtml}
      <p class="dlg-error hidden text-xs text-red-400"></p>
      <div class="flex justify-end gap-2 pt-1">
        <button type="button" class="dlg-cancel h-8 px-3 rounded-lg text-xs text-zinc-300 hover:text-white hover:bg-zinc-800 transition">${esc(cancelLabel)}</button>
        <button type="submit" class="dlg-submit h-8 px-3 rounded-lg text-xs font-semibold transition ${destructive ? 'bg-red-600 hover:bg-red-500 text-white' : 'bg-emerald-600 hover:bg-emerald-500 text-black'}">${esc(submitLabel)}</button>
      </div>
    </form>
  `;
  overlay.appendChild(panel);
  dialogRoot.appendChild(overlay);

  function focusables() {
    return Array.from(panel.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'))
      .filter(el => !el.disabled && el.getClientRects().length > 0);
  }

  function close() {
    document.removeEventListener('keydown', onKeydown);
    overlay.remove();
    if (trigger && typeof trigger.focus === 'function' && trigger.isConnected) trigger.focus();
  }

  function onKeydown(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
      return;
    }
    if (e.key === 'Tab') {
      const els = focusables();
      if (els.length === 0) return;
      const first = els[0];
      const last = els[els.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  }
  document.addEventListener('keydown', onKeydown);

  panel.querySelector('.dlg-close').addEventListener('click', close);
  panel.querySelector('.dlg-cancel').addEventListener('click', close);
  overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) close(); });

  if (onMount) onMount(panel);

  const form = panel.querySelector('.dlg-form');
  const errorEl = panel.querySelector('.dlg-error');
  const submitBtn = panel.querySelector('.dlg-submit');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errorEl.classList.add('hidden');
    submitBtn.disabled = true;
    try {
      await onSubmit(panel, close);
    } catch (err) {
      errorEl.textContent = err && err.message ? err.message : 'Request failed.';
      errorEl.classList.remove('hidden');
    } finally {
      submitBtn.disabled = false;
    }
  });

  (focusables()[0] || panel).focus();
  return { panel, close };
}

// A confirm is just a dialog whose only field is the warning text -- and, unlike an editable
// form, there is nothing in it left to correct on a refusal, so it closes as soon as the
// button is pressed rather than staying open for the write plus the reload that follows it.
// `onConfirm` routinely ends in `await afterWrite(...)`, which reloads the whole tree/graph
// (seconds on a large estate); leaving the dialog open for that made a Withdraw or a Remove
// look hung for as long as the reload took, disabled button and all.
function confirmDialog({ title, message, confirmLabel = 'Confirm', destructive = true, onConfirm }) {
  return openDialog({
    title,
    submitLabel: confirmLabel,
    destructive,
    bodyHtml: `<p class="text-xs text-zinc-300 leading-relaxed">${esc(message)}</p>`,
    onSubmit: async (panel, close) => {
      close();
      try {
        await onConfirm();
      } catch (err) {
        toast(err && err.message ? err.message : 'Request failed.', 'error');
      }
    }
  });
}

// Reload the tree/graph/stats and, if the node just written to is the one open in the
// inspector, re-render it -- the WS ledger watcher would do this too, ~0.7s later.
async function afterWrite(nodeId) {
  await loadAllData();
  if (nodeId && selectedNodeId === nodeId && graphInspector && !graphInspector.classList.contains('hidden')) {
    showGraphInspector(nodeId);
  }
}


// Frontmatter editor -----------------------------------------------------------------------
// declared_files (and any array-of-strings value) edits as one path per line. A value that is
// an object, or an array holding anything but strings (attachments, decision, ...), edits as
// pretty-printed JSON instead -- `[value].join('\n')` on an array of objects stringifies each
// element to the literal text "[object Object]", so treating every array as a path list lost
// that data on save. Anything else edits as a single field, parsed back through JSON.parse so
// a number/bool round-trips, and falls back to the raw string when it doesn't parse as JSON.

function frontmatterValueKind(key, value) {
  if (key === 'declared_files' || (Array.isArray(value) && value.every(v => typeof v === 'string'))) {
    return 'list';
  }
  if (Array.isArray(value) || (value !== null && typeof value === 'object')) return 'json';
  return 'scalar';
}

function frontmatterRowHtml(key, value) {
  const kind = frontmatterValueKind(key, value);
  const valueText = kind === 'list'
    ? (Array.isArray(value) ? value.join('\n') : String(value ?? ''))
    : kind === 'json'
      ? JSON.stringify(value, null, 2)
      : (typeof value === 'string' ? value : JSON.stringify(value ?? ''));
  const valueField = kind === 'scalar'
    ? `<input type="text" class="fm-value ${INPUT_CLS} font-mono" value="${esc(valueText)}">`
    : `<textarea rows="${kind === 'json' ? 6 : 3}" class="fm-value ${TEXTAREA_CLS}" data-fm-kind="${kind}" placeholder="${kind === 'json' ? 'JSON' : 'one path per line'}">${esc(valueText)}</textarea>`;
  return `
    <div class="fm-row flex items-start gap-2">
      <div class="flex-1 space-y-1 min-w-0">
        <input type="text" class="fm-key ${INPUT_CLS} font-mono" value="${esc(key)}" placeholder="key">
        ${valueField}
      </div>
      <label class="flex items-center gap-1 text-[10px] text-zinc-400 pt-1.5 flex-shrink-0">
        <input type="checkbox" class="fm-remove rounded border-zinc-600 bg-zinc-950">remove
      </label>
    </div>
  `;
}

function frontmatterEditorHtml(frontmatter) {
  const rows = Object.entries(frontmatter || {}).map(([k, v]) => frontmatterRowHtml(k, v)).join('');
  return `
    <div class="space-y-2 pt-1">
      <div class="flex items-center justify-between">
        <span class="text-[11px] font-semibold text-zinc-400 uppercase tracking-wider">Frontmatter</span>
        <button type="button" class="fm-add text-[11px] text-emerald-400 hover:text-emerald-300">+ Add field</button>
      </div>
      <div class="fm-rows space-y-2">${rows}</div>
    </div>
  `;
}

function readFrontmatterEditor(panel) {
  const set = {};
  const unset = [];
  panel.querySelectorAll('.fm-row').forEach(row => {
    const key = row.querySelector('.fm-key').value.trim();
    if (!key) return;
    if (row.querySelector('.fm-remove').checked) {
      unset.push(key);
      return;
    }
    const valueEl = row.querySelector('.fm-value');
    const kind = valueEl.tagName === 'TEXTAREA' ? valueEl.dataset.fmKind : 'scalar';
    if (kind === 'list') {
      set[key] = valueEl.value.split('\n').map(s => s.trim()).filter(Boolean);
    } else if (kind === 'json') {
      try {
        set[key] = JSON.parse(valueEl.value);
      } catch (e) {
        throw new Error(`"${key}" is not valid JSON.`);
      }
    } else {
      const raw = valueEl.value;
      try {
        set[key] = JSON.parse(raw);
      } catch (e) {
        set[key] = raw;
      }
    }
  });
  return { set, unset };
}


// Node create/edit -------------------------------------------------------------------------

function openNewSpecDialog() {
  openDialog({
    title: 'New spec',
    submitLabel: 'Create',
    bodyHtml: `
      ${fieldRow('Title', `<input type="text" required class="ns-title ${INPUT_CLS}" placeholder="Spec title">`)}
      ${fieldRow('Slug', `<input type="text" class="ns-slug ${INPUT_CLS}" placeholder="(derived from title)">`)}
      ${fieldRow('Priority', `<input type="number" min="1" max="100" class="ns-priority ${INPUT_CLS}" value="50">`)}
    `,
    onSubmit: async (panel, close) => {
      const title = panel.querySelector('.ns-title').value.trim();
      if (!title) throw new Error('Title is required.');
      const res = await api('POST', '/api/specs', {
        title,
        slug: panel.querySelector('.ns-slug').value.trim() || undefined,
        priority: Number(panel.querySelector('.ns-priority').value) || 50,
      });
      toast(`Spec ${res.id} created.`, 'success');
      close();
      await afterWrite(res.id);
    }
  });
}

async function openNewPlanDialog() {
  let meta;
  try {
    meta = await api('GET', '/api/meta');
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  if (meta.specs.length === 0) {
    toast('No specs exist yet -- create one first.', 'error');
    return;
  }
  openDialog({
    title: 'New plan',
    submitLabel: 'Create',
    bodyHtml: `
      ${fieldRow('Title', `<input type="text" required class="np-title ${INPUT_CLS}" placeholder="Plan title">`)}
      ${fieldRow('Spec', `<select class="np-spec ${SELECT_CLS}">${meta.specs.map(s => `<option value="${esc(s.id)}">${esc(s.id)} -- ${esc(s.title)}</option>`).join('')}</select>`)}
      ${fieldRow('Slug', `<input type="text" class="np-slug ${INPUT_CLS}" placeholder="(derived from title)">`)}
      <div class="grid grid-cols-2 gap-2">
        ${fieldRow('Priority', `<input type="number" min="1" max="100" class="np-priority ${INPUT_CLS}" value="50">`)}
        ${fieldRow('Order', `<input type="number" class="np-order ${INPUT_CLS}" value="0">`)}
      </div>
      <label class="flex items-center gap-2 text-xs text-zinc-300"><input type="checkbox" class="np-review rounded border-zinc-600 bg-zinc-950 text-emerald-500 focus:ring-emerald-500">Require review</label>
    `,
    onSubmit: async (panel, close) => {
      const title = panel.querySelector('.np-title').value.trim();
      if (!title) throw new Error('Title is required.');
      const res = await api('POST', '/api/plans', {
        title,
        spec: panel.querySelector('.np-spec').value,
        slug: panel.querySelector('.np-slug').value.trim() || undefined,
        priority: Number(panel.querySelector('.np-priority').value) || 50,
        order: Number(panel.querySelector('.np-order').value) || 0,
        require_review: panel.querySelector('.np-review').checked,
      });
      toast(`Plan ${res.id} created.`, 'success');
      close();
      await afterWrite(res.id);
    }
  });
}

async function openNewTaskDialog() {
  let meta;
  try {
    meta = await api('GET', '/api/meta');
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  if (meta.plans.length === 0) {
    toast('No plans exist yet -- create one first.', 'error');
    return;
  }
  openDialog({
    title: 'New task',
    submitLabel: 'Create',
    bodyHtml: `
      ${fieldRow('Title', `<input type="text" required class="nt-title ${INPUT_CLS}" placeholder="Task title">`)}
      ${fieldRow('Plan', `<select class="nt-plan ${SELECT_CLS}">${meta.plans.map(p => `<option value="${esc(p.id)}">${esc(p.id)} -- ${esc(p.title)}</option>`).join('')}</select>`)}
      ${fieldRow('Slug', `<input type="text" class="nt-slug ${INPUT_CLS}" placeholder="(derived from title)">`)}
      <div class="grid grid-cols-2 gap-2">
        ${fieldRow('Priority', `<input type="number" min="1" max="100" class="nt-priority ${INPUT_CLS}" value="50">`)}
        ${fieldRow('Order', `<input type="number" class="nt-order ${INPUT_CLS}" value="0">`)}
      </div>
      ${fieldRow('Depends on (comma separated ids)', `<input type="text" class="nt-deps ${INPUT_CLS}" placeholder="(optional)">`)}
      ${fieldRow('Acceptable models (comma separated)', `<input type="text" class="nt-models ${INPUT_CLS}" list="nt-models-list" placeholder="(optional)">`)}
      <datalist id="nt-models-list">${meta.models.map(m => `<option value="${esc(m)}">`).join('')}</datalist>
    `,
    onSubmit: async (panel, close) => {
      const title = panel.querySelector('.nt-title').value.trim();
      if (!title) throw new Error('Title is required.');
      const res = await api('POST', '/api/tasks', {
        title,
        plan: panel.querySelector('.nt-plan').value,
        slug: panel.querySelector('.nt-slug').value.trim() || undefined,
        priority: Number(panel.querySelector('.nt-priority').value) || 50,
        order: Number(panel.querySelector('.nt-order').value) || 0,
        depends_on: panel.querySelector('.nt-deps').value.split(',').map(s => s.trim()).filter(Boolean),
        models: panel.querySelector('.nt-models').value.split(',').map(s => s.trim()).filter(Boolean),
      });
      toast(`Task ${res.id} created.`, 'success');
      close();
      await afterWrite(res.id);
    }
  });
}

function openEditNodeDialog(node) {
  const isTask = node.kind === 'task';
  openDialog({
    title: `Edit ${node.id}`,
    submitLabel: 'Save',
    bodyHtml: `
      ${fieldRow('Title', `<input type="text" required class="ed-title ${INPUT_CLS}" value="${esc(node.title)}">`)}
      ${fieldRow('Priority', `<input type="number" min="1" max="100" class="ed-priority ${INPUT_CLS}" value="${node.priority || 50}">`)}
      ${isTask ? fieldRow('Acceptable models (comma separated)', `<input type="text" class="ed-models ${INPUT_CLS}" value="${esc((node.acceptable_models || []).join(', '))}">`) : ''}
      ${isTask ? fieldRow('Target repo', `<input type="text" class="ed-repo ${INPUT_CLS}" value="${esc(node.target_repo || '')}">`) : ''}
      ${isTask ? frontmatterEditorHtml(node.frontmatter) : ''}
    `,
    onMount: (panel) => {
      const addBtn = panel.querySelector('.fm-add');
      if (addBtn) {
        addBtn.addEventListener('click', () => {
          panel.querySelector('.fm-rows').insertAdjacentHTML('beforeend', frontmatterRowHtml('', ''));
        });
      }
    },
    onSubmit: async (panel, close) => {
      const title = panel.querySelector('.ed-title').value.trim();
      if (!title) throw new Error('Title is required.');
      const body = { title, priority: Number(panel.querySelector('.ed-priority').value) || 50 };
      const modelsEl = panel.querySelector('.ed-models');
      if (modelsEl) body.acceptable_models = modelsEl.value.split(',').map(s => s.trim()).filter(Boolean);
      const repoEl = panel.querySelector('.ed-repo');
      if (repoEl) body.target_repo = repoEl.value.trim() || null;
      if (isTask) {
        const { set, unset } = readFrontmatterEditor(panel);
        if (Object.keys(set).length) body.frontmatter_set = set;
        if (unset.length) body.frontmatter_unset = unset;
      }
      await api('PATCH', `/api/nodes/${node.id}`, body);
      toast(`${node.id} updated.`, 'success');
      close();
      await afterWrite(node.id);
    }
  });
}


// Status ---------------------------------------------------------------------------------

async function setNodeStatus(node, status, removeWorktree = false) {
  await api('POST', `/api/nodes/${node.id}/status`, { status, remove_worktree: removeWorktree });
  toast(`${node.id} is now ${status}.`, 'success');
  await afterWrite(node.id);
}

function changeStatus(node, status) {
  if (status === 'ABANDONED') {
    confirmDialog({
      title: `Abandon ${node.id}?`,
      message: `"${node.title}" will be marked ABANDONED and dropped for good.`,
      confirmLabel: 'Abandon',
      onConfirm: () => setNodeStatus(node, status),
    });
    return;
  }
  setNodeStatus(node, status).catch(e => toast(e.message, 'error'));
}

function openOtherStatusDialog(node, currentStatus) {
  openDialog({
    title: `Change status of ${node.id}`,
    submitLabel: 'Set status',
    bodyHtml: `
      ${fieldRow('Status', `<select class="st-select ${SELECT_CLS}">${REAL_NODE_STATUSES.map(s => `<option value="${s}" ${s === currentStatus ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select>`)}
      ${node.kind === 'task' ? `<label class="flex items-center gap-2 text-xs text-zinc-300"><input type="checkbox" class="rm-worktree rounded border-zinc-600 bg-zinc-950">Remove worktree</label>` : ''}
    `,
    onSubmit: async (panel, close) => {
      const status = panel.querySelector('.st-select').value;
      const removeWt = panel.querySelector('.rm-worktree');
      await setNodeStatus(node, status, removeWt ? removeWt.checked : false);
      close();
    }
  });
}


// Supersede / move --------------------------------------------------------------------------

function openSupersedeDialog(node) {
  openDialog({
    title: `Supersede ${node.id}`,
    submitLabel: 'Supersede',
    destructive: true,
    bodyHtml: `
      ${fieldRow('Superseded by (task id)', `<input type="text" required class="sup-by ${INPUT_CLS} font-mono" placeholder="task-id">`)}
      ${fieldRow('Transfer blocked dependents', `
        <select class="sup-mode ${SELECT_CLS}">
          <option value="all">All to the new task</option>
          <option value="none">None</option>
          <option value="custom">Custom ids</option>
        </select>`)}
      <div class="sup-custom-wrap hidden">
        ${fieldRow('Ids (comma separated)', `<input type="text" class="sup-custom ${INPUT_CLS} font-mono" placeholder="t1, t2">`)}
      </div>
    `,
    onMount: (panel) => {
      panel.querySelector('.sup-mode').addEventListener('change', (e) => {
        panel.querySelector('.sup-custom-wrap').classList.toggle('hidden', e.target.value !== 'custom');
      });
    },
    onSubmit: async (panel, close) => {
      const by = panel.querySelector('.sup-by').value.trim();
      if (!by) throw new Error('Superseded-by id is required.');
      const mode = panel.querySelector('.sup-mode').value;
      const transfer_blocks = mode === 'custom' ? panel.querySelector('.sup-custom').value : mode;
      await api('POST', `/api/nodes/${node.id}/supersede`, { by, transfer_blocks });
      toast(`${node.id} superseded by ${by}.`, 'success');
      close();
      await afterWrite(node.id);
    }
  });
}

async function openMoveDialog(node) {
  let meta;
  try {
    meta = await api('GET', '/api/meta');
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  if (meta.plans.length === 0) {
    toast('No plans exist to move into.', 'error');
    return;
  }
  openDialog({
    title: `Move ${node.id} to plan`,
    submitLabel: 'Move',
    bodyHtml: fieldRow('Plan', `<select class="mv-plan ${SELECT_CLS}">${meta.plans.map(p => `<option value="${esc(p.id)}">${esc(p.id)} -- ${esc(p.title)}</option>`).join('')}</select>`),
    onSubmit: async (panel, close) => {
      const plan = panel.querySelector('.mv-plan').value;
      await api('POST', `/api/nodes/${node.id}/move`, { plan });
      toast(`${node.id} moved to ${plan}.`, 'success');
      close();
      await afterWrite(node.id);
    }
  });
}


// Dependencies -----------------------------------------------------------------------------

// A search picker over ids and titles (§6.3): a native <datalist> is the whole
// implementation, so typing "which auth" resolves the same as typing "decision-which".
// `decisionsOnly` is "Wait on decision" -- the same picker, filtered to decisions, and the
// same POST (a decision dependency is an ordinary depends_on edge, per §3.2).
function openAddDependencyDialog(node, decisionsOnly = false) {
  const candidates = decisionsOnly
    ? decisionsData.filter(d => d.id !== node.id)
    : collectAllNodes(treeData).filter(n => n.kind !== 'decision' && n.id !== node.id);
  const listId = 'dep-picker-list';
  const optionsHtml = candidates.map(n => `<option value="${esc(n.id)}">${esc(n.title)}</option>`).join('');
  openDialog({
    title: decisionsOnly ? `Wait on decision (${node.id})` : `Add dependency to ${node.id}`,
    submitLabel: 'Add',
    bodyHtml: `
      ${fieldRow(decisionsOnly ? 'Decision (id or title)' : 'Depends on (id or title)', `<input type="text" required list="${listId}" class="dep-id ${INPUT_CLS} font-mono" placeholder="${decisionsOnly ? 'decision-id' : 'task-id'}"><datalist id="${listId}">${optionsHtml}</datalist>`)}
      ${decisionsOnly ? '' : fieldRow('Gate status', `<select class="dep-gate ${SELECT_CLS}">${REAL_NODE_STATUSES.map(s => `<option value="${s}" ${s === 'COMPLETED' ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select>`)}
    `,
    onSubmit: async (panel, close) => {
      const typed = panel.querySelector('.dep-id').value.trim();
      if (!typed) throw new Error('Dependency id is required.');
      // The datalist's <option value> is the id; a typed title resolves back to its id so
      // picking "Which auth flow?" from the list works the same as typing the id directly.
      const match = candidates.find(n => n.id === typed || n.title === typed);
      const id = match ? match.id : typed;
      const gateEl = panel.querySelector('.dep-gate');
      const gate = gateEl ? gateEl.value : undefined;
      await api('POST', `/api/nodes/${node.id}/dependencies`, { add: [{ id, gate }] });
      toast(`${id} added as a dependency of ${node.id}.`, 'success');
      close();
      await afterWrite(node.id);
    }
  });
}

function removeDependency(node, depId) {
  confirmDialog({
    title: `Remove dependency ${depId}?`,
    message: `${node.id} will no longer depend on ${depId}.`,
    confirmLabel: 'Remove',
    onConfirm: async () => {
      await api('POST', `/api/nodes/${node.id}/dependencies`, { remove: [depId] });
      toast(`${depId} removed from ${node.id}'s dependencies.`, 'success');
      await afterWrite(node.id);
    }
  });
}


// Sections -----------------------------------------------------------------------------------

function openSectionDialog(node, existing) {
  const isEdit = !!existing;
  openDialog({
    title: isEdit ? `Edit section ${existing.key}` : `Add section to ${node.id}`,
    submitLabel: isEdit ? 'Save' : 'Add',
    bodyHtml: `
      ${isEdit ? '' : fieldRow('Key', `<input type="text" required class="sec-key ${INPUT_CLS} font-mono" placeholder="context">`)}
      ${fieldRow('Header', `<input type="text" class="sec-header ${INPUT_CLS}" placeholder="(optional)" value="${esc(existing ? existing.header || '' : '')}">`)}
      <div class="space-y-1.5">
        <div class="flex items-center gap-1 border-b border-zinc-800">
          <button type="button" class="sec-tab-write px-2 py-1 text-[11px] font-medium text-white border-b-2 border-emerald-500">Write</button>
          <button type="button" class="sec-tab-preview px-2 py-1 text-[11px] font-medium text-zinc-400 border-b-2 border-transparent hover:text-white">Preview</button>
        </div>
        <textarea class="sec-content ${TEXTAREA_CLS}" rows="8" placeholder="Markdown content">${esc(existing ? existing.content || '' : '')}</textarea>
        <div class="sec-preview hidden prose prose-invert max-w-none text-xs leading-relaxed text-zinc-400 border border-zinc-800 rounded-lg p-2 min-h-[8rem]"></div>
      </div>
    `,
    onMount: (panel) => {
      const writeTab = panel.querySelector('.sec-tab-write');
      const previewTab = panel.querySelector('.sec-tab-preview');
      const content = panel.querySelector('.sec-content');
      const preview = panel.querySelector('.sec-preview');
      const ACTIVE = 'px-2 py-1 text-[11px] font-medium text-white border-b-2 border-emerald-500';
      const INACTIVE = 'px-2 py-1 text-[11px] font-medium text-zinc-400 border-b-2 border-transparent hover:text-white';
      previewTab.addEventListener('click', () => {
        preview.innerHTML = renderSectionBody(content.value);
        content.classList.add('hidden');
        preview.classList.remove('hidden');
        previewTab.className = ACTIVE;
        writeTab.className = INACTIVE;
      });
      writeTab.addEventListener('click', () => {
        content.classList.remove('hidden');
        preview.classList.add('hidden');
        writeTab.className = ACTIVE;
        previewTab.className = INACTIVE;
      });
    },
    onSubmit: async (panel, close) => {
      const key = isEdit ? existing.key : panel.querySelector('.sec-key').value.trim();
      if (!key) throw new Error('Section key is required.');
      const header = panel.querySelector('.sec-header').value.trim() || null;
      const content = panel.querySelector('.sec-content').value;
      await api('PUT', `/api/nodes/${node.id}/sections/${encodeURIComponent(key)}`, { content, header });
      toast(`Section ${key} saved.`, 'success');
      close();
      await afterWrite(node.id);
    }
  });
}

function removeSection(node, key) {
  confirmDialog({
    title: `Delete section ${key}?`,
    message: `The "${key}" section on ${node.id} will be removed.`,
    confirmLabel: 'Delete',
    onConfirm: async () => {
      await api('DELETE', `/api/nodes/${node.id}/sections/${encodeURIComponent(key)}`);
      toast(`Section ${key} deleted.`, 'success');
      await afterWrite(node.id);
    }
  });
}


// Verifications --------------------------------------------------------------------------

function openAddVerificationDialog(node) {
  const types = Object.keys(typeof VERIFICATION_LABEL !== 'undefined' ? VERIFICATION_LABEL : {});
  openDialog({
    title: `Add verification to ${node.id}`,
    submitLabel: 'Add',
    bodyHtml: `
      ${fieldRow('Type', `<select class="ver-type ${SELECT_CLS}">${types.map(t => `<option value="${esc(t)}">${esc(VERIFICATION_LABEL[t] || t)}</option>`).join('')}</select>`)}
      ${fieldRow('Target path', `<input type="text" required class="ver-target ${INPUT_CLS} font-mono" placeholder="path/or/symbol">`)}
      ${fieldRow('Expected pattern', `<input type="text" class="ver-pattern ${INPUT_CLS} font-mono" placeholder="(optional)">`)}
    `,
    onSubmit: async (panel, close) => {
      const target_path = panel.querySelector('.ver-target').value.trim();
      if (!target_path) throw new Error('Target path is required.');
      await api('POST', `/api/nodes/${node.id}/verifications`, {
        type: panel.querySelector('.ver-type').value,
        target_path,
        expected_pattern: panel.querySelector('.ver-pattern').value.trim() || null,
      });
      toast('Verification added.', 'success');
      close();
      await afterWrite(node.id);
    }
  });
}

function removeVerification(node, verificationId, target) {
  confirmDialog({
    title: `Remove verification?`,
    message: `The ${target ? `"${target}" ` : ''}verification on ${node.id} will be removed.`,
    confirmLabel: 'Remove',
    onConfirm: async () => {
      await api('DELETE', `/api/nodes/${node.id}/verifications/${verificationId}`);
      toast('Verification removed.', 'success');
      await afterWrite(node.id);
    }
  });
}


// Lease ------------------------------------------------------------------------------------

function releaseLease(node) {
  confirmDialog({
    title: `Release lease on ${node.id}?`,
    message: 'The active lease and its file locks are dropped. Status is unchanged.',
    confirmLabel: 'Release',
    onConfirm: async () => {
      await api('DELETE', `/api/nodes/${node.id}/lease`);
      toast(`Lease released on ${node.id}.`, 'success');
      await afterWrite(node.id);
    }
  });
}


// Toolbar "+ New" menu -----------------------------------------------------------------------

function renderNewMenu() {
  if (!canEdit()) return;
  toolbarActions.innerHTML = `
    <div class="relative">
      <button id="new-menu-btn" type="button" aria-haspopup="menu" aria-expanded="false" class="h-8 px-3 flex items-center gap-1 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-black text-xs font-semibold transition">
        <span aria-hidden="true">+</span><span>New</span>
      </button>
      <div id="new-menu-pop" role="menu" class="hidden absolute right-0 z-30 mt-1 w-36 rounded-lg border border-zinc-700 bg-zinc-900 shadow-2xl p-1">
        <button type="button" role="menuitem" data-new-kind="spec" class="w-full text-left px-2 py-1.5 rounded text-xs text-zinc-200 hover:bg-zinc-800">Spec</button>
        <button type="button" role="menuitem" data-new-kind="plan" class="w-full text-left px-2 py-1.5 rounded text-xs text-zinc-200 hover:bg-zinc-800">Plan</button>
        <button type="button" role="menuitem" data-new-kind="task" class="w-full text-left px-2 py-1.5 rounded text-xs text-zinc-200 hover:bg-zinc-800">Task</button>
      </div>
    </div>
  `;
  const btn = document.getElementById('new-menu-btn');
  const pop = document.getElementById('new-menu-pop');
  let open = false;
  function setOpen(v) {
    open = v;
    pop.classList.toggle('hidden', !open);
    btn.setAttribute('aria-expanded', String(open));
    // The button sits near the right edge of the toolbar, so a right-0 anchor at 375px
    // overflowed the popup off-screen to the left (x as low as -66 measured); clamp it back
    // into the viewport the same way the filter popovers do.
    if (open) clampToViewport(pop);
  }
  btn.addEventListener('click', (e) => { e.stopPropagation(); setOpen(!open); });
  btn.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') setOpen(false);
  });
  document.addEventListener('click', (e) => {
    if (open && !btn.parentElement.contains(e.target)) setOpen(false);
  });
  pop.querySelectorAll('[data-new-kind]').forEach(item => {
    item.addEventListener('click', () => {
      setOpen(false);
      const kind = item.getAttribute('data-new-kind');
      if (kind === 'spec') openNewSpecDialog();
      else if (kind === 'plan') openNewPlanDialog();
      else if (kind === 'task') openNewTaskDialog();
    });
  });
}

renderNewMenu();
