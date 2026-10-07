import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, jsonResponse, key } from './dom.mjs';

const BEAT = new Date(Date.now() - 8000).toISOString();
const LONG_COMMAND = `d=$(mktemp -d) && trap 'rm -rf "$d"' EXIT && echo "<b>not markup</b>" && ${'x'.repeat(240)}`;

function row(fields) {
  return {
    status: 'READY', display: 'READY', phase: 'QUEUED', ordinal: 0, priority: 50, child_count: 0,
    acceptable_models: [], target_repo: '.', lease: null, review: false, fix: false, merge: 'parent',
    ...fields,
  };
}

function body(node, parts = {}) {
  return {
    node: {
      review: false, fix: false, merge: 'parent', review_cycles: 0, merge_attempts: 0, step_failures: 0,
      requires: [], land_order: [], base_chain: ['MAIN'], frontmatter: {}, ...node,
    },
    display: node.display, phase: node.phase,
    sections: [], dependency_details: [], dependent_details: [], verifications: [], conditions: [], jobs: [], lease: null,
    ...parts,
  };
}

const SPEC = row({ id: 'S', kind: 'spec', title: 'A spec', parent: null, child_count: 1, display: 'IMPLEMENTING' });
const PLAN = row({ id: 'P', kind: 'plan', title: 'A plan', parent: 'S', child_count: 2, display: 'IMPLEMENTING' });
const TASK = row({ id: 'T', kind: 'task', title: 'A task held by a decision', parent: 'P', display: 'AWAITING_DECISION', acceptable_models: ['m-1'], target_repo: 'svc' });
const LEASED = row({
  id: 'U', kind: 'task', title: 'A running task', parent: 'P', ordinal: 1, status: 'IMPLEMENTING', display: 'IMPLEMENTING', phase: 'DISPATCHED',
  acceptable_models: ['m-1'], target_repo: 'svc', lease: { agent_id: 'wf-u', action: 'implement' },
});
const ROWS = [SPEC, PLAN, TASK, LEASED];

const BODIES = {
  S: body(SPEC, {
    sections: [{ key: 'brief', header: '## Brief', content: 'What it is.', ordinal: 1 }],
    dependency_details: [{ id: 'Q', kind: 'spec', title: 'An earlier spec', status: 'COMPLETED', finished: true }],
  }),
  P: body({ ...PLAN, review: true, fix: true }, {
    sections: [{ key: 'context', header: null, content: 'Why.', ordinal: 1 }],
    verifications: [{ id: 7, verification_type: 'file_exists', target_path: 'src/app.py', expected_pattern: null }],
  }),
  T: body({ ...TASK, fix: true, merge_attempts: 2, requires: ['figma'], land_order: ['A', 'B'], base_chain: ['P', 'MAIN'],
    frontmatter: { attachments: [{ asset: 'a1.png', name: 'shot.png', mime: 'image/png', size_bytes: 2048, source: { state: 'fresh' } }] } }, {
    sections: [
      { key: 'objective', header: '## Objective', content: 'Do it.', ordinal: 1 },
      { key: 'acceptance', header: '## Acceptance', content: 'Done.', ordinal: 2 },
    ],
    dependency_details: [
      { id: 'X', kind: 'task', title: 'Landed work', status: 'COMPLETED', finished: true },
      { id: 'decision-D1', kind: 'decision', title: 'Pick one', status: 'OPEN', finished: false },
    ],
    dependent_details: [{ id: 'W', kind: 'task', title: 'Waits on this', status: 'BLOCKED_BY_TASK', finished: false }],
    verifications: [
      { id: 1, verification_type: 'test_command', target_path: 'layout', expected_pattern: LONG_COMMAND },
      { id: 2, verification_type: 'symbol_signature', target_path: 'src/x.py', expected_pattern: 'def render_detail' },
    ],
    conditions: [{ idx: 0, needs: 'The CLI is on PATH', command: 'command -v "tailwindcss" && echo <ok>', stage: 'claim', last_result: null }],
    jobs: [{ id: 'j1', kind: 'land', node_id: 'T', repo: '.', target: 'main', state: 'succeeded', step: null, heartbeat: BEAT }],
  }),
  U: body({ ...LEASED, step_failures: 1, base_chain: ['P', 'MAIN'] }, {
    lease: { agent_id: 'wf-u', action: 'implement', acquired_at: BEAT, last_heartbeat: BEAT },
  }),
};

