// A node's details render through nodeDetailHtml on both surfaces, the expanded Document card
// and the drawer (opened from Graph, Waves and decision links): a facts strip, then the groups
// in one order, each drawn only when it has rows, with the same defaults on both.

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

function isBlockedDisplay(code) {
  return code === 'AWAITING_DECISION' || String(code || '').startsWith('BLOCKED_');
}

// The row's display when the row is loaded; a node opened without its row (a decision link's
// target, a deep link) reads the body's own, never its stored status.
function detailStatus(node, body, row) {
  if (row) return displayOf(row);
  return (body && body.display) || node.status;
}

// The branches a node builds on, nearest first: "tm/P → main".
function landsText(node) {
  return (node.base_chain || []).map(id => (id === 'MAIN' ? 'main' : `tm/${id}`)).join(' → ');
}

// A value longer than the strip (a review verdict) wraps inside it instead of running past the card.
function factHtml(label, valueHtml) {
  return `<span class="fact inline-flex items-center gap-1 min-w-0 max-w-full"><span class="flex-shrink-0 whitespace-nowrap text-zinc-400">${esc(label)}</span>${valueHtml}</span>`;
}

function factText(value) {
  return `<span class="min-w-0 break-words font-mono text-zinc-200">${esc(value)}</span>`;
}

const FACT_COUNTERS = [['Reviews', 'review_cycles'], ['Merge attempts', 'merge_attempts'], ['Step failures', 'step_failures']];

// The card opens its strip with the Lease fact; the drawer carries the lease badge on its pills
// line instead.
function factsStripHtml(node, lease, surface) {
  const facts = [];
  if (surface === 'card' && lease) facts.push(factHtml('Lease', leaseBadge(lease)));
  const lands = landsText(node);
  if (lands) facts.push(factHtml('Lands', factText(lands)));
  facts.push(factHtml('Review', factText(node.review ? 'on' : 'off')));
  facts.push(factHtml('Fix', factText(node.fix ? 'on' : 'off')));
  if (node.outcome) facts.push(factHtml('Outcome', factText(node.outcome)));
  if (node.verdict) facts.push(factHtml('Verdict', factText(node.verdict)));
  FACT_COUNTERS.forEach(([label, key]) => {
    if (node[key]) facts.push(factHtml(label, factText(node[key])));
  });
  if ((node.requires || []).length) facts.push(factHtml('Requires', factText(node.requires.join(', '))));
  if ((node.land_order || []).length) facts.push(factHtml('Land order', factText(node.land_order.join(' → '))));
  return `<div class="facts flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] leading-4">${facts.join('')}</div>`;
}

function detailGroupHtml(ownerId, key, label, count, defaultCollapsed, innerHtml, meta = '') {
  if (!count) return '';
  const groupId = `${ownerId}::${key}`;
  return `<div class="detail-group space-y-2" data-group="${key}">${renderGroupHeader(groupId, label, count, defaultCollapsed, meta)}<div class="${groupCollapsed(groupId, defaultCollapsed) ? 'hidden' : ''}">${innerHtml}</div></div>`;
}

const RELATION_ROW = 'group flex items-center gap-2 min-w-0 px-2 py-1.5 bg-zinc-950/60';
// A row's edit control shows on hover or focus from sm up, and always below it, where no
// pointer hovers.
const ROW_CONTROL = 'sm:opacity-0 sm:group-hover:opacity-100 focus:opacity-100';

function relationTableHtml(rows) {
  return `<div class="rounded-lg border border-zinc-800 divide-y divide-zinc-800 overflow-hidden">${rows.join('')}</div>`;
}

function relationTitleHtml(title) {
  return `<span class="flex-1 min-w-0 truncate text-[11px] leading-4 text-zinc-400" title="${esc(title || '')}">${esc(title || '')}</span>`;
}

function relationMetaHtml(text) {
  return `<span class="flex-shrink-0 text-[10px] leading-4 text-zinc-400">${esc(text)}</span>`;
}

// A decision row carries its own status's icon and label, not a node status.
function relationIconHtml(d) {
  if (!d.status) return '<span class="flex-shrink-0 font-mono text-[10px] text-red-400">missing</span>';
  if (d.kind === 'decision' && typeof decisionStatusIcon === 'function') return decisionStatusIcon(d.status);
  return statusIcon(d.status);
}

function removeButtonHtml(act, label, data) {
  return `<button type="button" class="p-1 flex-shrink-0 rounded text-zinc-400 hover:text-red-400 hover:bg-zinc-800 ${ROW_CONTROL} ${FOCUS_RING}" data-act="${act}" ${data} aria-label="${esc(label)}">${renderIcon('x', 'w-3 h-3')}</button>`;
}

