// Decisions view (§3, §6.4): a third view beside Document and Graph. Wraps core.js's
// setViewMode and edit.js's renderNewMenu rather than editing those files, the same
// reassignment pattern detail.js already uses for renderSectionBody/renderUnifiedDocument --
// every function reassigned here is a plain top-level `function` declared in an earlier
// <script> block, and script tags share one global scope executed in order.

window.VIEW_MODES.DECISIONS = 'decisions';

let decisionsData = [];
let selectedDecisionId = null;
let decisionsTab = 'open';

const decisionsPane = document.getElementById('decisions-pane');
const decisionsTabsEl = document.getElementById('decisions-tabs');
const decisionsListEl = document.getElementById('decisions-list');
const decisionsDetailEl = document.getElementById('decisions-detail');

const DECISION_TABS = [
  { key: 'open', label: 'Open', status: 'OPEN' },
  { key: 'answered', label: 'Answered', status: 'ANSWERED' },
  { key: 'withdrawn', label: 'Withdrawn', status: 'WITHDRAWN' },
];

function decisionTabFor(status) {
  const tab = DECISION_TABS.find(t => t.status === status);
  return tab ? tab.key : 'open';
}

function decisionStatusLabel(status) {
  const tab = DECISION_TABS.find(t => t.status === status);
  return tab ? tab.label : status;
}

// A decision dependency row (blockers/dependencies/dependents) reads its own status, not a
// display theme: none of the display themes means Open, Answered or Withdrawn.
const DECISION_STATUS_ICON = { OPEN: 'help-circle', ANSWERED: 'check-circle-2', WITHDRAWN: 'x-circle' };
function decisionStatusIcon(status, size = 'w-3.5 h-3.5') {
  const icon = DECISION_STATUS_ICON[status] || 'help-circle';
  return `<span class="flex-shrink-0" title="${esc(decisionStatusLabel(status))}">${renderIcon(icon, size)}</span>`;
}


// Data ---------------------------------------------------------------------------------------

let decisionsLoadFailed = false;

async function refreshDecisionsData() {
  if (isStaticMode) {
    decisionsData = (window.STATIC_DATA && window.STATIC_DATA.decisions) || [];
  } else {
    try {
      decisionsData = (await api('GET', '/api/decisions')).items;
      decisionsLoadFailed = false;
    } catch (e) {
      // A load failure used to read as "No open decisions." -- an empty queue, not a broken
      // one -- with the badge hiding too, which is the one case that most looks like nothing
      // is wrong.
      console.error('Failed to load decisions:', e);
      decisionsData = [];
      decisionsLoadFailed = true;
      toast(`Could not load decisions: ${e.message}`, 'error');
    }
  }
  updateDecisionsBadge();
  if (currentMode === window.VIEW_MODES.DECISIONS) renderDecisionsView();
}

if (typeof loadAllData === 'function') {
  const previousLoadAllData = loadAllData;
  loadAllData = async function () {
    await previousLoadAllData();
    await refreshDecisionsData();
  };
}


// Toolbar entry point: a badge-carrying button in #view-extra-buttons, and setViewMode
// wrapped so a third mode exists without touching core.js's own DOCUMENT/GRAPH switch.

function renderDecisionsToolbarButton() {
  const extra = document.getElementById('view-extra-buttons');
  if (!extra || document.getElementById('view-decisions-btn')) return;
  extra.insertAdjacentHTML('beforeend', `
    <button id="view-decisions-btn" title="Decisions view" aria-label="Decisions view" class="relative h-8 px-2.5 flex items-center gap-1.5 rounded-lg bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-zinc-400 hover:text-white text-xs font-medium transition">
      ${renderIcon('help-circle', 'w-3.5 h-3.5')}<span class="hidden sm:inline">Decisions</span>
      <span id="decisions-badge" class="hidden absolute -top-1.5 -right-1.5 min-w-[16px] h-4 px-1 rounded-full bg-amber-500 text-black text-[10px] font-bold leading-4 text-center">0</span>
    </button>
  `);
  document.getElementById('view-decisions-btn').addEventListener('click', () => setViewMode(window.VIEW_MODES.DECISIONS));
}
renderDecisionsToolbarButton();

