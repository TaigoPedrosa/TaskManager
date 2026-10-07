import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, jsonResponse, key, type, servedPage } from './dom.mjs';

const ACTIONS = { implement: 'Implementing', review: 'Reviewing', fix: 'Fixing', merge: 'Merging' };

function render(page, html) {
  const box = page.document.createElement('div');
  box.innerHTML = html;
  page.document.body.appendChild(box);
  return box;
}

function tooltip(page) {
  const tip = page.$('#tm-tooltip');
  return tip.classList.contains('hidden') ? null : tip;
}

function focus(page, el) {
  el.focus();
  el.dispatchEvent(new page.window.Event('focusin'));
}

// A manual clock for the page's own timers: tick(ms) runs every timer due by then.
function fakeClock() {
  let now = 0;
  let next = 0;
  const timers = new Map();
  return {
    setTimeout: (fn, ms = 0) => { timers.set(++next, { at: now + ms, fn }); return next; },
    clearTimeout: (id) => { timers.delete(id); },
    tick(ms) {
      now += ms;
      [...timers].sort((a, b) => a[1].at - b[1].at).forEach(([id, t]) => {
        if (t.at <= now && timers.delete(id)) t.fn();
      });
    },
  };
}

async function liveSnapshot(page, rows = [], extra = {}) {
  page.socket.open();
  const subscribe = page.socket.sent.at(-1);
  page.socket.message({ type: 'snapshot', re: subscribe && subscribe.id, rows, bodies: {}, statuses: [], decisions_open: 0, ...extra });
  await page.settle();
}

test('statusIcon names every status by its label alone: title and accessible name, no visible text', () => {
  const page = loadPage();
  const themes = page.window.STATUS_THEMES;
  for (const code of Object.keys(themes)) {
    const icon = render(page, page.run(`statusIcon(${JSON.stringify(code)})`)).firstChild;
    assert.equal(icon.getAttribute('role'), 'img', code);
    assert.equal(icon.getAttribute('title'), themes[code].label, code);
    assert.equal(icon.getAttribute('aria-label'), themes[code].label, code);
    assert.equal(icon.getAttribute('tabindex'), '0', code);
    assert.equal(icon.textContent.trim(), '', `${code} draws no label beside the icon`);
  }
});

test('leaseBadge shows the action as its status icon and then the agent, never the action word', () => {
  const page = loadPage();
  for (const [action, label] of Object.entries(ACTIONS)) {
    const badge = render(page, page.run(`leaseBadge({ agent_id: 'wf-a', action: '${action}' })`)).firstChild;
    const icon = badge.querySelector('[role="img"]');
    assert.equal(icon.getAttribute('aria-label'), label);
    assert.equal(icon.getAttribute('title'), label);
    assert.equal(badge.textContent.trim(), 'wf-a');
    assert.doesNotMatch(badge.textContent, /implement|review|fix|merg/i);
  }
});

test('leasePulse draws the dot and the heartbeat age, with the action and agent as its name', () => {
  const page = loadPage();
  const beat = new Date(Date.now() - 8000).toISOString();
  const pulse = render(page, page.run(`leasePulse({ agent_id: 'wf-a', action: 'review', last_heartbeat: '${beat}' })`)).firstChild;
  assert.equal(pulse.getAttribute('aria-label'), 'Reviewing · wf-a');
  assert.equal(pulse.getAttribute('tabindex'), '0');
  assert.match(pulse.textContent.trim(), /^\d+s$/);
  assert.equal(page.run('heartbeatAge(new Date(Date.now() - 125000).toISOString())'), '2m');
});