// A dependency that has not landed where this node builds is marked blocking. A row its
// container or a migration chain imposes names where it comes from and has no remove: removing
// it belongs to that container or chain.
function renderDependencies(node, details, status, editable) {
  const rows = (details || []).map((d) => {
    const own = !d.inherited_from && !d.migration_chain;
    const meta = [
      d.inherited_from ? `via ${d.inherited_from}` : '',
      d.migration_chain ? `${d.migration_chain} migration chain` : '',
      d.finished ? '' : 'blocking',
    ].filter(Boolean).map(relationMetaHtml).join('');
    const remove = editable && own ? removeButtonHtml('remove-dependency', `Remove dependency ${d.id}`, `data-dep-id="${esc(d.id)}"`) : '';
    return `<div class="${RELATION_ROW}"${d.finished ? '' : ' data-blocking'}>${relationIconHtml(d)}${idLink(d.id, d.kind)}${relationTitleHtml(d.title)}${meta}${remove}</div>`;
  });
  return detailGroupHtml(node.id, 'dependencies', 'Dependencies', rows.length, !isBlockedDisplay(status), relationTableHtml(rows));
}

function renderDependents(node, details) {
  const rows = (details || []).map(d => `<div class="${RELATION_ROW}">${relationIconHtml(d)}${idLink(d.id, d.kind)}${relationTitleHtml(d.title)}</div>`);
  return detailGroupHtml(node.id, 'dependents', 'Dependents', rows.length, true, relationTableHtml(rows));
}

function childRowHtml(child, shared) {
  let progress = '';
  if (child.kind !== 'task') {
    const p = progressParts(countsForRow(child));
    if (p.total > 0) progress = progressCount(p);
  }
  let pills = '';
  if (shared.models === null && child.kind === 'task') pills += (child.acceptable_models || []).map(modelPill).join('');
  if (shared.priority === null) pills += priorityPill(child.priority);
  return `<div class="${RELATION_ROW}">${statusIcon(displayOf(child))}${kindBadge(child.kind)}${idLink(child.id, child.kind)}${relationTitleHtml(child.title)}${progress}${leasePulse(leaseOf(child))}${pills}</div>`;
}

// A container's own list, ending with its own step once that has started. On the card each child
// is its own card; in the drawer each is a row with its progress. A value every child shares
// moves to the group header, once.
function renderChildren(node, row, byParent, surface) {
  if (node.kind === 'task') return '';
  const kids = byParent.get(node.id) || [];
  const stepTitle = ownStepTitle(row);
  const kinds = new Set(kids.map(c => c.kind));
  const label = kinds.size > 1 ? 'Plans and tasks' : kinds.has('plan') || (!kinds.size && node.kind === 'spec') ? 'Plans' : 'Tasks';
  const shared = sharedMeta(kids);
  let inner;
  if (surface === 'card') {
    const cards = kids.map(c => (c.kind === 'plan' ? renderPlanCard(c, byParent, shared) : renderTaskCard(c, shared)));
    if (stepTitle) cards.push(ownStepCardHtml(row));
    inner = `<div class="space-y-2.5">${cards.join('')}</div>`;
  } else {
    const rows = kids.map(c => childRowHtml(c, shared));
    if (stepTitle) rows.push(`<div class="own-step ${RELATION_ROW} cursor-pointer" data-step-of="${esc(row.id)}">${ownStepCells(row, relationTitleHtml(stepTitle))}</div>`);
    inner = relationTableHtml(rows);
  }
  return detailGroupHtml(node.id, 'children', label, kids.length + (stepTitle ? 1 : 0), true, inner, sharedMetaHtml(shared));
}

// The full check, on one line that scrolls sideways inside its own block, with a copy control.
function commandBlockHtml(command) {
  return `<div class="command flex items-center gap-2 px-3 py-2.5 rounded-lg border border-zinc-800 bg-zinc-950"><pre class="flex-1 min-w-0 overflow-x-auto font-mono text-xs leading-5 text-zinc-200"><code>${esc(command)}</code></pre><button type="button" class="w-7 h-7 flex-shrink-0 flex items-center justify-center rounded-md bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-zinc-200 ${FOCUS_RING}" data-act="copy-command" data-copy="${esc(command)}" aria-label="Copy command">${renderIcon('copy', 'w-3.5 h-3.5')}</button></div>`;
}

// A verification or condition row: a disclosure whose label is the check's target, opening onto
// its command.
function checkRowHtml(groupId, iconHtml, label, command, trailing) {
  const open = !groupCollapsed(groupId, true);
  return `<div class="check-row group px-2 bg-zinc-950/60${open ? ' pb-2' : ''}"><div class="flex items-center gap-2 min-w-0"><button type="button" class="disclosure flex flex-1 items-center gap-2 min-w-0 py-1.5 rounded-md text-left ${FOCUS_RING}" aria-expanded="${open}" data-group-id="${esc(groupId)}">${renderIcon(open ? 'chevron-down' : 'chevron-right', 'w-3 h-3 flex-shrink-0 text-zinc-400')}${iconHtml}<span class="flex-1 min-w-0 truncate font-mono text-[11px] leading-4 text-zinc-300">${esc(label)}</span></button>${trailing}</div>${open ? commandBlockHtml(command) : ''}</div>`;
}