function updateDecisionsBadge() {
  const badge = document.getElementById('decisions-badge');
  if (!badge) return;
  const openCount = decisionsData.filter(d => d.status === 'OPEN').length;
  if (openCount > 0) {
    badge.textContent = openCount > 99 ? '99+' : String(openCount);
    badge.classList.remove('hidden');
  } else {
    badge.classList.add('hidden');
  }
}

if (typeof setViewMode === 'function') {
  const previousSetViewMode = setViewMode;
  setViewMode = function (mode) {
    const decisionsBtn = document.getElementById('view-decisions-btn');
    if (mode === window.VIEW_MODES.DECISIONS) {
      currentMode = mode;
      documentPane.classList.add('hidden');
      graphPane.classList.add('hidden');
      sidebarPane.classList.add('hidden');
      decisionsPane.classList.remove('hidden');
      toggleSectionsBtn.classList.add('hidden');
      viewDocBtn.className = VIEW_BTN_INACTIVE;
      viewGraphBtn.className = VIEW_BTN_INACTIVE;
      if (decisionsBtn) decisionsBtn.className = decisionsBtn.className.replace('bg-zinc-900', 'bg-zinc-800').replace('text-zinc-400', 'text-white');
      renderDecisionsView();
      return;
    }
    decisionsPane.classList.add('hidden');
    if (decisionsBtn) {
      decisionsBtn.className = 'relative h-8 px-2.5 flex items-center gap-1.5 rounded-lg bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-zinc-400 hover:text-white text-xs font-medium transition';
    }
    previousSetViewMode(mode);
  };
}

// Reached from a document-view "Awaiting decision" banner (detail.js) or a decision-detail
// task chip: switch to the Decisions view and open that decision.
function goToDecision(decisionId) {
  const row = decisionsData.find(d => d.id === decisionId);
  decisionsTab = row ? decisionTabFor(row.status) : 'open';
  selectedDecisionId = decisionId;
  setViewMode(window.VIEW_MODES.DECISIONS);
}


// List -----------------------------------------------------------------------------------------

function renderDecisionsTabs() {
  decisionsTabsEl.innerHTML = DECISION_TABS.map(t => {
    const count = decisionsData.filter(d => decisionTabFor(d.status) === t.key).length;
    const active = t.key === decisionsTab;
    const cls = active
      ? 'bg-zinc-800 text-white'
      : 'text-zinc-400 hover:text-white hover:bg-zinc-900';
    return `<button type="button" id="dec-tab-${t.key}" role="tab" aria-selected="${active}" aria-controls="decisions-list" tabindex="${active ? '0' : '-1'}" data-tab="${t.key}" class="dec-tab-btn h-7 px-2.5 rounded-md text-xs font-medium transition ${cls}">${t.label} (${count})</button>`;
  }).join('');
  const tabs = Array.from(decisionsTabsEl.querySelectorAll('.dec-tab-btn'));
  function activate(key, focusIt) {
    decisionsTab = key;
    renderDecisionsView();
    if (focusIt) {
      const btn = decisionsTabsEl.querySelector(`[data-tab="${key}"]`);
      if (btn) btn.focus();
    }
  }
  tabs.forEach((btn, i) => {
    btn.addEventListener('click', () => activate(btn.getAttribute('data-tab'), false));
    // Standard ARIA tabs pattern: arrow keys move focus and selection together.
    btn.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      e.preventDefault();
      const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      activate(next.getAttribute('data-tab'), true);
    });
  });
  decisionsListEl.setAttribute('role', 'tabpanel');
  decisionsListEl.setAttribute('aria-labelledby', `dec-tab-${decisionsTab}`);
}

function decisionAgeText(iso) {
  return ageFromNow(iso);
}