test('a status icon and a lease pulse open their tooltip on focus and on hover, and close on blur or Escape', () => {
  const page = loadPage();
  const box = render(page, page.run(`statusIcon('IMPLEMENTING') + leasePulse({ agent_id: 'wf-a', action: 'fix' })`));
  const [icon, pulse] = box.children;

  focus(page, icon);
  assert.equal(tooltip(page).textContent, 'Implementing');
  assert.equal(icon.getAttribute('title'), null, 'the native title waits while the tooltip shows');
  key(icon, 'Escape');
  assert.equal(tooltip(page), null);
  assert.equal(icon.getAttribute('title'), 'Implementing');

  focus(page, pulse);
  assert.equal(tooltip(page).textContent, 'Fixing · wf-a');
  pulse.dispatchEvent(new page.window.Event('focusout'));
  assert.equal(tooltip(page), null);

  icon.dispatchEvent(new page.window.Event('mouseover'));
  assert.equal(tooltip(page).textContent, 'Implementing');
  icon.dispatchEvent(new page.window.Event('mouseout'));
  assert.equal(tooltip(page), null);
});

test('every renderer escapes what it interpolates', () => {
  const page = loadPage();
  const nasty = '<img src=x onerror=alert(1)>';
  const n = JSON.stringify(nasty);
  const box = render(page, page.run(`[
    idLink(${n}, 'task'), kindBadge('plan'), priorityPill(${n}), modelPill(${n}), repoPill(${n}),
    leasePulse({ agent_id: ${n}, action: 'implement' }), leaseBadge({ agent_id: ${n}, action: 'merge' }),
    disclosureHeader(${n}, ${n}, false, ${n}), paneState('error', ${n}, () => {}),
  ].join('')`));
  assert.equal(box.querySelectorAll('img').length, 0);
  assert.ok(box.textContent.includes(nasty));
});

test('ids keep their own case and every id is a link from the router', () => {
  const page = loadPage();
  const link = render(page, page.run(`idLink('Mixed-Case-1', 'task')`)).firstChild;
  assert.equal(link.localName, 'a');
  assert.equal(link.getAttribute('href'), '/document/Mixed-Case-1');
  assert.ok(!link.className.split(/\s+/).includes('uppercase'));
  assert.equal(link.textContent, 'Mixed-Case-1');
  const decision = render(page, page.run(`idLink('decision-D1', 'decision')`)).firstChild;
  assert.equal(decision.getAttribute('href'), '/decisions/decision-D1');
});

test('a plain click on an id opens it in place, and a modified click is left to the browser', async () => {
  const page = loadPage();
  await liveSnapshot(page, [{ id: 'S', kind: 'spec', title: 'A spec', status: 'READY', display: 'READY', parent: null, ordinal: 0, priority: 50, child_count: 0 }]);
  const link = () => page.$('#unified-document a.id-link[data-id="S"]');

  const modified = new page.window.Event('click', { ctrlKey: true });
  link().dispatchEvent(modified);
  assert.equal(modified.defaultPrevented, false);
  assert.equal(page.window.location.pathname, '/');

  const plain = new page.window.Event('click');
  link().dispatchEvent(plain);
  assert.equal(plain.defaultPrevented, true);
  assert.equal(page.window.location.pathname, '/document/S');
  assert.equal(page.run('expandedIds.has("S")'), true, 'the spec opened in the Document view');

  const decision = render(page, page.run(`idLink('decision-D1', 'decision')`)).firstChild;
  decision.click();
  await page.settle();
  assert.equal(page.window.location.pathname, '/decisions/decision-D1');
});

test('disclosureHeader is a button carrying aria-expanded, its label and its count', () => {
  const page = loadPage();
  for (const expanded of [true, false]) {
    const btn = render(page, page.run(`disclosureHeader('Sections', 3, ${expanded}, 'S::sections')`)).firstChild;
    assert.equal(btn.localName, 'button');
    assert.equal(btn.getAttribute('aria-expanded'), String(expanded));
    assert.equal(btn.textContent.replace(/\s+/g, ' ').trim(), 'Sections 3');
  }
});

