import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, jsonResponse, key } from './dom.mjs';

// One plan under one spec: T waits on the open decision-D1 and on landed work X, R waits on T, and
// U waits on X alone.
function row(fields) {
  return {
    status: 'READY', display: 'READY', phase: 'QUEUED', ordinal: 0, priority: 50, child_count: 0,
    acceptable_models: [], target_repo: '.', lease: null, review: false, fix: false, merge: 'parent', waits_on: [],
    ...fields,
  };
}

function body(node, parts = {}) {
  return {
    node: { review_cycles: 0, merge_attempts: 0, step_failures: 0, requires: [], land_order: [], base_chain: ['MAIN'], frontmatter: {}, ...node },
    display: node.display, phase: node.phase,
    sections: [], dependency_details: [], dependent_details: [], verifications: [], conditions: [], jobs: [], lease: null,
    ...parts,
  };
}

const SPEC = row({ id: 'S', kind: 'spec', title: 'A spec', parent: null, child_count: 1, display: 'IMPLEMENTING' });
const PLAN = row({ id: 'P', kind: 'plan', title: 'A plan', parent: 'S', child_count: 3, display: 'IMPLEMENTING' });
const HELD = row({ id: 'T', kind: 'task', title: 'Held by a decision', parent: 'P', display: 'AWAITING_DECISION', waits_on: ['decision-D1'] });
const BEHIND = row({ id: 'R', kind: 'task', title: 'Behind T', parent: 'P', ordinal: 1, display: 'BLOCKED_BY_TASK', waits_on: ['T'] });
const FREE = row({ id: 'U', kind: 'task', title: 'Free to start', parent: 'P', ordinal: 2 });
const ROWS = [SPEC, PLAN, HELD, BEHIND, FREE];
const ROWS_BY_ID = Object.fromEntries(ROWS.map((r) => [r.id, r]));

const DECISION = {
  node: {
    id: 'decision-D1', kind: 'decision', title: 'Pick one', status: 'OPEN', display: 'OPEN', priority: 50,
    frontmatter: { decision: { options: [{ key: 'a', label: 'A', description: '', recommended: true, effect: 'none' }], allow_custom: true, raised_by: 'P', answer: null, custom_effect: 'none' } },
  },
  sections: [],
  dependent_details: [{ id: 'T', kind: 'task', title: HELD.title, status: 'AWAITING_DECISION', finished: false }],
};

const BODIES = {
  S: body(SPEC),
  P: body(PLAN),
  T: body(HELD, {
    dependency_details: [
      { id: 'X', kind: 'task', title: 'Landed work', status: 'COMPLETED', finished: true },
      { id: 'decision-D1', kind: 'decision', title: 'Pick one', status: 'OPEN', finished: false },
    ],
  }),
  R: body(BEHIND, { dependency_details: [{ id: 'T', kind: 'task', title: HELD.title, status: 'AWAITING_DECISION', finished: false }] }),
  U: body(FREE, { dependency_details: [{ id: 'X', kind: 'task', title: 'Landed work', status: 'COMPLETED', finished: true }] }),
  'decision-D1': DECISION,
};

const NODE_BODIES = Object.fromEntries(ROWS.map((r) => [r.id, BODIES[r.id]]));

const WAVES = {
  waves: [{
    entries: [{ id: 'P', title: PLAN.title, kind: 'plan', action: 'review', model: 'opus', repos: ['.'], status_before: 'IMPLEMENTED', status_after: 'REVIEWED', in_flight: false }],
    held: ['T: awaiting decision decision-D1', 'R: waits on T'],
  }],
  max_depth: 20,
  nodes: {
    P: { kind: 'plan', title: PLAN.title, display: 'IMPLEMENTING', lease: null },
    T: { kind: 'task', title: HELD.title, display: 'AWAITING_DECISION', lease: null },
    R: { kind: 'task', title: BEHIND.title, display: 'BLOCKED_BY_TASK', lease: null },
  },
};