function renderDecisionsList() {
  const rows = decisionsData
    .filter(d => decisionTabFor(d.status) === decisionsTab)
    .sort((a, b) => decisionsTab === 'open'
      ? (b.priority - a.priority) || (new Date(a.created_at) - new Date(b.created_at))
      : (new Date(b.created_at) - new Date(a.created_at)));

  if (decisionsLoadFailed) {
    decisionsListEl.innerHTML = `<div role="alert" class="text-xs text-red-400 px-2 py-6 text-center">Could not load decisions. <button type="button" class="dec-retry-btn underline">Retry</button></div>`;
    const retryBtn = decisionsListEl.querySelector('.dec-retry-btn');
    if (retryBtn) retryBtn.addEventListener('click', refreshDecisionsData);
    return;
  }
  if (rows.length === 0) {
    decisionsListEl.innerHTML = `<div class="text-xs text-zinc-500 italic px-2 py-6 text-center">No ${decisionsTab} decisions.</div>`;
    return;
  }

  decisionsListEl.innerHTML = rows.map(d => {
    const active = d.id === selectedDecisionId;
    return `
      <button type="button" class="dec-row w-full text-left p-2.5 rounded-lg border transition ${active ? 'bg-zinc-800 border-zinc-700' : 'bg-zinc-900/50 border-zinc-800 hover:bg-zinc-900 hover:border-zinc-700'}" data-decision-id="${esc(d.id)}">
        <div class="text-xs font-medium text-zinc-100 line-clamp-2">${esc(d.title)}</div>
        <div class="flex items-center gap-2 mt-1.5 text-[10px] text-zinc-400">
          <span class="font-mono">${esc(d.id)}</span>
          ${d.waiting_count > 0 ? `<span class="px-1.5 py-0.5 rounded-full bg-amber-950/60 text-amber-300 border border-amber-800/60">${d.waiting_count} waiting</span>` : ''}
          <span class="ml-auto">${esc(decisionAgeText(d.created_at))}</span>
        </div>
      </button>
    `;
  }).join('');

  decisionsListEl.querySelectorAll('.dec-row').forEach(btn => {
    btn.addEventListener('click', () => {
      selectedDecisionId = btn.getAttribute('data-decision-id');
      renderDecisionsList();
      renderDecisionDetail(selectedDecisionId);
    });
  });
}


// Detail -----------------------------------------------------------------------------------------

async function fetchNodeDetail(id) {
  if (isStaticMode) return (window.STATIC_DATA.details || {})[id] || null;
  try {
    return await api('GET', `/api/nodes/${id}`);
  } catch (e) {
    return null;
  }
}

function optionCardHtml(opt, isChosen, selectable) {
  const base = 'w-full text-left p-3 rounded-lg border transition space-y-1';
  const cls = isChosen
    ? `${base} bg-emerald-950/40 border-emerald-600`
    : `${base} bg-zinc-900/60 border-zinc-800 ${selectable ? 'hover:border-zinc-600 cursor-pointer' : ''}`;
  const tag = selectable ? 'button' : 'div';
  // At most one option is ever chosen at a time, so a selectable card is a radio, not a
  // plain toggle button -- a screen reader otherwise never announces which one is selected.
  const roleAttrs = selectable ? `type="button" role="radio" aria-checked="${isChosen}"` : '';
  const effect = opt.effect && opt.effect !== 'none'
    ? `<span class="px-1.5 py-0.5 rounded-full bg-amber-950/60 text-amber-300 border border-amber-800/60 text-[10px] font-medium">Then: ${esc(opt.effect.replace('_', ' '))} the blocked nodes</span>`
    : '';
  return `
    <${tag} ${roleAttrs} class="dec-option-card ${cls}" data-option-key="${esc(opt.key)}">
      <div class="flex items-center gap-2">
        <span class="text-sm font-medium text-zinc-100">${esc(opt.label)}</span>
        ${opt.recommended ? '<span class="px-1.5 py-0.5 rounded-full bg-emerald-950/60 text-emerald-300 border border-emerald-800/60 text-[10px] font-medium">Recommended</span>' : ''}
        ${effect}
        ${isChosen ? `<span class="ml-auto">${renderIcon('check-circle-2', 'w-4 h-4 text-emerald-400')}</span>` : ''}
      </div>
      ${opt.description ? `<div class="prose prose-invert prose-sm max-w-none text-xs text-zinc-400">${renderSectionBody(opt.description)}</div>` : ''}
    </${tag}>
  `;
}