test('paneState states the fact alone, and only an error carries a control: Retry, wired to its handler', () => {
  const page = loadPage();
  for (const [kind, message] of [['empty', 'No specs, plans or tasks match.'], ['loading', 'Loading…'], ['error', 'Could not reach the server.']]) {
    let retried = 0;
    page.window.onRetry = () => { retried += 1; };
    const pane = render(page, page.run(`paneState(${JSON.stringify(kind)}, ${JSON.stringify(message)}, onRetry)`)).firstChild;
    assert.equal(pane.querySelector('p').textContent, message);
    const buttons = pane.querySelectorAll('button');
    if (kind === 'error') {
      assert.deepEqual(buttons.map((b) => b.textContent), ['Retry']);
      buttons[0].click();
      assert.equal(retried, 1);
    } else {
      assert.equal(buttons.length, 0, `${kind} has no control`);
    }
  }
});

test('the Document pane reads loading before the first answer, the empty fact after it, and an error with Retry once the socket is lost', async () => {
  const page = loadPage();
  await page.settle();
  const pane = () => page.$('#unified-document .pane-state');
  assert.equal(pane().getAttribute('data-pane-state'), 'loading');

  await liveSnapshot(page);
  assert.equal(pane().getAttribute('data-pane-state'), 'empty');
  assert.equal(pane().textContent.trim(), 'No specs, plans or tasks match.');
  assert.equal(pane().querySelectorAll('button').length, 0);

  const lost = page.socket;
  lost.close();
  await page.settle();
  assert.equal(pane().getAttribute('data-pane-state'), 'error');
  assert.equal(pane().querySelector('p').textContent, 'Could not reach the server.');
  pane().querySelector('.pane-retry').click();
  assert.notEqual(page.window.WebSocket.last, lost, 'Retry reconnects at once');
});

test('a subscribe refused before any answer shows the refusal as the error, with Retry', async () => {
  const page = loadPage();
  page.socket.open();
  const subscribe = page.socket.sent.at(-1);
  page.socket.message({ type: 'error', re: subscribe.id, detail: 'Too many watched ids.' });
  await page.settle();
  const pane = page.$('#unified-document .pane-state');
  assert.equal(pane.getAttribute('data-pane-state'), 'error');
  assert.equal(pane.querySelector('p').textContent, 'Too many watched ids.');
  const sent = page.socket.sent.length;
  pane.querySelector('.pane-retry').click();
  assert.equal(page.socket.sent.length, sent + 1, 'Retry subscribes again');
});

test('a tree row is one line: one status icon, its id in its own case, its title, and no decision chip', async () => {
  const page = loadPage();
  await liveSnapshot(page, [
    { id: 'T-held', kind: 'task', title: 'Held by a decision', status: 'READY', display: 'AWAITING_DECISION', parent: null, ordinal: 0, priority: 50, child_count: 0, waits_on: ['decision-D1'] },
  ]);
  page.$('#view-graph-btn').click();
  await page.settle();
  const row = page.$('#tree-list [role="treeitem"]');
  const icons = row.querySelectorAll('[role="img"]');
  assert.equal(icons.length, 1);
  assert.equal(icons[0].getAttribute('aria-label'), 'Awaiting Decision');
  assert.equal(row.querySelector('a.id-link').textContent, 'T-held');
  assert.ok(row.textContent.includes('Held by a decision'));
  assert.equal(row.querySelectorAll('[data-decision], .decision-chip').length, 0);
  assert.equal(row.querySelectorAll('div').length, 0, 'no second line inside the row');
  assert.ok(!servedPage().includes('zinc-850'), 'no class the stylesheet does not define');
});

test('zero-count toolbar chips drop only their fill and keep an AA count, never dimmed', async () => {
  const page = loadPage();
  await liveSnapshot(page, [], { facets: { status: { READY: 2 } } });
  const chips = page.$$('#stats-digest .st-toggle');
  const ready = chips.find((c) => c.dataset.statusCode === 'READY');
  const zero = chips.find((c) => c.dataset.statusCode === 'FAILED');
  assert.ok(!ready.classList.contains('st-zero'));
  assert.ok(zero.classList.contains('st-zero'));
  assert.ok(zero.querySelector('strong').classList.contains('text-zinc-400'));
  assert.ok(chips.every((c) => !c.classList.contains('opacity-50') && c.classList.contains('rounded-full')));
});

