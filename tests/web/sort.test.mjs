import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { loadPage, type } from './dom.mjs';

const REPO_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '../..');
const BEAT = new Date(Date.now() - 6000).toISOString();

function row(fields) {
  return {
    status: 'READY', display: 'READY', phase: 'QUEUED', ordinal: 0, priority: 50, child_count: 0,
    acceptable_models: ['m-1'], target_repo: '.', lease: null, review: true, fix: true, merge: 'parent', parent: null,
    ...fields,
  };
}

// A plan, then one task per display, each task stored in the status it displays.
function plan(id, ordinal, fields, taskDisplays) {
  return [
    row({ id, kind: 'plan', title: `Plan ${id}`, ordinal, child_count: taskDisplays.length, ...fields }),
    ...taskDisplays.map((display, i) => row({
      id: `${id}-T${i + 1}`, kind: 'task', title: `Task ${i + 1} of ${id}`, parent: id, ordinal: i, status: display, display,
    })),
  ];
}

const done = (n) => Array(n).fill('COMPLETED');
const ready = (n) => Array(n).fill('READY');
const REVIEW = { agent_id: 'wf-rv', action: 'review' };

// Listed out of plan order, so only the comparator's own tie-break puts P-TIE-1 first. P-ONE and
// P-FOUR display the Implementing roll-up while no step of theirs runs.
const ROWS = [
  ...plan('P-RV', 7, { status: 'REVIEWING', display: 'REVIEWING', priority: 30, lease: REVIEW }, done(5)),
  ...plan('P-TIE-2', 5, {}, ready(2)),
  ...plan('P-TIE-1', 4, {}, ready(2)),
  ...plan('P-ONE', 6, { display: 'IMPLEMENTING' }, [...done(1), ...ready(4)]),
  ...plan('P-FOUR', 3, { display: 'IMPLEMENTING', priority: 70 }, [...done(4), ...ready(1)]),
  ...plan('P-DONE', 1, { status: 'COMPLETED', display: 'COMPLETED' }, done(2)),
  ...plan('P-SUP', 2, { status: 'SUPERSEDED', display: 'SUPERSEDED' }, ['SUPERSEDED']),
  row({ id: 'T-DONE', kind: 'task', title: 'A finished task', status: 'COMPLETED', display: 'COMPLETED' }),
  row({ id: 'T-RUN', kind: 'task', title: 'A running task', ordinal: 8, status: 'IMPLEMENTING', display: 'IMPLEMENTING', lease: { agent_id: 'wf-im', action: 'implement' } }),
];

const PROGRESS = ['P-RV', 'T-RUN', 'P-TIE-1', 'P-TIE-2', 'P-ONE', 'P-FOUR', 'T-DONE', 'P-DONE', 'P-SUP'];
const PRIORITY = ['P-FOUR', 'T-DONE', 'P-DONE', 'P-SUP', 'P-TIE-1', 'P-TIE-2', 'P-ONE', 'T-RUN', 'P-RV'];

const byId = (rows) => Object.fromEntries(rows.map((r) => [r.id, r]));

// The counts tree exactly as the server builds it from these rows.
function countsTree(rows) {
  const out = execFileSync('uv', ['run', '--project', REPO_ROOT, '--quiet', 'python', '-c',
    'import json, sys; from taskmanager.web.rows import statuses; print(json.dumps(statuses(json.load(sys.stdin))))'],
  { input: JSON.stringify(byId(rows)), encoding: 'utf8' });
  return JSON.parse(out);
}

function body(r) {
  return {
    node: { ...r, review_cycles: 0, merge_attempts: 0, step_failures: 0, requires: [], land_order: [], base_chain: ['MAIN'], frontmatter: {} },
    display: r.display, phase: r.phase,
    sections: [{ key: 'brief', header: '## Brief', content: 'What it is.', ordinal: 1 }],
    dependency_details: [], dependent_details: [], verifications: [], conditions: [], jobs: [], lease: null,
  };
}

const roots = ROWS.filter((r) => r.parent === null);
const STATUSES = countsTree(ROWS);
const BODIES = Object.fromEntries(roots.map((r) => [r.id, body(r)]));