function renderVerifications(node, verifications, editable) {
  const list = verifications || [];
  const rows = list.map((v) => {
    const kind = VERIFICATION_LABEL[v.verification_type] || v.verification_type;
    const icon = `<span class="flex-shrink-0 text-emerald-400" role="img" aria-label="${esc(kind)}" title="${esc(kind)}">${renderIcon(VERIFICATION_ICON[v.verification_type] || 'check', 'w-3.5 h-3.5')}</span>`;
    const remove = editable ? `<button type="button" class="p-1 flex-shrink-0 rounded text-zinc-400 hover:text-red-400 hover:bg-zinc-800 ${ROW_CONTROL} ${FOCUS_RING}" data-act="remove-verification" data-ver-id="${esc(v.id)}" data-ver-target="${esc(v.target_path)}" aria-label="Remove verification ${esc(v.target_path)}">${renderIcon('x', 'w-3 h-3')}</button>` : '';
    const trailing = `<span class="ver-result flex-shrink-0 text-[10px] font-semibold" data-ver-id="${esc(v.id)}"></span>${remove}`;
    return checkRowHtml(`${node.id}::verification::${v.id}`, icon, v.target_path, v.expected_pattern || v.target_path, trailing);
  });
  const run = editable && list.length
    ? `<div class="pt-2"><button type="button" class="h-7 px-2.5 rounded-md bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-[11px] leading-4 font-medium text-zinc-200 ${FOCUS_RING}" data-act="run-verifications">Run all</button></div>`
    : '';
  return detailGroupHtml(node.id, 'verifications', 'Verifications', list.length, true, relationTableHtml(rows) + run);
}

async function runVerifications(node, control) {
  const results = await submitWrite(control, { method: 'POST', path: `/api/nodes/${node.id}/verify` });
  results.forEach(r => {
    document.querySelectorAll(`.ver-result[data-ver-id="${r.id}"]`).forEach((el) => {
      el.textContent = r.passed ? 'PASS' : 'FAIL';
      el.className = `ver-result flex-shrink-0 text-[10px] font-semibold ${r.passed ? 'text-emerald-400' : 'text-red-400'}`;
      el.title = r.detail || '';
    });
  });
  const passed = results.filter(r => r.passed).length;
  toast(`${passed}/${results.length} verifications passed.`, passed === results.length ? 'success' : 'error');
}

function conditionResult(c) {
  if (c.last_result === null || c.last_result === undefined) return 'not run';
  return c.last_result === 0 ? 'holds' : `exit ${c.last_result}`;
}

function renderConditions(node, conditions, editable) {
  const rows = (conditions || []).map((c) => {
    const remove = editable ? removeButtonHtml('remove-condition', `Remove condition ${c.needs}`, `data-idx="${esc(c.idx)}" data-needs="${esc(c.needs)}"`) : '';
    const trailing = `${relationMetaHtml(String(c.stage || '').toUpperCase())}<span class="flex-shrink-0 text-[10px] leading-4 ${c.last_result === 0 ? 'text-emerald-400' : 'text-amber-400'}">${esc(conditionResult(c))}</span>${remove}`;
    return checkRowHtml(`${node.id}::condition::${c.idx}`, '', c.needs, c.command, trailing);
  });
  return detailGroupHtml(node.id, 'conditions', 'Conditions', rows.length, true, relationTableHtml(rows));
}

// A job names what it lands where, its state and step, and how long ago it last beat.
function renderJobs(node, jobs) {
  const rows = (jobs || []).map((j) => {
    const age = heartbeatAge(j.heartbeat);
    return `<div class="${RELATION_ROW} text-[11px] leading-4"><span class="flex-shrink-0 font-mono text-zinc-200">${esc(j.kind)}</span><span class="flex-1 min-w-0 truncate font-mono text-zinc-400">${esc(j.repo || '')} → ${esc(j.target || '')}</span><span class="flex-shrink-0 text-zinc-200">${esc(j.state)}</span>${j.step ? `<span class="flex-shrink-0 font-mono text-zinc-400">${esc(j.step)}</span>` : ''}${age ? `<time class="flex-shrink-0 font-mono text-zinc-400" datetime="${esc(j.heartbeat)}" title="${esc(j.heartbeat)}">${esc(age)}</time>` : ''}</div>`;
  });
  return detailGroupHtml(node.id, 'jobs', 'Jobs', rows.length, true, relationTableHtml(rows));
}

function renderAttachmentsGroup(node, editable) {
  const list = (node.frontmatter && node.frontmatter.attachments) || [];
  const recheck = editable && list.length
    ? `<div class="pt-2"><button type="button" class="h-7 px-2.5 inline-flex items-center gap-1 rounded-md bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-[11px] leading-4 font-medium text-zinc-200 ${FOCUS_RING}" data-act="recheck-attachments">${renderIcon('rotate-cw', 'w-3 h-3')}Re-check</button></div>`
    : '';
  const cards = `<div class="grid grid-cols-2 gap-2">${list.map(entry => attachmentCardHtml(entry, editable)).join('')}</div>`;
  return detailGroupHtml(node.id, 'attachments', 'Attachments', list.length, true, cards + recheck);
}