function renderDecisionDetail(id) {
  decisionsDetailEl.innerHTML = '<div class="max-w-2xl mx-auto text-sm text-zinc-500 italic pt-12 text-center">Loading&hellip;</div>';
  fetchNodeDetail(id).then(detail => {
    if (selectedDecisionId !== id) return; // a later click superseded this fetch
    if (!detail) {
      decisionsDetailEl.innerHTML = '<div class="max-w-2xl mx-auto text-sm text-red-400 pt-12 text-center">Decision not found.</div>';
      return;
    }
    const node = detail.node;
    const data = (node.frontmatter && node.frontmatter.decision) || {};
    const isOpen = node.status === 'OPEN';
    const isAnswered = node.status === 'ANSWERED';
    const editable = canEdit();
    const attachments = (node.frontmatter && node.frontmatter.attachments) || [];

    const raisedByHtml = data.raised_by ? `
      <button type="button" class="dec-raised-by-link text-xs font-mono text-emerald-400 hover:text-emerald-300 underline decoration-dotted" data-task-id="${esc(data.raised_by)}">
        Raised by ${esc(data.raised_by)}
      </button>` : '';

    // §6.4: open decisions offer editing of blocked tasks. A withdrawn/answered decision only
    // ever shows the read-only chip list -- removing a block from one that already resolved
    // wouldn't change anything downstream, since the tasks it unblocked have already moved on.
    const canEditBlocks = isOpen && editable;
    const waiting = detail.dependent_details || [];
    const waitingHtml = (waiting.length || canEditBlocks) ? `
      <div class="space-y-1.5">
        <div class="text-[11px] font-semibold text-zinc-400 uppercase tracking-wider">Waiting on this (${waiting.length})</div>
        <div class="flex flex-wrap gap-1.5 items-center">
          ${waiting.map(t => `
            <span class="dec-waiting-chip flex items-center gap-1.5 pl-2 pr-1 py-1 rounded-lg bg-zinc-900/60 border border-zinc-800">
              <button type="button" class="dec-task-link flex items-center gap-1.5" data-task-id="${esc(t.id)}">${statusIcon(t.status, 'w-3.5 h-3.5')}<span class="font-mono text-[11px] text-zinc-300">${esc(t.id)}</span></button>
              ${canEditBlocks ? `<button type="button" class="dec-block-remove p-0.5 rounded text-zinc-500 hover:text-red-400 hover:bg-zinc-800" data-task-id="${esc(t.id)}" aria-label="Stop ${esc(t.id)} waiting on this decision">${renderIcon('x', 'w-3 h-3')}</button>` : ''}
            </span>
          `).join('')}
          ${canEditBlocks ? `<button type="button" class="dec-block-add h-7 px-2 rounded-md text-[11px] font-medium text-emerald-400 hover:text-emerald-300 hover:bg-zinc-800 border border-dashed border-zinc-700 transition">+ Add task</button>` : ''}
        </div>
      </div>` : '';

    const sectionsHtml = renderSections(detail.sections, node.id);
    const attachmentsHtml = renderAttachments(node, attachments, editable);

    let answerHtml = '';
    if (isAnswered && data.answer) {
      const chosenLabel = data.answer.option
        ? (data.options.find(o => o.key === data.answer.option) || {}).label
        : null;
      answerHtml = `
        <div class="space-y-2">
          <div class="text-[11px] font-semibold text-emerald-400 uppercase tracking-wider">Answer</div>
          <div class="text-sm text-zinc-100">${esc(chosenLabel || data.answer.text || '(no answer text)')}</div>
          ${chosenLabel && data.answer.text ? `<div class="text-xs text-zinc-400">${esc(data.answer.text)}</div>` : ''}
          ${data.answer.rationale ? `<div class="text-xs text-zinc-400"><span class="font-semibold text-zinc-300">Rationale:</span> ${esc(data.answer.rationale)}</div>` : ''}
          <div class="text-[11px] text-zinc-400">by ${esc(data.answer.answered_by)} &middot; ${esc(new Date(data.answer.answered_at).toLocaleString())}</div>
        </div>
      `;
    } else if (node.status === 'WITHDRAWN') {
      answerHtml = `<div class="text-xs text-zinc-400">Withdrawn${data.withdrawn_reason ? `: ${esc(data.withdrawn_reason)}` : '.'}</div>`;
    }

    let answerFormHtml = '';
    if (isOpen && editable) {
      const options = data.options || [];
      answerFormHtml = `
        <form class="dec-answer-form space-y-3">
          ${options.length ? `<div class="grid gap-2" role="radiogroup" aria-label="Options">${options.map(o => optionCardHtml(o, false, true)).join('')}</div>` : ''}
          ${data.allow_custom !== false ? `
            <div class="dec-custom-card p-3 rounded-lg border border-zinc-800 bg-zinc-900/60 space-y-1.5">
              <div class="text-xs font-medium text-zinc-300">Custom answer</div>
              <textarea class="dec-custom-text ${TEXTAREA_CLS}" rows="2" placeholder="Write a custom answer instead of picking an option"></textarea>
            </div>` : ''}
          ${fieldRow('Rationale', `<textarea class="dec-rationale ${TEXTAREA_CLS}" rows="2" placeholder="(optional)"></textarea>`)}
          <div class="flex items-center justify-between gap-2 pt-1">
            <button type="submit" class="dec-answer-submit h-8 px-3 rounded-lg text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 text-black transition disabled:opacity-40 disabled:cursor-not-allowed" disabled>Answer</button>
            <div class="flex items-center gap-2">
              <button type="button" class="dec-withdraw-btn h-8 px-3 rounded-lg text-xs font-medium bg-zinc-900 hover:bg-red-950 text-red-300 border border-red-900/60 transition">Withdraw&hellip;</button>
            </div>
          </div>
        </form>
      `;
    } else if (isAnswered && editable) {
      answerFormHtml = `<button type="button" class="dec-reopen-btn h-8 px-3 rounded-lg text-xs font-medium bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700 transition">Reopen</button>`;
    } else if (node.status === 'WITHDRAWN' && editable) {
      answerFormHtml = `<button type="button" class="dec-reopen-btn h-8 px-3 rounded-lg text-xs font-medium bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700 transition">Reopen</button>`;
    }

    const chosenCards = (isAnswered && data.answer && data.answer.option)
      ? `<div class="grid gap-2">${(data.options || []).map(o => optionCardHtml(o, o.key === data.answer.option, false)).join('')}</div>`
      : '';

    decisionsDetailEl.innerHTML = `
      <div class="max-w-2xl mx-auto space-y-5 pb-16">
        <div class="space-y-2">
          <div class="flex items-center gap-2">
            <span class="px-2 py-0.5 rounded text-[11px] font-mono uppercase bg-amber-500/10 text-amber-300 border border-amber-500/30">decision</span>
            <span class="font-mono text-xs font-semibold text-zinc-400">${esc(node.id)}</span>
            ${decisionStatusIcon(node.status, 'w-4 h-4')}
            <span class="text-xs font-medium text-zinc-300">${esc(decisionStatusLabel(node.status))}</span>
            ${copyIdButton(node.id)}
          </div>
          <h1 class="text-xl font-bold tracking-tight text-white">${esc(node.title)}</h1>
          ${raisedByHtml}
        </div>
        ${waitingHtml}
        ${sectionsHtml}
        ${attachmentsHtml}
        ${chosenCards}
        ${answerHtml}
        ${answerFormHtml}
      </div>
    `;

    attachSectionToggleHandlers(decisionsDetailEl);
    attachGroupHeaderHandlers(decisionsDetailEl, () => renderDecisionDetail(id));
    wireAttachmentControls(decisionsDetailEl, node, attachments, editable, () => renderDecisionDetail(id));

    decisionsDetailEl.querySelectorAll('.dec-task-link, .dec-raised-by-link').forEach(btn => {
      btn.addEventListener('click', () => {
        setViewMode(window.VIEW_MODES.DOCUMENT);
        selectNode(btn.getAttribute('data-task-id'));
      });
    });

    decisionsDetailEl.querySelectorAll('.dec-block-remove').forEach(btn => {
      btn.addEventListener('click', () => {
        const taskId = btn.getAttribute('data-task-id');
        confirmDialog({
          title: `Stop ${taskId} waiting on ${node.id}?`,
          message: `${taskId} will no longer depend on this decision.`,
          confirmLabel: 'Remove',
          onConfirm: async () => {
            await api('POST', `/api/decisions/${node.id}/blocks`, { remove: [taskId] });
            toast(`${taskId} no longer waits on ${node.id}.`, 'success');
            await afterDecisionWrite(node.id);
          }
        });
      });
    });
    const addBlockBtn = decisionsDetailEl.querySelector('.dec-block-add');
    if (addBlockBtn) {
      addBlockBtn.addEventListener('click', () => {
        const taskOptions = collectTasks(treeData);
        const listId = 'dec-block-picker-list';
        openDialog({
          title: `Block a task on ${node.id}`,
          submitLabel: 'Add',
          bodyHtml: `
            ${fieldRow('Task (id or title)', `<input type="text" required list="${listId}" class="dbk-task ${INPUT_CLS} font-mono" placeholder="task-id"><datalist id="${listId}">${taskOptions.map(t => `<option value="${esc(t.id)}">${esc(t.title)}</option>`).join('')}</datalist>`)}
          `,
          onSubmit: async (panel, close) => {
            const typed = panel.querySelector('.dbk-task').value.trim();
            if (!typed) throw new Error('Task id is required.');
            const match = taskOptions.find(t => t.id === typed || t.title === typed);
            const taskId = match ? match.id : typed;
            await api('POST', `/api/decisions/${node.id}/blocks`, { add: [taskId] });
            toast(`${taskId} now waits on ${node.id}.`, 'success');
            close();
            await afterDecisionWrite(node.id);
          }
        });
      });
    }

    wireDecisionAnswerForm(decisionsDetailEl, node.id);

    const reopenBtn = decisionsDetailEl.querySelector('.dec-reopen-btn');
    if (reopenBtn) {
      reopenBtn.addEventListener('click', async () => {
        try {
          await api('POST', `/api/decisions/${node.id}/reopen`);
          toast(`${node.id} reopened.`, 'success');
          await afterDecisionWrite(node.id);
        } catch (e) {
          toast(e.message, 'error');
        }
      });
    }
  });
}