const STEP = (over) => ({
  id: 'T-1', title: 'A task', kind: 'task', action: 'implement', model: 'opus', repos: ['.'],
  status_before: 'READY', status_after: 'IMPLEMENTED', in_flight: false, ...over,
});

async function wavesPage(payload) {
  const page = loadPage({
    url: '/waves',
    fetch: async (u) => (u.startsWith('/api/waves') ? jsonResponse(200, payload) : undefined),
  });
  await page.settle();
  return page;
}

test('a Waves row is one line: now -> next icons, the kind badge on a container, id, title and the lease pulse', async () => {
  const beat = new Date(Date.now() - 5000).toISOString();
  const page = await wavesPage({
    waves: [{
      entries: [
        STEP({ id: 'DECIDE', title: 'Decisions UX', kind: 'spec', action: 'review', status_before: 'REVIEWING', status_after: 'REVIEWED', in_flight: true }),
        STEP({ repos: ['api'] }),
      ],
      held: ['T-2: waits on decision-D1'],
    }],
    max_depth: 20,
    nodes: {
      DECIDE: { kind: 'spec', title: 'Decisions UX', display: 'REVIEWING', lease: { agent_id: 'wf-tm-review-DECIDE', action: 'review', last_heartbeat: beat } },
      'T-2': { kind: 'task', title: 'Held task', display: 'AWAITING_DECISION', lease: null },
    },
  });
  const content = page.$('#waves-content');
  assert.equal(content.querySelector('.wave-head').textContent.replace(/\s+/g, ' ').trim(), 'Wave 1 · 2 steps · opus');

  const [spec, task, held] = content.querySelectorAll('.wave-row');
  for (const row of [spec, task, held]) {
    assert.ok(row.className.split(/\s+/).includes('h-6'), 'one 24px line');
    assert.equal(row.querySelectorAll('div').length, 0, 'no second line inside the row');
    assert.doesNotMatch(row.textContent, /implement|review|fix|merg|wf-tm/i, 'no action word, no agent');
    assert.equal(row.querySelectorAll('.model-pill').length, 0, 'the shared model sits in the header');
  }
  assert.deepEqual(spec.querySelectorAll('[role="img"]').map((i) => i.getAttribute('aria-label')).slice(0, 2), ['Reviewing', 'Waiting Merge']);
  assert.equal(spec.querySelector('.kind-badge').textContent, 'SPEC');
  assert.equal(spec.querySelector('a.id-link').textContent, 'DECIDE');
  assert.equal(spec.querySelector('.lease-pulse').getAttribute('aria-label'), 'Reviewing · wf-tm-review-DECIDE');
  assert.deepEqual(task.querySelectorAll('[role="img"]').map((i) => i.getAttribute('aria-label')), ['Ready', 'Waiting Review']);
  assert.equal(task.querySelector('.kind-badge'), null);
  assert.equal(task.querySelector('.lease-pulse'), null);
  assert.deepEqual(content.querySelectorAll('.repo-pill').map((p) => p.textContent), ['api'], 'the default repo draws no pill');

  assert.equal(held.querySelector('[role="img"]').getAttribute('aria-label'), 'Awaiting Decision');
  assert.equal(held.querySelector('a.id-link').textContent, 'T-2');
  assert.ok(held.textContent.includes('Held task') && held.textContent.includes('waits on decision-D1'));
  assert.equal(held.querySelectorAll('.decision-chip').length, 0, 'no decision chip');
  const toggle = content.querySelector('.disclosure[data-group-id="wave-held-0"]');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
});