// `node` is the body's node (or the row), `row` the store's row when loaded. Sections open,
// Dependencies open while the node shows a blocked or awaiting-decision status, the rest closed.
function nodeDetailHtml(node, body, row, { surface, byParent } = {}) {
  const editable = canEdit();
  const status = detailStatus(node, body, row);
  const lease = (body && body.lease) || (row && row.lease) || null;
  const sections = sectionsListFrom(body);
  const groups = [
    detailGroupHtml(node.id, 'sections', 'Sections', sections.length, false, sectionItemsHtml(sections, node.id, editable)),
    renderChildren(node, row, byParent || visibleChildrenByParent(), surface),
    renderVerifications(node, body.verifications, editable),
    renderDependencies(node, body.dependency_details, status, editable),
    renderDependents(node, body.dependent_details),
    renderConditions(node, body.conditions, editable),
    renderJobs(node, body.jobs),
    renderAttachmentsGroup(node, editable),
  ];
  return `<div class="node-detail space-y-3" data-detail-for="${esc(node.id)}">${factsStripHtml(node, lease, surface)}${groups.join('')}</div>`;
}


// Actions: one menu in the header of every expanded card and of the drawer. Verbs, never a
// status picker: each is a transition the stored status allows, and each asks for its note.
const REOPENABLE = ['FAILED', 'DEFERRED', 'ABANDONED'];
const SETTABLE_ASIDE = ['READY', 'IMPLEMENTED', 'REVIEWED', 'FIXED', 'LANDED', 'FAILED'];

// The menu's items in order; null is a divider. A static export offers Copy ID alone.
function nodeActions(node, hasLease) {
  const items = [{ act: 'copy-id', label: 'Copy ID' }];
  if (!canEdit()) return items;
  const isTask = node.kind === 'task';
  items.push({ act: 'edit', label: 'Edit' });
  if (node.kind !== 'decision') items.push({ act: 'flags', label: 'Flags…' });
  if (REOPENABLE.includes(node.status)) items.push({ act: 'reopen', label: 'Reopen…' });
  if (!hasLease) items.push({ act: 'reset', label: 'Reset…' });
  if (isTask) items.push({ act: 'move', label: 'Move to plan…' });
  items.push(null, { act: 'add-section', label: 'Add section…' });
  if (isTask) {
    items.push(
      { act: 'add-verification', label: 'Add verification…' },
      { act: 'add-dependency', label: 'Add dependency…' },
      { act: 'wait-on-decision', label: 'Wait on decision…' },
    );
  }
  items.push({ act: 'add-condition', label: 'Add condition…' }, { act: 'attach-file', label: 'Attach file…' });
  const tail = [];
  if (!hasLease && SETTABLE_ASIDE.includes(node.status)) {
    tail.push({ act: 'defer', label: 'Defer…' }, { act: 'abandon', label: 'Abandon…', danger: true });
  }
  if (isTask) tail.push({ act: 'supersede', label: 'Supersede…', danger: true });
  if (hasLease) tail.push({ act: 'release-lease', label: 'Release lease', danger: true });
  if (tail.length) items.push(null, ...tail);
  return items;
}

function actionsMenuHtml(node, lease) {
  const items = nodeActions(node, !!lease).map(item => (item === null
    ? '<div class="h-px my-1 bg-zinc-800" role="separator"></div>'
    : `<button type="button" role="menuitem" tabindex="-1" class="w-full h-7 px-2 flex items-center rounded text-left text-xs ${item.danger ? 'text-red-300' : 'text-zinc-200'} hover:bg-zinc-800 focus:bg-zinc-800 focus:outline-none" data-act="${item.act}">${esc(item.label)}</button>`)).join('');
  return `<div class="actions relative z-[1] flex-shrink-0"><button type="button" class="actions-btn h-7 px-2.5 inline-flex items-center gap-1 rounded-md bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-[11px] leading-4 font-medium text-zinc-200 ${FOCUS_RING}" aria-haspopup="menu" aria-expanded="false">Actions${renderIcon('chevron-down', 'w-3 h-3')}</button><div class="actions-menu hidden absolute right-0 top-full mt-1 z-30 w-[200px] p-1 rounded-lg border border-zinc-700 bg-zinc-900 shadow-2xl" role="menu" aria-label="Actions">${items}</div></div>`;
}

function setActionsMenuOpen(menu, open) {
  menu.classList.toggle('hidden', !open);
  menu.parentNode.querySelector('.actions-btn').setAttribute('aria-expanded', String(open));
  if (!open) return;
  clampToViewport(menu);
  const first = menu.querySelector('[role="menuitem"]');
  if (first) first.focus();
}

function closeActionsMenus(except = null) {
  document.querySelectorAll('.actions-menu:not(.hidden)').forEach((menu) => {
    if (menu !== except) setActionsMenuOpen(menu, false);
  });
}