function wireDecisionAnswerForm(root, decisionId) {
  const form = root.querySelector('.dec-answer-form');
  if (!form) return;
  const submitBtn = form.querySelector('.dec-answer-submit');
  let chosenOption = null;
  const customText = form.querySelector('.dec-custom-text');

  function updateSubmitEnabled() {
    const hasCustom = customText && customText.value.trim().length > 0;
    submitBtn.disabled = !chosenOption && !hasCustom;
  }

  // Shared by an option click and a custom-text edit, so the two can never disagree about
  // which card (if any) is actually highlighted -- typing a custom answer used to leave the
  // previously picked card's highlight in place even though it no longer had chosenOption.
  function paintChosen(chosenCard) {
    form.querySelectorAll('.dec-option-card').forEach(c => {
      const isChosen = c === chosenCard;
      c.classList.toggle('border-emerald-600', isChosen);
      c.classList.toggle('bg-emerald-950/40', isChosen);
      c.classList.toggle('bg-zinc-900/60', !isChosen);
      c.classList.toggle('border-zinc-800', !isChosen);
      c.setAttribute('aria-checked', String(isChosen));
    });
  }

  form.querySelectorAll('.dec-option-card').forEach(card => {
    card.addEventListener('click', () => {
      chosenOption = card.getAttribute('data-option-key');
      if (customText) customText.value = '';
      paintChosen(card);
      updateSubmitEnabled();
    });
  });
  if (customText) {
    customText.addEventListener('input', () => {
      if (customText.value.trim()) {
        chosenOption = null;
        paintChosen(null);
      }
      updateSubmitEnabled();
    });
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const rationale = form.querySelector('.dec-rationale').value.trim();
    const text = customText ? customText.value.trim() : '';
    try {
      await api('POST', `/api/decisions/${decisionId}/answer`, {
        option: chosenOption,
        text,
        rationale,
      });
      toast(`${decisionId} answered.`, 'success');
      await afterDecisionWrite(decisionId);
    } catch (err) {
      toast(err.message, 'error');
    }
  });

  const withdrawBtn = root.querySelector('.dec-withdraw-btn');
  if (withdrawBtn) {
    withdrawBtn.addEventListener('click', () => {
      // §6.4: a decision is withdrawn with a reason, so this is a full dialog (a text field)
      // rather than confirmDialog's plain message-only shape.
      openDialog({
        title: `Withdraw ${decisionId}?`,
        submitLabel: 'Withdraw',
        destructive: true,
        bodyHtml: `
          <p class="text-xs text-zinc-300 leading-relaxed">The decision is dropped; tasks waiting on it unblock immediately.</p>
          ${fieldRow('Reason', `<textarea class="wd-reason ${TEXTAREA_CLS}" rows="2" placeholder="(optional)"></textarea>`)}
        `,
        onSubmit: async (panel, close) => {
          const reason = panel.querySelector('.wd-reason').value.trim();
          await api('POST', `/api/decisions/${decisionId}/withdraw`, { reason });
          toast(`${decisionId} withdrawn.`, 'success');
          close();
          await afterDecisionWrite(decisionId);
        }
      });
    });
  }
}