test('a wave that mixes models names no model in its header and one on each row', async () => {
  const page = await wavesPage({
    waves: [{ entries: [STEP({ id: 'A' }), STEP({ id: 'B', model: 'sonnet' })], held: [] }],
    max_depth: 20,
    nodes: {},
  });
  const content = page.$('#waves-content');
  assert.equal(content.querySelector('.wave-head').textContent.replace(/\s+/g, ' ').trim(), 'Wave 1 · 2 steps');
  assert.deepEqual(content.querySelectorAll('.wave-row').map((r) => r.querySelector('.model-pill').textContent), ['opus', 'sonnet']);
});

test('a Waves error is the request message with Retry, and Retry asks again', async () => {
  let calls = 0;
  const page = loadPage({
    url: '/waves',
    fetch: async (u) => {
      if (!u.startsWith('/api/waves')) return undefined;
      calls += 1;
      return jsonResponse(500, { detail: 'boom' });
    },
  });
  await page.settle();
  const pane = page.$('#waves-content .pane-state');
  assert.equal(pane.getAttribute('data-pane-state'), 'error');
  assert.equal(pane.querySelector('p').textContent, 'Could not compute waves: boom');
  const before = calls;
  pane.querySelector('.pane-retry').click();
  await page.settle();
  assert.equal(calls, before + 1);
});

function fakeVis(w) {
  class DataSet {
    constructor(items) { this.items = new Map(items.map((i) => [i.id, i])); }
    getIds() { return [...this.items.keys()]; }
    update(list) { list.forEach((i) => this.items.set(i.id, i)); }
    remove(ids) { ids.forEach((id) => this.items.delete(id)); }
  }
  class Network {
    constructor(container, data) { this.data = data; this.handlers = {}; Network.last = this; }
    on(event, fn) { (this.handlers[event] ||= []).push(fn); }
    emit(event, params) { (this.handlers[event] || []).forEach((fn) => fn(params)); }
    getBoundingBox() { return { left: 10, top: 20, right: 180, bottom: 54 }; }
    canvasToDOM(p) { return p; }
    fit() {}
    selectNodes() {}
    unselectAll() {}
    focus() {}
  }
  w.vis = { DataSet, Network };
}

test('a Graph node is focusable, its tooltip spells the status with no icon beside it, and Escape closes it', async () => {
  const page = loadPage({ url: '/graph', beforeScripts: fakeVis });
  await liveSnapshot(page, [{ id: 'T-1', kind: 'task', title: 'A task', status: 'READY', display: 'BLOCKED_BY_TASK', parent: null, ordinal: 0, priority: 50, child_count: 0 }]);
  const network = page.window.vis.Network.last;
  assert.equal(network.data.nodes.items.get('T-1').label, 'T-1', 'the box shows only the id');
  network.emit('afterDrawing');
  const node = page.$('.graph-node-focus[data-node-id="T-1"]');
  assert.equal(node.getAttribute('tabindex'), '0');
  assert.equal(node.getAttribute('aria-label'), 'T-1: A task');

  focus(page, node);
  const tip = tooltip(page);
  assert.ok(tip.textContent.includes('T-1') && tip.textContent.includes('A task'));
  const status = tip.querySelector('.graph-tip-status');
  assert.equal(status.textContent, 'Blocked by Task');
  assert.equal(tip.querySelectorAll('[role="img"]').length, 0, 'the spelled label stands alone');
  key(node, 'Escape');
  assert.equal(tooltip(page), null);

  network.emit('hoverNode', { node: 'T-1' });
  assert.ok(tooltip(page));
  network.emit('blurNode', { node: 'T-1' });
  await new Promise((r) => setTimeout(r, 250));
  assert.ok(!tooltip(page), 'gone once the pointer has had its moment to reach it');
});