async function server(url) {
  const u = new URL(url, 'http://test');
  if (u.pathname === '/api/nodes' && u.searchParams.has('ids')) {
    const found = ROWS_BY_ID[u.searchParams.get('ids')];
    return jsonResponse(200, { items: found ? [found] : [], next: null });
  }
  const m = u.pathname.match(/^\/api\/nodes\/([^/]+)$/);
  if (m) {
    const found = BODIES[decodeURIComponent(m[1])];
    return found ? jsonResponse(200, found) : jsonResponse(404, { detail: 'Node not found' });
  }
  if (u.pathname === '/api/decisions') {
    const open = u.searchParams.get('status') === 'OPEN';
    const items = open ? [{ id: 'decision-D1', title: 'Pick one', status: 'OPEN', priority: 50, created_at: '2026-10-06T10:00:00+00:00' }] : [];
    return jsonResponse(200, { items, next: null, counts: { open: 1, answered: 0, withdrawn: 0 } });
  }
  if (u.pathname === '/api/waves') return jsonResponse(200, WAVES);
  return undefined;
}

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

async function openAt(url, { width = 1440 } = {}) {
  const page = loadPage({ url, fetch: server, beforeScripts: (w) => { w.innerWidth = width; fakeVis(w); } });
  await page.settle();
  page.socket.open();
  const subscribe = page.socket.sent.at(-1);
  page.socket.message({ type: 'snapshot', re: subscribe && subscribe.id, rows: ROWS, bodies: NODE_BODIES, statuses: [{ spec: 'S', plans: [{ plan: 'P', counts: { AWAITING_DECISION: 1, BLOCKED_BY_TASK: 1, READY: 1 } }] }], decisions_open: 1 });
  await page.settle();
  return page;
}

const path = (page) => page.window.location.pathname;
const tooltip = (page) => {
  const tip = page.$('#tm-tooltip');
  return tip.classList.contains('hidden') ? null : tip;
};

function graphNode(page, id) {
  page.window.vis.Network.last.emit('afterDrawing');
  return page.$(`.graph-node-focus[data-node-id="${id}"]`);
}

function focusIn(page, el) {
  el.focus();
  el.dispatchEvent(new page.window.Event('focusin'));
}

// From a view, into Decisions on decision-D1, as the owner gets there: the switcher.
async function decisionFrom(page) {
  page.$('#view-decisions-btn').click();
  await page.settle();
  page.$('.dec-row[data-decision-id="decision-D1"]').click();
  await page.settle();
}

test('no node row carries a decision chip: the Document card, the drawer header, the tree, a Waves entry and a held row', async () => {
  const card = await openAt('/document/T');
  const line = card.$('#doc-node-T .task-line');
  assert.equal(line.querySelector('[role="img"]').getAttribute('aria-label'), 'Awaiting Decision');
  assert.equal(line.querySelectorAll('.decision-chip, a[data-decision]').length, 0);

  const graph = await openAt('/graph/T');
  assert.equal(graph.$$('#inspector-line .decision-chip, #inspector-line a[data-decision], #inspector-pills .decision-chip, #inspector-pills a[data-decision]').length, 0);
  const treeRow = graph.$('#tree-list [role="treeitem"][data-node-id="T"]');
  assert.ok(treeRow, 'the tree reveals T');
  assert.equal(treeRow.querySelectorAll('.decision-chip, a[data-decision]').length, 0);

  const waves = await openAt('/waves');
  const [entry, held] = waves.$$('#waves-content .wave-row');
  assert.equal(entry.querySelectorAll('.decision-chip, a[data-decision]').length, 0);
  assert.equal(held.querySelector('a.id-link').textContent, 'T');
  assert.equal(held.querySelectorAll('.decision-chip').length, 0);
  assert.ok(held.querySelectorAll('a[data-decision]').every((a) => a.closest('.held-reason')), 'the decision is named in the reason alone');
});