async function afterDecisionWrite(decisionId) {
  await loadAllData(); // wrapped above to also refresh decisionsData
  selectedDecisionId = decisionId;
  const row = decisionsData.find(d => d.id === decisionId);
  if (row) decisionsTab = decisionTabFor(row.status);
  renderDecisionsView();
}

function renderDecisionsView() {
  renderDecisionsTabs();
  renderDecisionsList();
  if (selectedDecisionId && decisionsData.some(d => d.id === selectedDecisionId)) {
    renderDecisionDetail(selectedDecisionId);
  } else {
    selectedDecisionId = null;
    decisionsDetailEl.innerHTML = '<div class="max-w-2xl mx-auto text-sm text-zinc-500 italic pt-12 text-center">Select a decision to view it.</div>';
  }
}


// New decision dialog -----------------------------------------------------------------------

function decisionOptionRowHtml(index, key = '', label = '', description = '', recommended = false) {
  // The key/label/description inputs were named only by their placeholder, which is not an
  // accessible name (axe aria-input-field-name); a placeholder disappears the moment there is
  // a value, an aria-label never does.
  return `
    <div class="dec-opt-row space-y-1.5 p-2 rounded-lg border border-zinc-800" data-opt-index="${index}">
      <div class="flex items-center gap-2">
        <input type="text" class="dec-opt-key ${INPUT_CLS} font-mono" style="max-width:6rem" placeholder="key" aria-label="Option key" value="${esc(key)}">
        <input type="text" class="dec-opt-label ${INPUT_CLS}" placeholder="Label" aria-label="Option label" value="${esc(label)}">
        <label class="flex items-center gap-1 text-[10px] text-zinc-400 flex-shrink-0">
          <input type="radio" name="dec-opt-recommend" class="dec-opt-recommend" ${recommended ? 'checked' : ''}>Recommended
        </label>
        <button type="button" class="dec-opt-remove p-1 rounded text-zinc-500 hover:text-red-400 hover:bg-zinc-800 flex-shrink-0" aria-label="Remove option">${renderIcon('x', 'w-3 h-3')}</button>
      </div>
      <input type="text" class="dec-opt-desc ${INPUT_CLS}" placeholder="Description (optional)" aria-label="Option description" value="${esc(description)}">
    </div>
  `;
}