test('toasts: one polite region, never focused, a new one replaces the last, gone after 6 s; an error stays until Retry, close or Escape', () => {
  const clock = fakeClock();
  const page = loadPage({ beforeScripts: (w) => { w.setTimeout = clock.setTimeout; w.clearTimeout = clock.clearTimeout; } });
  const root = page.$('#toast-root');
  assert.equal(root.getAttribute('aria-live'), 'polite');
  const focused = page.document.activeElement;

  page.run("toast('one')");
  page.run("toast('two', { tone: 'success' })");
  assert.deepEqual(root.children.map((t) => t.textContent.trim()), ['two']);
  assert.equal(page.document.activeElement, focused);
  clock.tick(5999);
  assert.equal(root.children.length, 1);
  clock.tick(1);
  assert.equal(root.children.length, 0);

  let retried = 0;
  page.window.again = () => { retried += 1; };
  page.run("toast('bad', { tone: 'error', retry: again })");
  clock.tick(60000);
  assert.equal(root.children.length, 1, 'an error toast stays');
  root.querySelector('.toast-retry').click();
  assert.equal(retried, 1);
  assert.equal(root.children.length, 0);

  page.run("toast('bad', { tone: 'error' })");
  root.querySelector('.toast-close').click();
  assert.equal(root.children.length, 0);
  page.run("toast('bad', { tone: 'error' })");
  key(page.document.body, 'Escape');
  assert.equal(root.children.length, 0);
});

test('a failing write disables its control with a spinner, keeps every field as typed, and Retry resends the same body', async () => {
  let answer;
  const page = loadPage({
    fetch: async (u) => (u === '/api/specs' ? new Promise((resolve) => { answer = resolve; }) : undefined),
  });
  page.$('#new-menu-btn').click();
  page.$('[data-new-kind="spec"]').click();
  const title = page.$('#dialog-root .ns-title');
  type(title, 'A new spec');
  const submit = page.$('#dialog-root .dlg-submit');
  submit.click();
  await page.settle();
  assert.equal(submit.disabled, true);
  assert.equal(submit.getAttribute('aria-busy'), 'true');
  assert.ok(submit.querySelector('.animate-spin'), 'the spinner replaces the label');
  assert.equal(page.$('#toast-root').children.length, 0, 'nothing changes before the response');

  answer(jsonResponse(409, { detail: 'Refused.' }));
  await page.settle();
  assert.equal(submit.disabled, false);
  assert.equal(submit.textContent, 'Create');
  assert.equal(title.value, 'A new spec');
  assert.ok(page.$('#dialog-root [role="dialog"]'), 'the form stays open');
  const toastEl = page.$('#toast-root .toast');
  assert.equal(toastEl.getAttribute('data-tone'), 'error');
  assert.ok(toastEl.textContent.includes('Refused.'));

  const sent = () => page.fetchCalls.filter((c) => c.url === '/api/specs');
  toastEl.querySelector('.toast-retry').click();
  await page.settle();
  assert.equal(sent().length, 2);
  assert.equal(sent()[1].body, sent()[0].body);
  assert.equal(JSON.parse(sent()[0].body).title, 'A new spec');
  answer(jsonResponse(201, { id: 'S-NEW' }));
  await page.settle();
  assert.equal(page.$('#dialog-root [role="dialog"]'), null, 'the dialog closes on success');
});

test('submitWrite disables any control it is handed for the whole flight, then restores its label', async () => {
  let answer;
  const page = loadPage({ fetch: async (u) => (u === '/api/x' ? new Promise((resolve) => { answer = resolve; }) : undefined) });
  page.window.btn = render(page, '<button type="button">Go</button>').firstChild;
  page.run("submitWrite(btn, { method: 'POST', path: '/api/x', body: { a: 1 } })");
  await page.settle();
  assert.equal(page.window.btn.disabled, true);
  answer(jsonResponse(200, {}));
  await page.settle();
  assert.equal(page.window.btn.disabled, false);
  assert.equal(page.window.btn.textContent, 'Go');
});