test('a node held by a decision opens its Dependencies group, where only a decision dependency is a decision row', async () => {
  for (const [at, scope] of [['/document/T', '#doc-node-T'], ['/graph/T', '#inspector-body']]) {
    const page = await openAt(at);
    const group = page.$(`${scope} [data-group="dependencies"]`);
    assert.equal(group.querySelector('.disclosure').getAttribute('aria-expanded'), 'true', `${at}: open while T is blocked`);
    const decision = group.querySelector('a.id-link[data-id="decision-D1"]');
    assert.ok(decision.hasAttribute('data-decision'));
    assert.equal(decision.getAttribute('href'), '/decisions/decision-D1');
    assert.equal(decision.parentNode.querySelector('[role="img"]').getAttribute('aria-label'), 'Open', 'the decision\'s own status');
    const work = group.querySelector('a.id-link[data-id="X"]');
    assert.equal(work.hasAttribute('data-decision'), false, 'landed work is a node row');
    assert.equal(work.parentNode.querySelector('[role="img"]').getAttribute('aria-label'), 'Completed');
    assert.equal(group.querySelectorAll('a[data-decision]').length, 1, 'one decision, one decision row');
  }
  const unblocked = await openAt('/document/U');
  assert.equal(unblocked.$('#doc-node-U [data-group="dependencies"] .disclosure').getAttribute('aria-expanded'), 'false', 'a node nothing holds keeps it closed');
});

test('the Graph tooltip names each open decision a node waits on, as a decision chip, and work it waits on as nothing', async () => {
  const page = await openAt('/graph');
  const network = page.window.vis.Network.last;
  graphNode(page, 'T');
  network.emit('hoverNode', { node: 'T' });
  const tip = tooltip(page);
  assert.equal(tip.querySelector('.graph-tip-status').textContent, 'Awaiting Decision');
  assert.equal(tip.querySelectorAll('[role="img"]').length, 0);
  const chips = tip.querySelectorAll('.decision-chip');
  assert.deepEqual(chips.map((c) => c.textContent), ['decision-D1']);
  assert.equal(chips[0].localName, 'a');
  assert.ok(chips[0].querySelector('use[href="#icon-help-circle"]'));

  network.emit('hoverNode', { node: 'R' });
  assert.equal(tooltip(page).querySelectorAll('.decision-chip').length, 0, 'R waits on work, which is no chip');
});

test('following a Waiting on this row reveals the node under a collapsed spec, plan and Tasks group, in the view the owner came from', async () => {
  const page = await openAt('/document');
  assert.equal(page.$('#doc-node-P'), null, 'the spec starts collapsed');
  await decisionFrom(page);
  const link = page.$('.dec-waiting-row a.id-link[data-id="T"]');
  assert.equal(link.getAttribute('href'), '/document/T');
  link.focus();
  link.click();
  await page.settle();

  assert.equal(path(page), '/document/T');
  assert.equal(page.$('.node-toggle[data-node-id="S"]').getAttribute('aria-expanded'), 'true');
  assert.equal(page.$('.node-toggle[data-node-id="P"]').getAttribute('aria-expanded'), 'true');
  assert.equal(page.$('#doc-node-P [data-group-id="P::children"]').getAttribute('aria-expanded'), 'true', 'the Tasks group is open');
  assert.equal(page.$('.node-toggle[data-node-id="T"]').getAttribute('aria-expanded'), 'true');
  assert.ok(page.$('#doc-node-T [data-detail-for="T"]'), 'T opens with its groups');
  assert.ok(page.document.activeElement === page.$('#doc-node-T'), 'focus is on its card');
  assert.ok(page.$('#graph-inspector').classList.contains('hidden'), 'no drawer over Document');

  const graph = await openAt('/graph');
  await decisionFrom(graph);
  graph.$('.dec-waiting-row a.id-link[data-id="T"]').click();
  await graph.settle();
  assert.equal(path(graph), '/graph/T');
  assert.equal(graph.$('#inspector-id').textContent, 'T', 'the drawer opens on it');
  assert.equal(graph.$('#tree-list [data-node-id="T"]').getAttribute('aria-selected'), 'true', 'the tree reveals and selects it');
});