// P-FOUR's last task lands and its own review starts; a new task arrives.
const P_FOUR_IN_REVIEW = { ...roots.find((r) => r.id === 'P-FOUR'), status: 'REVIEWING', display: 'REVIEWING', lease: REVIEW };
const T_NEW = row({ id: 'T-NEW', kind: 'task', title: 'A new task', ordinal: 9 });
const UPDATED_STATUSES = countsTree([
  ...ROWS.map((r) => (r.id === 'P-FOUR' ? P_FOUR_IN_REVIEW : r.id === 'P-FOUR-T5' ? { ...r, status: 'COMPLETED', display: 'COMPLETED' } : r)),
  T_NEW,
]);
const UPDATE = {
  type: 'update',
  items: [
    { op: 'row', row: P_FOUR_IN_REVIEW },
    { op: 'row', row: T_NEW },
    { op: 'statuses', spec: null, entry: UPDATED_STATUSES.find((s) => s.spec === null) },
  ],
};

function fakeVis(w) {
  class DataSet {
    constructor(items) { this.items = new Map(items.map((i) => [i.id, i])); }
    getIds() { return [...this.items.keys()]; }
    update(list) { list.forEach((i) => this.items.set(i.id, i)); }
    remove(ids) { ids.forEach((id) => this.items.delete(id)); }
  }
  class Network {
    constructor() { this.handlers = {}; }
    on(event, fn) { (this.handlers[event] ||= []).push(fn); }
    getBoundingBox() { return { left: 0, top: 0, right: 0, bottom: 0 }; }
    canvasToDOM(p) { return p; }
    fit() {}
    selectNodes() {}
    unselectAll() {}
    focus() {}
  }
  w.vis = { DataSet, Network };
}

async function live(url = '/') {
  const page = loadPage({ url, beforeScripts: fakeVis });
  page.socket.open();
  page.socket.message({ type: 'snapshot', re: page.socket.sent.at(-1).id, rows: roots, bodies: BODIES, statuses: STATUSES, decisions_open: 0 });
  await page.settle();
  return page;
}

async function exported(route) {
  const page = loadPage({
    url: `file:///tmp/export.html#${route}`,
    beforeScripts: (w) => {
      w.STATIC_DATA = { rows: byId(ROWS), edges: [], statuses: STATUSES, bodies: BODIES, decisions: [] };
      fakeVis(w);
    },
  });
  await page.settle();
  return page;
}

const docOrder = (page) => page.$('#unified-document').children.map((el) => el.getAttribute('data-detail-root')).filter(Boolean);
const treeOrder = (page) => page.$$('#tree-list [role="treeitem"]').map((el) => el.getAttribute('data-node-id'));
const url = (page) => page.window.location.pathname + page.window.location.search;
const segment = (page, sort) => page.$(`#sort-control [data-sort="${sort}"]`);
const pressed = (page) => page.$$('#sort-control [aria-pressed="true"]').map((b) => b.getAttribute('data-sort'));

test('Document and the tree read ongoing first, then least complete, complete and set aside, ties in plan order', async () => {
  const page = await live();
  assert.deepEqual(docOrder(page), PROGRESS);
  assert.deepEqual(pressed(page), ['progress']);
  assert.equal(page.$('#sort-control').getAttribute('role'), 'group');
  assert.equal(page.$('#sort-control').getAttribute('aria-label'), 'Sort');

  page.$('#view-graph-btn').click();
  await page.settle();
  assert.deepEqual(treeOrder(page), PROGRESS);
  assert.equal(page.$('#sort-control').classList.contains('hidden'), false);
});

test('Priority orders by priority, highest first, then plan order, and rides in ?sort=priority', async () => {
  const page = await live();
  segment(page, 'priority').click();
  await page.settle();
  assert.equal(url(page), '/?sort=priority');
  assert.deepEqual(docOrder(page), PRIORITY);
  assert.equal(page.window.history.length, 1, 'a sort change replaces the entry');

  type(page.$('#search-box'), 'p');
  await page.settle();
  assert.equal(url(page), '/?q=p&sort=priority', 'a filter change keeps the sort');
  assert.equal(page.socket.sent.at(-1).filters.sort, undefined, 'the store never sees the sort');

  const graph = await live('/graph?sort=priority');
  assert.deepEqual(pressed(graph), ['priority']);
  assert.deepEqual(treeOrder(graph), PRIORITY);
  segment(graph, 'progress').click();
  await graph.settle();
  assert.equal(url(graph), '/graph');
  assert.deepEqual(treeOrder(graph), PROGRESS);
});

test('the static export sorts the same way from its rows and counts tree', async () => {
  assert.deepEqual(docOrder(await exported('/document')), PROGRESS);
  assert.deepEqual(docOrder(await exported('/document?sort=priority')), PRIORITY);
});