test('a dialog opens on Cancel, keeps Tab inside, and Escape returns focus to its opener with the draft intact', () => {
  const page = loadPage();
  const opener = page.$('#new-menu-btn');
  opener.click();
  page.$('[data-new-kind="spec"]').click();
  const dialog = page.$('#dialog-root [role="dialog"]');
  assert.ok(page.document.activeElement.classList.contains('dlg-cancel'));

  const submit = dialog.querySelector('.dlg-submit');
  submit.focus();
  key(submit, 'Tab');
  assert.ok(page.document.activeElement.classList.contains('dlg-close'), 'Tab wraps from the last control to the first');
  key(page.document.activeElement, 'Tab');
  key(page.document.activeElement, 'Tab');

  const title = dialog.querySelector('.ns-title');
  type(title, 'Half typed');
  key(title, 'Escape');
  assert.equal(page.$('#dialog-root [role="dialog"]'), null);
  assert.equal(page.document.activeElement, opener);
  assert.equal(title.value, 'Half typed');

  opener.click();
  page.$('[data-new-kind="spec"]').click();
  assert.equal(page.$('#dialog-root .ns-title').value, 'Half typed', 'reopening brings the draft back');
  page.$('#dialog-root .dlg-cancel').click();
  assert.equal(page.document.activeElement, opener);
});

test('a confirm leaves once its write succeeds, before the re-read after it', async () => {
  const page = loadPage({ fetch: async (u, o) => (o && o.method === 'DELETE' ? jsonResponse(200, {}) : undefined) });
  page.run("confirmDialog({ title: 'Remove it?', confirmLabel: 'Remove', onConfirm: async (write) => { await write('DELETE', '/api/x'); await new Promise(() => {}); } })");
  page.$('#dialog-root .dlg-submit').click();
  await page.settle();
  assert.equal(page.$('#dialog-root [role="dialog"]'), null);
});

const REMOVED = [
  'Done means Completed and nothing else', 'Drag to resize', 'Filter specs, plans, tasks',
  '(optional)', '(derived from title)', 'one path per line', 'Plan title', 'Spec title', 'Task title',
  'Markdown content', 'comma separated', 'a state outside the corpus', 'exits 0 once it holds',
  'What should the next attempt do differently?', 'Why, and until when?', 'Why is it dropped for good?',
  'Why the stored state was wrong', '(for REVIEWED)', 'A review step follows', 'fixes what its review rejects',
  'the old one is kept as', 'The step is given back', 'will no longer', 'will be removed',
];

test('no explanatory text renders: the legend, the resize handle, the search box and every dialog', async () => {
  const page = loadPage({
    fetch: async (u) => (u.startsWith('/api/meta')
      ? jsonResponse(200, { dispatch: { wave_size: 5, tick_budget: 20 }, specs: [{ id: 'S', title: 'S' }], plans: [{ id: 'P', title: 'P' }], models: ['opus'] })
      : undefined),
  });
  await page.settle();
  page.$('#legend-btn').click();
  const shell = page.document.body.textContent + page.$('#sidebar-resize-handle').outerHTML;
  assert.equal(page.$('#search-box').getAttribute('placeholder'), 'Filter');
  const node = { id: 'T', kind: 'task', title: 'T', priority: 50, frontmatter: { declared_files: ['a'] }, requires: [], acceptable_models: [] };
  page.window.node = node;
  const openers = [
    'openNewSpecDialog()', 'openNewPlanDialog()', 'openNewTaskDialog()', 'openEditNodeDialog(node)',
    "openVerbDialog(node, 'reopen')", "openVerbDialog(node, 'defer')", "openVerbDialog(node, 'abandon')",
    'openResetDialog(node)', 'openFlagsDialog(node)', 'openAddConditionDialog(node)', 'openSupersedeDialog(node)',
    'openMoveDialog(node)', 'openAddDependencyDialog(node)', 'openSectionDialog(node)', 'openAddVerificationDialog(node)',
    "removeCondition(node, 0, 'up')", "removeDependency(node, 'X')", "removeSection(node, 'k')",
    "removeVerification(node, 1, 'a.py')", 'releaseLease(node)',
  ];
  for (const open of openers) {
    page.run(open);
    await page.settle();
    const dialog = page.$('#dialog-root [role="dialog"]');
    assert.ok(dialog, `${open} opened`);
    assert.equal(dialog.querySelectorAll('[placeholder]').length, 0, `${open} has no placeholder`);
    for (const text of REMOVED) assert.ok(!dialog.textContent.includes(text), `${open} still says "${text}"`);
    dialog.querySelectorAll('input:not([type="checkbox"]), textarea, select').forEach((field) => {
      assert.ok(field.closest('label') || field.getAttribute('aria-label'), `${open}: a field without a name`);
    });
    key(page.document.activeElement, 'Escape');
  }
  for (const text of REMOVED) assert.ok(!shell.includes(text), `the page still says "${text}"`);
});