function copyText(text) {
  return navigator.clipboard.writeText(text);
}

// A picked file goes straight to the node; `control` spins while it uploads.
function pickAttachment(node, control, afterChange) {
  const input = document.createElement('input');
  input.type = 'file';
  input.addEventListener('change', () => {
    const file = input.files && input.files[0];
    if (file) attachFile(node, file, control, afterChange);
  });
  input.click();
}

function sectionOf(id, key) {
  const body = detailBody(id);
  const section = body && body.sections[key];
  return section ? { key, header: section.header, content: section.content } : null;
}

function attachmentOf(node, asset) {
  return ((node.frontmatter && node.frontmatter.attachments) || []).find(a => a.asset === asset) || null;
}

const DETAIL_ACTIONS = {
  'copy-id': node => copyText(node.id).then(() => toast(`Copied ${node.id}.`, 'success'), e => toast(e.message, 'error')),
  edit: node => openEditNodeDialog(node),
  flags: node => openFlagsDialog(node),
  reopen: node => openVerbDialog(node, 'reopen'),
  reset: node => openResetDialog(node),
  move: node => openMoveDialog(node),
  'add-section': node => openSectionDialog(node, null),
  'add-verification': node => openAddVerificationDialog(node),
  'add-dependency': node => openAddDependencyDialog(node),
  'wait-on-decision': node => openAddDependencyDialog(node, true),
  'add-condition': node => openAddConditionDialog(node),
  'attach-file': (node, el) => pickAttachment(node, el.closest('.actions').querySelector('.actions-btn')),
  defer: node => openVerbDialog(node, 'defer'),
  abandon: node => openVerbDialog(node, 'abandon'),
  supersede: node => openSupersedeDialog(node),
  'release-lease': node => releaseLease(node),
  'edit-section': (node, el) => openSectionDialog(node, sectionOf(node.id, el.getAttribute('data-key'))),
  'delete-section': (node, el) => removeSection(node, el.getAttribute('data-key')),
  'remove-dependency': (node, el) => removeDependency(node, el.getAttribute('data-dep-id')),
  'remove-verification': (node, el) => removeVerification(node, Number(el.getAttribute('data-ver-id')), el.getAttribute('data-ver-target')),
  'run-verifications': (node, el) => runVerifications(node, el),
  'remove-condition': (node, el) => removeCondition(node, Number(el.getAttribute('data-idx')), el.getAttribute('data-needs')),
  'copy-command': (node, el) => copyText(el.getAttribute('data-copy')).then(() => {
    el.innerHTML = renderIcon('check', 'w-3.5 h-3.5');
    setTimeout(() => { if (el.isConnected) el.innerHTML = renderIcon('copy', 'w-3.5 h-3.5'); }, 1200);
  }, e => toast(e.message, 'error')),
  'open-attachment': (node, el) => {
    const entry = attachmentOf(node, el.getAttribute('data-asset'));
    const url = entry && attachmentAssetUrl(entry);
    if (url) openLightbox(url, entry.caption || entry.name);
  },
  'detach-attachment': (node, el) => {
    const entry = attachmentOf(node, el.getAttribute('data-asset'));
    detachAttachment(node, el.getAttribute('data-asset'), null, entry && entry.name);
  },
  'recheck-attachments': (node, el) => recheckAttachments(node, el),
};

// One listener serves every detail root, card or drawer, whenever it was drawn: a control's
// data-act names its action and its nearest [data-detail-root] names the node.
document.addEventListener('click', (e) => {
  const target = e.target && e.target.closest ? e.target : null;
  if (!target) return;
  const trigger = target.closest('.actions-btn');
  const menu = trigger && trigger.parentNode.querySelector('.actions-menu');
  closeActionsMenus(menu);
  if (menu) {
    setActionsMenuOpen(menu, menu.classList.contains('hidden'));
    return;
  }
  const control = target.closest('[data-act]');
  const root = control && control.closest('[data-detail-root]');
  if (!root) return;
  const owner = control.closest('.actions');
  if (owner) owner.querySelector('.actions-btn').focus();
  const act = DETAIL_ACTIONS[control.getAttribute('data-act')];
  if (act) act(detailNode(root.getAttribute('data-detail-root')), control);
});

// role=menu's keyboard grammar: ArrowDown opens it from its button, arrows move between items,
// Escape closes it back onto the button, Tab closes it.
document.addEventListener('keydown', (e) => {
  const target = e.target && e.target.closest ? e.target : null;
  if (!target) return;
  const trigger = target.closest('.actions-btn');
  if (trigger && e.key === 'ArrowDown') {
    e.preventDefault();
    setActionsMenuOpen(trigger.parentNode.querySelector('.actions-menu'), true);
    return;
  }
  const menu = target.closest('.actions-menu');
  if (!menu) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    setActionsMenuOpen(menu, false);
    menu.parentNode.querySelector('.actions-btn').focus();
  } else if (e.key === 'Tab') {
    setActionsMenuOpen(menu, false);
  } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    const items = [...menu.querySelectorAll('[role="menuitem"]')];
    const at = items.indexOf(document.activeElement);
    items[(at + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus();
  }
});