test('below sm the toolbar is one row, brand, switcher, search and Filters, and the toggle holds the filters, the status chips and the actions', async () => {
  const page = await openAt('/?status=READY', { width: 375 });
  const toggle = page.$('#filters-toggle-btn');
  const panel = page.$('#filters-panel');
  const row = page.$('#toolbar').children[0];
  assert.equal(page.$('#toolbar').children.length, 1, 'the status chips left the second row');
  assert.ok(row.contains(page.$('#brand-icon')) && row.contains(page.$('#view-doc-btn')) && row.contains(page.$('#search-box')));
  assert.ok(!panel.contains(page.$('#search-box')) && !panel.contains(page.$('#view-doc-btn')));
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.ok(panel.classList.contains('max-sm:hidden'), 'closed, the panel takes no room');
  assert.ok(toggle.querySelector('use[href="#icon-chevron-down"]'));
  assert.equal(toggle.textContent.trim(), 'Filters (1)', 'a status filter counts while its chip is folded away');

  const order = ['filter-controls-group', 'stats-digest', 'toolbar-actions', 'sort-control', 'toggle-sections-btn', 'legend-btn', 'refresh-btn'];
  const inPanel = panel.querySelectorAll(order.map((id) => `#${id}`).join(', ')).map((el) => el.id);
  assert.deepEqual(inPanel, order, 'filters, then chips, then + New and the actions, in Tab order');

  toggle.click();
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.ok(!panel.classList.contains('max-sm:hidden'));

  page.window.innerWidth = 768;
  page.window.dispatchEvent(new page.window.Event('resize'));
  assert.equal(page.$('#toolbar').children.at(-1).id, 'stats-digest', 'from sm up the chips are the second row again');
  assert.ok(panel.classList.contains('sm:contents'), 'and the panel\'s controls are row 1\'s');
  page.window.innerWidth = 375;
  page.window.dispatchEvent(new page.window.Event('resize'));
  assert.ok(page.$('#stats-digest').parentNode === panel, 'below sm the chips are back in the panel');
});

test('activating an id does one thing everywhere: a node opens in the current view, a decision opens the Decisions pane', async () => {
  const graph = await openAt('/graph/T');
  const dep = graph.$('#inspector-body [data-group="dependencies"] a.id-link[data-id="decision-D1"]');
  dep.click();
  await graph.settle();
  assert.equal(path(graph), '/decisions/decision-D1', 'a Dependencies decision row');
  assert.equal(graph.$('#decisions-detail .dec-title').textContent, 'Pick one');
  const raisedBy = graph.$('#decisions-detail .dec-raised-by a.id-link');
  assert.equal(raisedBy.getAttribute('href'), '/graph/P', 'a node id in Decisions names the view the owner came from');
  const waiting = graph.$('.dec-waiting-row a.id-link');
  assert.equal(waiting.getAttribute('href'), '/graph/T');
  waiting.click();
  await graph.settle();
  assert.equal(path(graph), '/graph/T', 'a Waiting on this row');
  graph.$('#inspector-body [data-group="dependencies"] a.id-link[data-id="X"]').click();
  await graph.settle();
  assert.equal(path(graph), '/graph/X', 'a Dependencies node row');

  const tipPage = await openAt('/graph');
  graphNode(tipPage, 'T');
  tipPage.window.vis.Network.last.emit('hoverNode', { node: 'T' });
  const chip = tooltip(tipPage).querySelector('a.id-link.decision-chip');
  assert.equal(chip.getAttribute('href'), '/decisions/decision-D1');
  chip.click();
  await tipPage.settle();
  assert.equal(path(tipPage), '/decisions/decision-D1', 'a Graph tooltip chip');
  assert.equal(tooltip(tipPage), null, 'the tooltip goes with the click');

  const waves = await openAt('/waves');
  const reasons = waves.$$('#waves-content .held-reason');
  assert.equal(reasons[0].textContent, 'waits on decision-D1');
  assert.equal(reasons[0].getAttribute('title'), 'waits on decision-D1');
  const onDecision = reasons[0].querySelector('a.id-link');
  const onWork = reasons[1].querySelector('a.id-link');
  assert.equal(onWork.getAttribute('href'), '/waves/T');
  onWork.click();
  await waves.settle();
  assert.equal(path(waves), '/waves/T', 'a held row\'s waits-on node');
  assert.equal(waves.$('#inspector-id').textContent, 'T');
  onDecision.click();
  await waves.settle();
  assert.equal(path(waves), '/decisions/decision-D1', 'a held row\'s waits-on decision');
});