function openNewDecisionDialog() {
  let optIndex = 0;
  openDialog({
    title: 'New decision',
    submitLabel: 'Raise',
    bodyHtml: `
      ${fieldRow('Question', `<input type="text" required class="nd-question ${INPUT_CLS}" placeholder="Which auth flow?">`)}
      ${fieldRow('Context', `<textarea class="nd-context ${TEXTAREA_CLS}" rows="3" placeholder="(optional, markdown)"></textarea>`)}
      <div class="space-y-2">
        <div class="flex items-center justify-between">
          <span class="text-[11px] font-semibold text-zinc-400 uppercase tracking-wider">Options</span>
          <button type="button" class="nd-opt-add text-[11px] text-emerald-400 hover:text-emerald-300">+ Add option</button>
        </div>
        <div class="nd-opt-rows space-y-2"></div>
      </div>
      <label class="flex items-center gap-2 text-xs text-zinc-300"><input type="checkbox" class="nd-allow-custom rounded border-zinc-600 bg-zinc-950 text-emerald-500 focus:ring-emerald-500" checked>Allow a custom answer</label>
      ${fieldRow('Blocks tasks (comma separated ids)', `<input type="text" class="nd-blocks ${INPUT_CLS} font-mono" list="nd-blocks-list" placeholder="(optional)"><datalist id="nd-blocks-list">${collectTasks(treeData).map(t => `<option value="${esc(t.id)}">${esc(t.title)}</option>`).join('')}</datalist>`)}
    `,
    onMount: (panel) => {
      const rows = panel.querySelector('.nd-opt-rows');
      panel.querySelector('.nd-opt-add').addEventListener('click', () => {
        rows.insertAdjacentHTML('beforeend', decisionOptionRowHtml(optIndex++));
        wireOptionRemove(rows);
      });
      function wireOptionRemove(root) {
        root.querySelectorAll('.dec-opt-remove').forEach(btn => {
          btn.onclick = () => btn.closest('.dec-opt-row').remove();
        });
      }
    },
    onSubmit: async (panel, close) => {
      const question = panel.querySelector('.nd-question').value.trim();
      if (!question) throw new Error('Question is required.');
      const options = Array.from(panel.querySelectorAll('.dec-opt-row')).map(row => ({
        key: row.querySelector('.dec-opt-key').value.trim(),
        label: row.querySelector('.dec-opt-label').value.trim(),
        description: row.querySelector('.dec-opt-desc').value.trim(),
        recommend: row.querySelector('.dec-opt-recommend').checked,
      })).filter(o => o.key && o.label);
      const recommended = options.find(o => o.recommend);
      const res = await api('POST', '/api/decisions', {
        question,
        context: panel.querySelector('.nd-context').value.trim() || undefined,
        options: options.map(({ key, label, description }) => ({ key, label, description })),
        recommend: recommended ? recommended.key : undefined,
        allow_custom: panel.querySelector('.nd-allow-custom').checked,
        blocks: panel.querySelector('.nd-blocks').value.split(',').map(s => s.trim()).filter(Boolean),
      });
      toast(`Decision ${res.id} raised.`, 'success');
      close();
      await afterDecisionWrite(res.id);
    }
  });
}

// Patch the toolbar's "+ New" menu (edit.js) to add a Decision entry, and re-render it once
// so the button already on screen (edit.js calls renderNewMenu() at its own load time) picks
// it up immediately rather than waiting for the next unrelated re-render.
if (typeof renderNewMenu === 'function') {
  const previousRenderNewMenu = renderNewMenu;
  renderNewMenu = function () {
    previousRenderNewMenu();
    const pop = document.getElementById('new-menu-pop');
    if (!pop || pop.querySelector('[data-new-kind="decision"]')) return;
    const item = document.createElement('button');
    item.type = 'button';
    item.setAttribute('role', 'menuitem');
    item.setAttribute('data-new-kind', 'decision');
    item.className = 'w-full text-left px-2 py-1.5 rounded text-xs text-zinc-200 hover:bg-zinc-800';
    item.textContent = 'Decision';
    item.addEventListener('click', () => {
      pop.classList.add('hidden');
      const menuBtn = document.getElementById('new-menu-btn');
      if (menuBtn) menuBtn.setAttribute('aria-expanded', 'false');
      openNewDecisionDialog();
    });
    pop.appendChild(item);
  };
  renderNewMenu();
}