// The drawer: a dialog over whichever pane is showing. Opening it watches the node through the
// store, so every live change re-renders it from window.tmStore; a node the store holds no body
// for yet is read once from /api/nodes/<id>.
const inspectorLine = document.getElementById('inspector-line');
const inspectorActions = document.getElementById('inspector-actions');
const inspectorTitle = document.getElementById('inspector-title');
const inspectorPills = document.getElementById('inspector-pills');
const inspectorBody = document.getElementById('inspector-body');
const DRAWER_LOADING_DELAY_MS = 300;

let inspectorNodeId = null;
let drawerOpener = null;
// The one /api/nodes/<id> read in flight or answered for the drawer's node:
// { id, body, error, slow }; `slow` once it has been in flight for DRAWER_LOADING_DELAY_MS.
let drawerLoad = null;

function detailBody(id) {
  const body = window.tmStore.bodies.get(id);
  if (body && body.node) return body;
  return drawerLoad && drawerLoad.id === id ? drawerLoad.body : null;
}

function detailNode(id) {
  const body = detailBody(id);
  return (body && body.node) || window.tmStore.rows.get(id) || { id };
}

function drawerOpen() {
  return !graphInspector.classList.contains('hidden');
}

// A node still expanded in the tree/document keeps its own reason to stay watched; only
// drop the watch here when closing or switching leaves nothing else asking for it.
function releaseInspectorWatch(id) {
  if (id && !expandedIds.has(id)) window.tmStore.unwatch([id]);
}

function loadDrawerBody(id) {
  const load = { id, body: null, error: null, slow: false };
  drawerLoad = load;
  const timer = setTimeout(() => {
    if (drawerLoad !== load || load.body || load.error) return;
    load.slow = true;
    renderGraphInspector(id);
  }, DRAWER_LOADING_DELAY_MS);
  api('GET', `/api/nodes/${encodeURIComponent(id)}`)
    .then((body) => { load.body = normalizeBody(body); }, (err) => { load.error = err; })
    .then(() => {
      clearTimeout(timer);
      if (drawerLoad !== load) return;
      const node = load.body && load.body.node;
      if (node && node.kind !== 'task') expandId(node);
      renderGraphInspector(id);
    });
}

function retryDrawerLoad() {
  if (!inspectorNodeId) return;
  loadDrawerBody(inspectorNodeId);
  renderGraphInspector(inspectorNodeId);
}

// UI only: the location that names the node is the caller's to set.
function openDrawer(id) {
  if (drawerOpen() && inspectorNodeId === id) return;
  const opening = !drawerOpen();
  if (opening) drawerOpener = document.activeElement;
  if (inspectorNodeId !== id) releaseInspectorWatch(inspectorNodeId);
  inspectorNodeId = id;
  drawerLoad = null;
  graphInspector.classList.remove('hidden');
  window.tmStore.watch([id]);
  // A container lists its children, which the store only holds once it is open. A deep link's
  // node is named by its body until the reveal has opened its ancestors.
  const known = window.tmStore.rows.get(id) || (detailBody(id) || {}).node;
  if (known && known.kind !== 'task') expandId(known);
  if (!isStaticMode && !detailBody(id)) loadDrawerBody(id);
  renderGraphInspector(id);
  if (opening) inspectorTitle.focus();
}

// A control drawn again since it opened the drawer is found again by the node it names.
function reconnected(el) {
  if (!el || el === document.body || el.isConnected) return el;
  const id = el.getAttribute('data-node-id') || el.getAttribute('data-id');
  return id ? document.querySelector(`${el.localName}[data-node-id="${CSS.escape(id)}"], ${el.localName}[data-id="${CSS.escape(id)}"]`) : null;
}

// UI only, like openDrawer. Focus goes back to the control that opened it, unless it has
// already moved somewhere else on purpose.
function closeDetailDrawer() {
  if (!drawerOpen()) return;
  const focusInside = graphInspector.contains(document.activeElement) || document.activeElement === document.body;
  graphInspector.classList.add('hidden');
  releaseInspectorWatch(inspectorNodeId);
  inspectorNodeId = null;
  drawerLoad = null;
  const opener = drawerOpener;
  drawerOpener = null;
  if (!focusInside) return;
  const refocus = () => {
    const back = reconnected(opener);
    if (back && back !== document.body) back.focus();
  };
  refocus();
  // The render this close scheduled redraws the opener (a tree row); focus its new self after.
  requestAnimationFrame(() => {
    if (document.activeElement === document.body || !document.activeElement.isConnected) refocus();
  });
}

// Closing is a location change: the view without a node.
function closeDrawerByUser() {
  if (readLocation().id) navigate({ view: currentMode, id: null });
  else closeDetailDrawer();
}

// Opens the drawer on a node from any view, Document included, and names it in the location.
function showGraphInspector(nodeId) {
  openNode(nodeId);
  openDrawer(nodeId);
}