test('the page defines no phase chip: nothing draws one', () => {
  const page = loadPage();
  assert.equal(page.run('typeof phaseChip'), 'undefined');
});

test('the load bar shows from the first subscribe until its answer, while the panes still read loading', async () => {
  const page = loadPage();
  await page.settle();
  const bar = () => !page.$('#load-indicator').classList.contains('hidden');
  assert.equal(bar(), false, 'nothing is in flight before the socket opens');
  page.socket.open();
  await page.settle();
  assert.equal(bar(), true);
  assert.equal(page.$('#unified-document .pane-state').getAttribute('data-pane-state'), 'loading', 'a request in flight is not an answer');
  const subscribe = page.socket.sent.at(-1);
  page.socket.message({ type: 'snapshot', re: subscribe.id, rows: [], bodies: {}, statuses: [], decisions_open: 0 });
  await page.settle();
  assert.equal(bar(), false);
});

// Which items of the toolbar's first row show at a width, in their flex order, read off the
// Tailwind classes they carry: display from hidden, max-sm:hidden, sm:hidden, sm:block, sm:flex
// and lg:block, an sm:contents wrapper's children as row items from sm up, order from
// sm:max-lg:order-last.
function toolbarRowAt(page, width) {
  const sm = width >= 640;
  const lg = width >= 1024;
  const name = (el) => (el.querySelector('#brand-icon') && 'brand') || (el.querySelector('#view-doc-btn') && 'switcher')
    || (el.querySelector('#search-box') && 'search') || (el.querySelector('#refresh-btn') && 'actions')
    || el.id || (el.classList.contains('w-px') && 'divider');
  const items = (parent) => parent.children.flatMap((el) => (sm && el.classList.contains('sm:contents') ? items(el) : [el]));
  return items(page.$('#toolbar').children[0])
    .map((el, i) => {
      const c = (k) => el.classList.contains(k);
      let shown = !c('hidden') && !(!sm && c('max-sm:hidden'));
      if (sm && (c('sm:block') || c('sm:flex'))) shown = true;
      if (sm && c('sm:hidden')) shown = false;
      if (lg && c('lg:block')) shown = true;
      const order = sm && !lg && c('sm:max-lg:order-last') ? 9999 : 0;
      return { name: name(el), shown, order, i };
    })
    .filter((x) => x.shown)
    .sort((a, b) => a.order - b.order || a.i - b.i)
    .map((x) => x.name);
}

test('the toolbar keeps its actions on the first row beside the search at 768, with no divider left behind, and folds them below sm', () => {
  const page = loadPage();
  assert.deepEqual(toolbarRowAt(page, 768), ['brand', 'switcher', 'search', 'actions', 'filter-controls-group', 'toolbar-actions']);
  assert.deepEqual(toolbarRowAt(page, 1440), ['brand', 'switcher', 'search', 'divider', 'filter-controls-group', 'toolbar-actions', 'divider', 'actions']);
  assert.deepEqual(toolbarRowAt(page, 375), ['brand', 'switcher', 'search', 'filters-toggle-btn']);
});