test('the sort control shows only on Document and Graph', async () => {
  const page = await live();
  page.$('#view-waves-btn').click();
  await page.settle();
  assert.equal(page.$('#sort-control').classList.contains('hidden'), true);
  page.$('#view-decisions-btn').click();
  await page.settle();
  assert.equal(page.$('#sort-control').classList.contains('hidden'), true);
  page.$('#view-doc-btn').click();
  await page.settle();
  assert.equal(page.$('#sort-control').classList.contains('hidden'), false);
});

test('the control leads the actions group at every width, which below sm is the Filters panel\'s last row after + New', async () => {
  const page = await live();
  const control = page.$('#sort-control');
  for (const width of [1440, 375, 768]) {
    page.window.innerWidth = width;
    page.window.dispatchEvent(new page.window.Event('resize'));
    assert.equal(control.parentNode.children[0], control, `at ${width}`);
    assert.equal(control.parentNode.children[1].id, 'toggle-sections-btn');
    assert.equal(control.parentNode.parentNode.id, 'filters-panel');
    assert.equal(control.parentNode.parentNode.children.at(-1), control.parentNode, 'the actions close the panel');
  }
});

test('a picked segment shows aria-pressed and the selected fill from the click, ahead of the re-sort', async () => {
  const page = await live();
  segment(page, 'priority').click();
  assert.equal(segment(page, 'priority').getAttribute('aria-pressed'), 'true');
  assert.ok(segment(page, 'priority').classList.contains('bg-zinc-800'));
  assert.equal(segment(page, 'progress').getAttribute('aria-pressed'), 'false');
  assert.equal(segment(page, 'progress').classList.contains('bg-zinc-800'), false);
  assert.deepEqual(docOrder(page), PROGRESS, 'the list has not re-sorted yet');
  await page.settle();
  assert.deepEqual(docOrder(page), PRIORITY);
});

test('clicking the picked segment again restores Progress, drops ?sort and scrolls the pane to the top', async () => {
  const page = await live();
  segment(page, 'priority').click();
  await page.settle();
  page.$('#document-pane').scrollTop = 240;

  segment(page, 'priority').click();
  await page.settle();
  assert.equal(page.run('sortOrder'), 'progress');
  assert.equal(url(page), '/');
  assert.deepEqual(docOrder(page), PROGRESS);
  assert.deepEqual(pressed(page), ['progress']);
  assert.equal(page.$('#document-pane').scrollTop, 0);
});

test('clicking the current view segment also returns the sort to Progress and drops ?sort', async () => {
  const page = await live('/?sort=priority');
  assert.deepEqual(docOrder(page), PRIORITY);
  page.$('#view-doc-btn').click();
  await page.settle();
  assert.equal(page.window.location.search, '');
  assert.deepEqual(pressed(page), ['progress']);
  assert.deepEqual(docOrder(page), PROGRESS);
});

test('a live update moving a plan into review moves it to the top, and keeps the selection, the scroll and the focus', async () => {
  const page = await live();
  page.run("selectNode('P-TIE-2')");
  await page.settle();
  assert.equal(url(page), '/document/P-TIE-2');
  page.$('#document-pane').scrollTop = 240;
  page.$('[data-group-id="P-TIE-2::sections"]').focus();

  page.socket.message(UPDATE);
  await page.settle();
  assert.deepEqual(docOrder(page), ['P-FOUR', 'P-RV', 'T-RUN', 'P-TIE-1', 'P-TIE-2', 'T-NEW', 'P-ONE', 'T-DONE', 'P-DONE', 'P-SUP']);
  assert.equal(page.run('selectedNodeId'), 'P-TIE-2');
  assert.equal(page.$('#doc-node-P-TIE-2').getAttribute('aria-selected'), 'true');
  assert.equal(page.$('#document-pane').scrollTop, 240);
  assert.equal(page.document.activeElement, page.$('[data-group-id="P-TIE-2::sections"]'));
  assert.equal(page.$('#doc-node-T-NEW').getAttribute('aria-selected'), null, 'the arriving row is not selected');
  assert.equal(url(page), '/document/P-TIE-2');
});

test('the tree re-sorts in place and keeps its scroll and its focused row', async () => {
  const page = await live('/graph');
  page.$('#tree-list').scrollTop = 120;
  page.$('#tree-list [data-node-id="P-ONE"]').focus();

  page.socket.message(UPDATE);
  await page.settle();
  assert.deepEqual(treeOrder(page).slice(0, 2), ['P-FOUR', 'P-RV']);
  assert.equal(page.$('#tree-list').scrollTop, 120);
  assert.equal(page.document.activeElement, page.$('#tree-list [data-node-id="P-ONE"]'));
  assert.equal(page.$('#tree-list [data-node-id="T-NEW"]').getAttribute('aria-selected'), 'false');
});