inspectorCloseBtn.addEventListener('click', closeDrawerByUser);

function drawerFocusables() {
  return [...graphInspector.querySelectorAll(FOCUSABLE)].filter(el => !el.disabled && !el.closest('.hidden, [hidden]'));
}

// A dialog: Escape closes it and Tab stays inside it, unless a dialog opened over it (a form, the
// lightbox) or a menu or toast inside it already took the key.
document.addEventListener('keydown', (e) => {
  if (e.defaultPrevented || !drawerOpen() || dialogRoot.children.length > 0) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    closeDrawerByUser();
    return;
  }
  if (e.key !== 'Tab') return;
  const els = drawerFocusables();
  if (els.length === 0) return;
  const at = els.indexOf(document.activeElement);
  if (e.shiftKey && at <= 0) {
    e.preventDefault();
    els[els.length - 1].focus();
  } else if (!e.shiftKey && (at === -1 || at === els.length - 1)) {
    e.preventDefault();
    els[0].focus();
  }
});

function drawerPillsHtml(node, row, lease) {
  let pills = leaseBadge(lease);
  if (node.kind === 'task') pills += (node.acceptable_models || []).map(modelPill).join('');
  // The project's own repo ('.') is every node's default, so only another repo is named.
  if (node.target_repo && node.target_repo !== '.') pills += repoPill(node.target_repo);
  pills += priorityPill(node.priority);
  if (node.kind !== 'task' && row) {
    const counts = countsForRow(row);
    const p = progressParts(counts);
    if (p.total > 0) pills += `<div class="w-24">${progressBar(counts)}</div>${progressCount(p)}`;
  }
  return pills;
}

function drawerBodyHtml(nodeId, node, body, row) {
  if (node && body) return nodeDetailHtml(node, body, row, { surface: 'drawer' });
  const load = drawerLoad && drawerLoad.id === nodeId ? drawerLoad : null;
  if (load && load.error && load.error.status === 404) return paneState('empty', `${nodeId} not found`);
  if (load && load.error) return paneState('error', load.error.message, retryDrawerLoad);
  if (!node && (isStaticMode || !load)) return paneState('empty', `${nodeId} not found`);
  return load && load.slow ? paneState('loading') : '';
}

// The header is known from the row before the body arrives. Until the node is known at all,
// its id, drawn once on the line, names the dialog.
function renderGraphInspector(nodeId) {
  const row = window.tmStore.rows.get(nodeId) || null;
  const body = detailBody(nodeId);
  const node = (body && body.node) || row;
  const lease = (body && body.lease) || (row && row.lease) || null;
  graphInspector.setAttribute('data-detail-root', nodeId);
  graphInspector.setAttribute('aria-labelledby', node ? 'inspector-title' : 'inspector-id');
  redrawKeeping(graphInspector, inspectorBody, () => {
    inspectorLine.innerHTML = `${node ? statusIcon(detailStatus(node, body, row)) + kindBadge(node.kind) : ''}<span id="inspector-id" class="min-w-0">${idLink(nodeId, node && node.kind, 'wrap')}</span>${leasePulse(lease)}`;
    inspectorActions.innerHTML = node ? actionsMenuHtml(node, lease) : '';
    inspectorTitle.textContent = node ? node.title : '';
    inspectorPills.innerHTML = node ? drawerPillsHtml(node, row, lease) : '';
    inspectorPills.classList.toggle('hidden', !node);
    inspectorBody.innerHTML = drawerBodyHtml(nodeId, node, body, row);
  });
  attachSectionToggleHandlers(inspectorBody);
  attachGroupHeaderHandlers(inspectorBody, () => renderGraphInspector(nodeId));
}

// Every write lands as a row/body/section item on this same subscription, never a reload; a
// container's drawer also follows its children and its counts.
window.tmStore.onChange((patch) => {
  if (!inspectorNodeId || !drawerOpen() || !patch.rowIds) return;
  const id = inspectorNodeId;
  const touched = patch.rowIds.includes(id) || patch.bodyIds.includes(id) || patch.statusesChanged
    || patch.rowIds.some(rid => { const r = window.tmStore.rows.get(rid); return r && r.parent === id; });
  if (touched) renderGraphInspector(id);
});


// Attachments (§4): a gallery with lightbox, source/age/staleness badges and Re-check, plus
// the Attach-file action and detach. A node's Attachments group draws attachmentCardHtml and
// acts through data-act; the decisions view draws attachButtonHtml and renderAttachments and
// wires them with wireAttachmentControls.

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