test('the Graph tooltip opens on keyboard focus of a node as well as on hover, and closes on blur or Escape', async () => {
  const page = await openAt('/graph');
  const node = graphNode(page, 'T');
  focusIn(page, node);
  assert.ok(tooltip(page).textContent.includes('Held by a decision'), 'focus opens it');
  node.dispatchEvent(new page.window.Event('focusout'));
  assert.equal(tooltip(page), null, 'blur closes it');

  focusIn(page, node);
  assert.ok(tooltip(page));
  key(node, 'Escape');
  assert.equal(tooltip(page), null, 'Escape closes it');

  const network = page.window.vis.Network.last;
  network.emit('hoverNode', { node: 'T' });
  assert.ok(tooltip(page), 'hover opens it');
  network.emit('blurNode', { node: 'T' });
  assert.ok(tooltip(page), 'it waits while the pointer crosses to its links');
  page.$('#tm-tooltip').dispatchEvent(new page.window.Event('mouseenter', { bubbles: false }));
  await new Promise((r) => setTimeout(r, 250));
  assert.ok(tooltip(page), 'and stays while the pointer is on it');
  page.$('#tm-tooltip').dispatchEvent(new page.window.Event('mouseleave', { bubbles: false }));
  assert.equal(tooltip(page), null);
});

test('Tab from a focused Graph node enters its tooltip\'s links, and Shift+Tab, Tab past the last link and Escape leave it', async () => {
  const page = await openAt('/graph');
  const node = graphNode(page, 'T');
  const next = graphNode(page, 'R');
  const press = (el, keyName, mods = {}) => {
    const event = new page.window.Event('keydown', { key: keyName, ...mods });
    el.dispatchEvent(event);
    return event;
  };
  const focusOut = (el, to) => el.dispatchEvent(new page.window.Event('focusout', { relatedTarget: to }));
  const active = () => page.document.activeElement;

  // Each focus on the node draws its tooltip afresh, so its links are read again each time.
  const links = () => tooltip(page).querySelectorAll('a.id-link');
  focusIn(page, node);
  assert.ok(links()[1].classList.contains('decision-chip'));
  assert.equal(press(node, 'Tab').defaultPrevented, true, 'Tab from the node goes into its tooltip');
  assert.ok(active() === links()[0], 'focus is on the tooltip\'s first link');
  focusOut(node, links()[0]);
  assert.ok(tooltip(page), 'focus moving into the tooltip keeps it open');

  links()[1].focus();
  assert.equal(press(links()[1], 'Tab').defaultPrevented, true);
  assert.ok(active() === next, 'Tab past the last link goes on to the node after T');

  focusIn(page, node);
  links()[0].focus();
  assert.equal(press(links()[0], 'Tab', { shiftKey: true }).defaultPrevented, true);
  assert.ok(active() === node, 'Shift+Tab before the first link returns to the node');

  focusIn(page, node);
  links()[1].focus();
  press(links()[1], 'Escape');
  assert.ok(active() === node, 'Escape returns focus to the node');
  assert.equal(tooltip(page), null, 'and closes the tooltip');

  focusIn(page, node);
  press(node, 'Escape');
  assert.equal(press(node, 'Tab').defaultPrevented, false, 'with its tooltip closed, Tab leaves the node as usual');

  next.disabled = true;
  focusIn(page, node);
  links()[1].focus();
  press(links()[1], 'Tab');
  assert.ok(active() === graphNode(page, 'U'), 'Tab skips a disabled control, as the browser does');
  next.disabled = false;

  const last = graphNode(page, 'U');
  focusIn(page, last);
  links()[0].focus();
  assert.equal(press(links()[0], 'Tab').defaultPrevented, false, 'after the last node nothing visible follows, so Tab is the browser\'s');

  focusIn(page, node);
  const held = links()[1];
  held.focus();
  page.$('#tm-tooltip').dispatchEvent(new page.window.Event('mouseleave', { bubbles: false }));
  assert.equal(press(held, 'Escape').defaultPrevented, false, 'a key in a tooltip the pointer already closed does nothing');

  focusIn(page, node);
  const chip = links()[1];
  chip.focus();
  focusOut(chip, page.document.body);
  assert.equal(tooltip(page), null, 'focus leaving the tooltip for elsewhere closes it');
});