const STATUSES = [{ spec: 'S', plans: [{ plan: 'P', counts: { AWAITING_DECISION: 1, IMPLEMENTING: 1 } }] }];
const ROWS_BY_ID = Object.fromEntries(ROWS.map((r) => [r.id, r]));

// The API a live page reads around its socket: a node's own body, rows by id, and /api/meta.
function server(overrides = {}) {
  return async (url) => {
    const u = new URL(url, 'http://test');
    if (overrides[u.pathname]) return overrides[u.pathname]();
    if (u.pathname === '/api/nodes' && u.searchParams.has('ids')) {
      const found = ROWS_BY_ID[u.searchParams.get('ids')];
      return jsonResponse(200, { items: found ? [found] : [], next: null });
    }
    const m = u.pathname.match(/^\/api\/nodes\/([^/]+)$/);
    if (m) {
      const found = BODIES[decodeURIComponent(m[1])];
      return found ? jsonResponse(200, found) : jsonResponse(404, { detail: 'Node not found' });
    }
    if (u.pathname === '/api/meta') return jsonResponse(200, { dispatch: { wave_size: 5, tick_budget: 20 }, plans: [{ id: 'P', title: 'A plan' }] });
    return undefined;
  };
}

function snapshot(page, { rows = ROWS, bodies = BODIES } = {}) {
  if (page.socket.readyState !== 1) page.socket.open();
  const subscribe = page.socket.sent.at(-1);
  page.socket.message({ type: 'snapshot', re: subscribe && subscribe.id, rows, bodies, statuses: STATUSES, decisions_open: 1 });
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

async function openAt(url, { fetch = server(), beforeScripts = fakeVis, rows, bodies } = {}) {
  const page = loadPage({ url, fetch, beforeScripts });
  await page.settle();
  snapshot(page, { rows, bodies });
  await page.settle();
  return page;
}

function url(page) {
  const { pathname, search } = page.window.location;
  return pathname + search;
}

function text(el) {
  return el.textContent.replace(/\s+/g, ' ').trim();
}

// A node's own groups (never a nested card's), as the header reads and whether it starts open.
function groupsIn(page, scope, id) {
  return page.$$(`${scope} [data-detail-for="${id}"] > .detail-group`).map((g) => {
    const header = g.querySelector('.disclosure');
    return { group: g.getAttribute('data-group'), header: text(header), open: header.getAttribute('aria-expanded') === 'true' };
  });
}

const drawerShown = (page) => !page.$('#graph-inspector').classList.contains('hidden');

const EXPECTED_GROUPS = {
  T: [
    { group: 'sections', header: 'Sections 2', open: true },
    { group: 'verifications', header: 'Verifications 2', open: false },
    { group: 'dependencies', header: 'Dependencies 2', open: true },
    { group: 'dependents', header: 'Dependents 1', open: false },
    { group: 'conditions', header: 'Conditions 1', open: false },
    { group: 'jobs', header: 'Jobs 1', open: false },
    { group: 'attachments', header: 'Attachments 1', open: false },
  ],
  P: [
    { group: 'sections', header: 'Sections 1', open: true },
    { group: 'children', header: 'Tasks 2', open: false },
    { group: 'verifications', header: 'Verifications 1', open: false },
  ],
  S: [
    { group: 'sections', header: 'Sections 1', open: true },
    { group: 'children', header: 'Plans 1', open: false },
    { group: 'dependencies', header: 'Dependencies 1', open: false },
  ],
};

for (const id of ['T', 'P', 'S']) {
  test(`${id}: the expanded Document card and the drawer draw the same groups, in order, with the same counts and defaults`, async () => {
    const card = await openAt(`/document/${id}`);
    const drawer = await openAt(`/graph/${id}`);
    assert.deepEqual(groupsIn(card, `#doc-node-${id}`, id), EXPECTED_GROUPS[id]);
    assert.deepEqual(groupsIn(drawer, '#inspector-body', id), EXPECTED_GROUPS[id]);
  });
}

test('an empty group is not drawn, and Dependencies opens only while the node is blocked', async () => {
  const page = await openAt('/graph/U');
  assert.deepEqual(groupsIn(page, '#inspector-body', 'U'), [], 'U has no sections, children, checks or relations');
  const blocked = await openAt('/graph/S', {
    rows: ROWS.map((r) => (r.id === 'S' ? { ...r, display: 'BLOCKED_BY_TASK' } : r)),
  });
  assert.equal(groupsIn(blocked, '#inspector-body', 'S').find((g) => g.group === 'dependencies').open, true);
});

test('the facts strip reads where it lands, review and fix, non-zero counters, requires and land order; the card leads with the lease', async () => {
  const facts = (page, scope, id) => page.$$(`${scope} [data-detail-for="${id}"] > .facts > .fact`).map((f) => f.children.map(text).join(' '));
  const drawer = await openAt('/graph/T');
  assert.deepEqual(facts(drawer, '#inspector-body', 'T'), ['Lands tm/P → main', 'Review off', 'Fix on', 'Merge attempts 2', 'Requires figma', 'Land order A → B']);
  const card = await openAt('/document/U');
  assert.deepEqual(facts(card, '#doc-node-U', 'U'), ['Lease wf-u', 'Lands tm/P → main', 'Review off', 'Fix off', 'Step failures 1']);
  assert.ok(card.$('#doc-node-U .fact .lease-badge [aria-label="Implementing"]'), 'the Lease fact is the lease badge');
});

test('the drawer carries the lease, model, repo and priority pills, and a container lists its children with their progress', async () => {
  const task = await openAt('/graph/U');
  const pills = task.$('#inspector-pills');
  assert.ok(pills.querySelector('.lease-badge'));
  assert.equal(text(pills.querySelector('.model-pill')), 'm-1');
  assert.equal(text(pills.querySelector('.repo-pill')), 'svc');
  assert.equal(text(pills.querySelector('.priority-pill')), 'P50');
  assert.match(text(task.$('#inspector-line .lease-pulse')), /^\d+s$/, 'the heartbeat age is drawn once, in the pulse');

  const spec = await openAt('/graph/S');
  assert.equal(spec.$('#inspector-pills .repo-pill'), null, "the project's own repo is not named");
  spec.$('#inspector-body [data-group-id="S::children"]').click();
  await spec.settle();
  const child = spec.$('#inspector-body [data-group="children"] a.id-link[data-id="P"]').parentNode;
  assert.ok(child.querySelector('.kind-badge'));
  assert.ok(text(child).includes('0/2'), 'the plan row shows its own progress');
});

test('a dependency names its blocked subset, and a decision reads its own status icon and label', async () => {
  const page = await openAt('/graph/T');
  const rows = page.$$('#inspector-body [data-group="dependencies"] [data-blocking]');
  assert.deepEqual(rows.map((r) => r.querySelector('a.id-link').getAttribute('data-id')), ['decision-D1']);
  const decision = rows[0];
  assert.ok(decision.querySelector('a.id-link[data-decision]'), 'the decision is an id link to the Decisions pane');
  assert.equal(decision.querySelector('[title]').getAttribute('title'), page.run("decisionStatusLabel('OPEN')"));
  assert.ok(text(decision).endsWith('blocking'));
});

test('the header line shows the status as an icon and never a decision chip, on the card and in the drawer', async () => {
  const card = await openAt('/document/T');
  const line = card.$('#doc-node-T .task-line');
  assert.equal(line.querySelector('[role="img"]').getAttribute('aria-label'), 'Awaiting Decision');
  assert.equal(line.querySelectorAll('a[data-decision]').length, 0);
  assert.ok(card.$('#doc-node-T [data-group="dependencies"] a.id-link[data-id="decision-D1"]'));

  const drawer = await openAt('/graph/T');
  assert.equal(drawer.$('#inspector-line [role="img"]').getAttribute('aria-label'), 'Awaiting Decision');
  assert.equal(drawer.$$('#inspector-line a[data-decision], #inspector-pills a[data-decision]').length, 0);
});

test('a node opened without its row reads display and phase from the body: a decision link shows Awaiting Decision', async () => {
  const page = await openAt('/graph', { rows: [SPEC] });
  page.run("viewModeBeforeDecisions = 'graph'; openBlockedNodeDetail('T')");
  await page.settle();
  assert.equal(page.window.tmStore.rows.has('T'), false);
  assert.equal(page.$('#inspector-line [role="img"]').getAttribute('aria-label'), 'Awaiting Decision');
  assert.equal(page.$('#inspector-title').textContent, TASK.title);
});

test('jobs show their time', async () => {
  const page = await openAt('/graph/T');
  page.$('#inspector-body [data-group-id="T::jobs"]').click();
  await page.settle();
  const time = page.$('#inspector-body [data-group="jobs"] time');
  assert.match(time.textContent, /^\d+s$/);
  assert.equal(time.getAttribute('datetime'), BEAT);
});

test('a verification and a condition open onto their full command, escaped, with a copy control that copies it verbatim', async () => {
  for (const [at, scope] of [['/document/T', '#doc-node-T'], ['/graph/T', '#inspector-body']]) {
    const page = await openAt(at);
    const copied = [];
    page.window.navigator.clipboard.writeText = async (t) => { copied.push(t); };
    page.$(`${scope} [data-group-id="T::verifications"]`).click();
    await page.settle();
    const toggle = page.$(`${scope} [data-group-id="T::verification::1"]`);
    assert.equal(toggle.localName, 'button');
    assert.equal(toggle.getAttribute('aria-expanded'), 'false');
    assert.equal(page.$$(`${scope} [data-group="verifications"] pre`).length, 0, 'collapsed, the command is not drawn');
    toggle.click();
    await page.settle();
    const code = page.$(`${scope} [data-group="verifications"] pre code`);
    assert.equal(code.textContent, LONG_COMMAND);
    assert.equal(code.querySelectorAll('*').length, 0, 'the command is text, never markup');
    assert.ok(code.parentNode.classList.contains('overflow-x-auto'), 'it scrolls sideways inside its own block');
    page.$(`${scope} [data-group="verifications"] [data-act="copy-command"]`).click();
    await page.settle();
    assert.deepEqual(copied, [LONG_COMMAND]);

    page.$(`${scope} [data-group-id="T::verification::2"]`).click();
    await page.settle();
    assert.ok(page.$$(`${scope} [data-group="verifications"] pre code`).some((c) => c.textContent === 'def render_detail'), 'a path check shows its pattern');

    page.$(`${scope} [data-group-id="T::conditions"]`).click();
    await page.settle();
    page.$(`${scope} [data-group-id="T::condition::0"]`).click();
    await page.settle();
    assert.equal(page.$(`${scope} [data-group="conditions"] pre code`).textContent, 'command -v "tailwindcss" && echo <ok>');
  }
});

// The verbs a node's stored status and lease allow: Edit; Flags (not a decision); Reopen
// (FAILED, DEFERRED, ABANDONED); Reset (no lease); Defer and Abandon (no lease, a status that can
// be set aside); Supersede and Move (a task); Release lease (a lease).
function allowedVerbs(node, hasLease) {
  const acts = ['edit'];
  if (node.kind !== 'decision') acts.push('flags');
  if (['FAILED', 'DEFERRED', 'ABANDONED'].includes(node.status)) acts.push('reopen');
  if (!hasLease) acts.push('reset');
  if (!hasLease && ['READY', 'IMPLEMENTED', 'REVIEWED', 'FIXED', 'LANDED', 'FAILED'].includes(node.status)) acts.push('defer', 'abandon');
  if (node.kind === 'task') acts.push('supersede', 'move');
  if (hasLease) acts.push('release-lease');
  return acts;
}

async function dialogTitles(page, scope, acts) {
  const titles = [];
  for (const act of acts) {
    page.$(`${scope} .actions-btn`).click();
    page.$(`${scope} .actions-menu [data-act="${act}"]`).click();
    await page.settle();
    const dialog = page.$('#dialog-root [role="dialog"]');
    titles.push(dialog && text(dialog.querySelector('h2')));
    key(page.document.activeElement, 'Escape');
    await page.settle();
  }
  return titles;
}

for (const [id, hasLease] of [['T', false], ['U', true]]) {
  test(`${id}: the Actions menu offers every verb its status and lease allow, on both surfaces, and each opens the same dialog`, async () => {
    const card = await openAt(`/document/${id}`);
    const drawer = await openAt(`/graph/${id}`);
    const items = (page, scope) => page.$$(`${scope} .actions-menu [role="menuitem"]`).map((b) => b.getAttribute('data-act'));
    const cardScope = `#doc-node-${id} .task-line`;
    assert.deepEqual(items(card, cardScope), items(drawer, '#inspector-actions'));
    const verbs = allowedVerbs(BODIES[id].node, hasLease);
    assert.deepEqual(verbs.filter((a) => !items(card, cardScope).includes(a)), []);
    const titles = await dialogTitles(card, cardScope, verbs);
    assert.ok(titles.every(Boolean), 'every action opens a dialog');
    assert.deepEqual(await dialogTitles(drawer, '#inspector-actions', verbs), titles);
  });
}

test('the drawer is a dialog named by its title: focus moves to the heading, Tab stays inside, Escape closes it back onto the opener', async () => {
  const page = await openAt('/graph');
  page.run("expandId(window.tmStore.rows.get('S')); expandId(window.tmStore.rows.get('P'))");
  page.run('scheduleRender()');
  await page.settle();
  const drawer = page.$('#graph-inspector');
  assert.equal(drawer.getAttribute('role'), 'dialog');
  assert.equal(drawer.getAttribute('aria-labelledby'), 'inspector-title');
  assert.equal(page.$('#inspector-close-btn').getAttribute('aria-label'), 'Close details');

  const opener = page.$('#tree-list [data-node-id="U"]');
  opener.focus();
  key(opener, 'Enter');
  await page.settle();
  assert.ok(drawerShown(page));
  assert.equal(page.document.activeElement, page.$('#inspector-title'));
  assert.equal(page.$('#inspector-title').textContent, LEASED.title);

  const focusables = page.run('drawerFocusables()');
  assert.ok(focusables.length > 2);
  focusables.at(-1).focus();
  key(focusables.at(-1), 'Tab');
  assert.equal(page.document.activeElement, focusables[0], 'Tab from the last control wraps to the first');
  const back = new page.window.Event('keydown', { key: 'Tab', shiftKey: true });
  focusables[0].dispatchEvent(back);
  assert.equal(page.document.activeElement, focusables.at(-1), 'Shift+Tab from the first wraps to the last');
  page.$('#tree-list').focus();
  key(page.$('#tree-list'), 'Tab');
  assert.ok(page.$('#graph-inspector').contains(page.document.activeElement), 'Tab from outside comes back in');

  key(page.document.activeElement, 'Escape');
  await page.settle();
  assert.equal(drawerShown(page), false);
  assert.equal(page.document.activeElement.getAttribute('data-node-id'), 'U', 'focus is back on the tree row that opened it');
  assert.ok(page.$('#tree-list').contains(page.document.activeElement), 'on the row as drawn now, not the one the selection redrew');
  assert.equal(url(page), '/graph');
});

test('at 384px the drawer header keeps Actions and Close on their line: a long id breaks instead', async () => {
  const long = 'WEBUX-NODES-DETAIL-WITH-A-VERY-LONG-IDENTIFIER-THAT-NEVER-FITS';
  const rows = [...ROWS, row({ id: long, kind: 'task', title: 'Long', parent: null })];
  const page = await openAt(`/graph/${long}`, { rows, bodies: { ...BODIES, [long]: body(rows.at(-1)) } });
  const line = page.$('#inspector-line');
  assert.ok(line.classList.contains('flex-1') && line.classList.contains('min-w-0') && line.classList.contains('flex-wrap'));
  assert.ok(page.$('#inspector-id a.id-link').classList.contains('break-all'));
  assert.ok(page.$('#inspector-actions').classList.contains('flex-shrink-0'));
  assert.ok(page.$('#inspector-close-btn').classList.contains('flex-shrink-0'));
  assert.equal(line.nextSibling.nextSibling, page.$('#inspector-actions'), 'Actions follows the line, outside it');
});

test('a deep link reveals collapsed ancestors (spec, plan, its children group) before it scrolls to the node', async () => {
  const scrolled = [];
  const page = loadPage({ url: '/document/T', fetch: server() });
  const proto = Object.getPrototypeOf(page.$('#unified-document'));
  const original = proto.scrollIntoView;
  proto.scrollIntoView = function () { scrolled.push(this.id); };
  try {
    await page.settle();
    snapshot(page, { rows: [SPEC] });
    await page.settle();
    assert.deepEqual([...page.run('[...expandedIds].sort()')], ['P', 'S', 'T']);
    assert.ok(page.run("collapsedGroups.has('S::children') && collapsedGroups.has('P::children')"));
    assert.deepEqual([...page.socket.sent.at(-1).open].sort(), ['P', 'S']);
    assert.deepEqual(scrolled, [], 'nothing to scroll to before the rows arrive');

    page.socket.message({ type: 'update', items: [{ op: 'row', row: PLAN }, { op: 'row', row: TASK }, { op: 'row', row: LEASED }] });
    await page.settle();
    assert.deepEqual(scrolled, ['doc-node-T']);
    assert.equal(page.document.activeElement, page.$('#doc-node-T'));

    page.socket.message({ type: 'update', items: [{ op: 'row', row: { ...LEASED, title: 'Renamed' } }] });
    await page.settle();
    assert.equal(page.document.activeElement, page.$('#doc-node-T'), 'a later redraw keeps the revealed card focused');
  } finally {
    proto.scrollIntoView = original;
  }
});

test('the node the path names is drawn selected: the card and the tree row carry aria-selected, the Graph node the zinc-100 stroke', async () => {
  const doc = await openAt('/document/T');
  const card = doc.$('#doc-node-T');
  assert.equal(card.getAttribute('aria-selected'), 'true');
  assert.ok(card.classList.contains('border-emerald-400'));
  assert.equal(doc.$$('[aria-selected="true"][data-detail-root]').length, 1);

  const graph = await openAt('/graph/T');
  assert.equal(graph.$('#tree-list [data-node-id="T"]').getAttribute('aria-selected'), 'true');
  const nodes = graph.window.vis.Network.last.data.nodes.items;
  assert.equal(nodes.get('T').color.border, '#f4f4f5');
  assert.equal(nodes.get('U').color.border, graph.window.STATUS_THEMES.IMPLEMENTING.graph_border);

  graph.$('#inspector-close-btn').click();
  await graph.settle();
  assert.equal(nodes.get('T').color.border, graph.window.STATUS_THEMES.AWAITING_DECISION.graph_border, 'closing drops the stroke');
});

test('opening a node from Graph pushes /graph/<id>, closing pushes /graph, and Back returns to where it was opened from', async () => {
  const page = await openAt('/graph');
  const { history } = page.window;
  page.window.vis.Network.last.emit('click', { nodes: ['S'] });
  await page.settle();
  assert.equal(url(page), '/graph/S');
  assert.equal(history.length, 2);
  assert.ok(drawerShown(page));

  history.back();
  await page.settle();
  assert.equal(url(page), '/graph');
  assert.equal(drawerShown(page), false);
  assert.equal(page.run('selectedNodeId'), null);

  history.forward();
  await page.settle();
  assert.ok(drawerShown(page));
  page.$('#inspector-close-btn').click();
  await page.settle();
  assert.equal(url(page), '/graph');
  assert.equal(history.length, 3, 'closing is its own entry');
});

test('Waves: opening a row pushes /waves/<id> with the drawer, and Back closes it', async () => {
  const page = await openAt('/waves');
  page.run("openNode('U')");
  await page.settle();
  assert.equal(url(page), '/waves/U');
  assert.ok(drawerShown(page));
  page.window.history.back();
  await page.settle();
  assert.equal(url(page), '/waves');
  assert.equal(drawerShown(page), false);
});

test('Document: an id link reveals the node as a pushed entry, and Back clears it', async () => {
  const page = await openAt('/document');
  page.$('#unified-document a.id-link[data-id="S"]').click();
  await page.settle();
  assert.equal(url(page), '/document/S');
  assert.equal(page.$('#doc-node-S').getAttribute('aria-selected'), 'true');
  assert.equal(drawerShown(page), false, 'Document never opens the drawer for its own reveal');
  page.window.history.back();
  await page.settle();
  assert.equal(url(page), '/document');
  assert.equal(page.$('#doc-node-S').getAttribute('aria-selected'), null);
});

test('loading /<view>/<id> reveals it in Document and opens the drawer on it in Graph and Waves', async () => {
  const doc = await openAt('/document/U');
  assert.ok(doc.$('#doc-node-U [data-detail-for="U"]'));
  assert.equal(drawerShown(doc), false);
  for (const view of ['graph', 'waves']) {
    const page = await openAt(`/${view}/U`);
    assert.ok(drawerShown(page), view);
    assert.equal(page.$('#inspector-id').textContent, 'U');
  }
});

test('an unknown id shows the view with a not-found pane state', async () => {
  const doc = await openAt('/document/NOPE');
  const pane = doc.$('#unified-document .pane-state');
  assert.equal(pane.getAttribute('data-pane-state'), 'empty');
  assert.equal(text(pane), 'NOPE not found');
  assert.ok(doc.$('#doc-node-S'), 'the view itself still shows');

  const graph = await openAt('/graph/NOPE');
  const drawerPane = graph.$('#inspector-body .pane-state');
  assert.equal(text(drawerPane), 'NOPE not found');
  assert.equal(drawerPane.querySelectorAll('button').length, 0, 'not found carries no control');
});

test('a drawer read that fails shows the request message with Retry, which reads it again', async () => {
  let calls = 0;
  const failing = server({ '/api/nodes/T': () => { calls += 1; return jsonResponse(502, {}); } });
  const page = await openAt('/graph/T', { fetch: failing, bodies: {} });
  const pane = page.$('#inspector-body .pane-state');
  assert.equal(pane.getAttribute('data-pane-state'), 'error');
  assert.equal(text(pane.querySelector('p')), 'GET /api/nodes/T failed (502)');
  pane.querySelector('.pane-retry').click();
  await page.settle();
  assert.equal(calls, 2);
});

test('collapsed card headers keep the id and the title, with only pills hidden below sm', async () => {
  const page = await openAt('/document');
  page.run("expandId(window.tmStore.rows.get('S')); expandId(window.tmStore.rows.get('P')); collapsedGroups.add('S::children'); collapsedGroups.add('P::children')");
  page.run('scheduleRender()');
  await page.settle();
  for (const id of ['P', 'T', 'U']) {
    const line = page.$(`#doc-node-${id} .task-line, #doc-node-${id} .plan-line`);
    const link = line.querySelector('a.id-link');
    assert.equal(link.closest('.hidden'), null, `${id}'s id stays below sm`);
    assert.ok(link.classList.contains('flex-shrink-0'), `${id}'s id never shrinks`);
    const title = line.querySelector('span.truncate');
    assert.equal(title.closest('.hidden'), null, `${id}'s title stays below sm`);
    assert.equal(title.textContent, ROWS_BY_ID[id].title);
  }
});

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

for (const [delay, shows] of [[100, false], [400, true]]) {
  test(`the drawer's loading pane state ${shows ? 'shows' : 'never shows'} for a ${delay} ms /api/nodes/<id> read`, async () => {
    const clock = fakeClock();
    let answer;
    const fetch = server({ '/api/nodes/T': () => new Promise((resolve) => { answer = () => resolve(jsonResponse(200, BODIES.T)); }) });
    const page = loadPage({ url: '/graph', fetch, beforeScripts: (w) => { fakeVis(w); w.setTimeout = clock.setTimeout; w.clearTimeout = clock.clearTimeout; } });
    await page.settle();
    snapshot(page, { bodies: {} });
    await page.settle();
    page.run("openNode('T')");
    await page.settle();
    const loading = () => page.$('#inspector-body [data-pane-state="loading"]');
    clock.tick(delay);
    await page.settle();
    assert.equal(!!loading(), shows);
    answer();
    await page.settle();
    clock.tick(1000);
    await page.settle();
    assert.equal(loading(), null);
    assert.ok(page.$('#inspector-body [data-detail-for="T"]'));
  });
}

test('an expanded card shows its loading pane state only once its body has been missing for 300 ms', async () => {
  const clock = fakeClock();
  const page = loadPage({ url: '/document', fetch: server(), beforeScripts: (w) => { w.setTimeout = clock.setTimeout; w.clearTimeout = clock.clearTimeout; } });
  await page.settle();
  snapshot(page, { rows: [SPEC], bodies: {} });
  await page.settle();
  page.$('.node-toggle[data-node-id="S"]').click();
  await page.settle();
  const loading = () => page.$('#doc-node-S [data-pane-state="loading"]');
  clock.tick(299);
  await page.settle();
  assert.equal(loading(), null);
  clock.tick(1);
  await page.settle();
  assert.ok(loading());
});