// One attachment: the thumbnail (an image opens the lightbox), the name, size, and with
// `provenance` its source and staleness. Its open and detach controls carry both their class and
// their data-act. The name takes focus once the attachment is added.
function attachmentCardHtml(entry, editable, provenance = true) {
  const url = attachmentAssetUrl(entry);
  const isImage = (entry.mime || '').startsWith('image/');
  const thumb = isImage && url
    ? `<button type="button" data-act="open-attachment" class="att-open-btn block w-full aspect-video rounded-md overflow-hidden bg-zinc-900 border border-zinc-800 hover:border-zinc-600 transition" data-asset="${esc(entry.asset)}" aria-label="Open ${esc(entry.name)} full size"><img src="${esc(url)}" alt="${esc(entry.caption || entry.name)}" class="w-full h-full object-cover"></button>`
    : `<div class="flex items-center justify-center aspect-video rounded-md bg-zinc-900 border border-zinc-800 text-zinc-500">${renderIcon(isImage ? 'file-x' : 'file-text', 'w-6 h-6')}</div>`;
  const nameEl = !isImage && url
    ? `<a href="${esc(url)}" download="${esc(entry.name)}" class="att-name rounded-sm text-emerald-400 hover:text-emerald-300 underline decoration-dotted ${FOCUS_RING}">${esc(entry.name)}</a>`
    : `<span class="att-name rounded-sm ${FOCUS_RING}" tabindex="-1">${esc(entry.caption || entry.name)}</span>`;
  const sizeLabel = humanBytes(entry.size_bytes);
  const uri = entry.source && entry.source.uri;
  return `
    <div class="att-card space-y-1.5" data-asset="${esc(entry.asset)}">
      ${thumb}
      <div class="flex items-center justify-between gap-1.5 text-[11px] text-zinc-300">
        <span class="truncate min-w-0" title="${esc(entry.name)}">${nameEl}</span>
        <span class="flex items-center gap-1 flex-shrink-0">
          ${sizeLabel ? `<span class="text-zinc-400">${esc(sizeLabel)}</span>` : ''}
          ${editable ? `<button type="button" data-act="detach-attachment" class="att-detach-btn p-0.5 rounded text-zinc-400 hover:text-red-400 hover:bg-zinc-800 ${FOCUS_RING}" data-asset="${esc(entry.asset)}" aria-label="Detach ${esc(entry.name)}">${renderIcon('x', 'w-3 h-3')}</button>` : ''}
        </span>
      </div>
      ${provenance && uri ? `<div class="truncate text-[10px] font-mono text-zinc-400" title="${esc(uri)}">${esc(uri)}</div>` : ''}
      ${provenance ? `<div class="flex items-center flex-wrap gap-1">${sourceBadgeHtml(entry.source)}</div>` : ''}
    </div>
  `;
}

// The decision header's Attach: it opens the native file picker, and the write starts on choice.
function attachButtonHtml() {
  return `<button type="button" class="att-add-btn h-7 px-2.5 flex items-center flex-shrink-0 rounded-md bg-zinc-800 hover:bg-zinc-700 active:bg-zinc-600 border border-zinc-700 text-[11px] leading-4 font-medium text-zinc-200 transition ${FOCUS_RING}">Attach</button>`;
}

// A decision's Attachments group, below its answer form, only while it has any: names, sizes and
// detach, three to a row from lg up.
function renderAttachments(node, attachments, editable) {
  const list = attachments || [];
  if (list.length === 0) return '';
  const groupId = `${node.id}::attachments`;
  return `
    <div class="dec-attachments space-y-2.5">
      ${renderGroupHeader(groupId, 'Attachments', list.length, false)}
      <div class="grid grid-cols-2 lg:grid-cols-3 gap-3${groupCollapsed(groupId, false) ? ' hidden' : ''}">${list.map(entry => attachmentCardHtml(entry, editable, false)).join('')}</div>
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

async function attachFile(node, file, control, afterChange) {
  let content_base64;
  try {
    content_base64 = await readFileAsBase64(file);
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  const entry = await submitWrite(control, { method: 'POST', path: `/api/nodes/${node.id}/attachments`, body: { filename: file.name, content_base64 } });
  toast(`Attached ${file.name}.`, 'success');
  if (afterChange) await afterChange(entry);
}

function detachAttachment(node, asset, afterChange, name) {
  confirmDialog({
    title: `Detach ${name || asset}?`,
    message: `The attachment will be removed from ${node.id}.`,
    confirmLabel: 'Detach',
    onConfirm: async (write) => {
      await write('DELETE', `/api/nodes/${node.id}/attachments/${encodeURIComponent(asset)}`);
      toast('Attachment detached.', 'success');
      if (afterChange) await afterChange();
    }
  });
}

async function recheckAttachments(node, control, afterChange) {
  await submitWrite(control, { method: 'POST', path: `/api/nodes/${node.id}/attachments/check` });
  toast('Attachment sources re-checked.', 'success');
  if (afterChange) await afterChange();
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
  const addBtn = root.querySelector('.att-add-btn');
  if (addBtn) addBtn.addEventListener('click', () => pickAttachment(node, addBtn, afterChange));
  root.querySelectorAll('.att-detach-btn').forEach(btn => {
    const asset = btn.getAttribute('data-asset');
    const entry = (attachments || []).find(a => a.asset === asset);
    btn.addEventListener('click', () => detachAttachment(node, asset, afterChange, entry && entry.name));
  });
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

