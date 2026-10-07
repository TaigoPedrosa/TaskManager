import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { loadPage } from './dom.mjs';

const REPO_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '../..');
const BEAT = new Date(Date.now() - 8000).toISOString();

function row(fields) {
  return {
    status: 'READY', display: 'READY', phase: 'QUEUED', ordinal: 0, priority: 50, child_count: 0,
    acceptable_models: ['m-1'], target_repo: '.', lease: null, review: true, fix: true, merge: 'parent', parent: null,
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

// The counts tree exactly as the server builds it from these rows.
function countsTree(rows) {
  const out = execFileSync('uv', ['run', '--project', REPO_ROOT, '--quiet', 'python', '-c',
    'import json, sys; from taskmanager.web.rows import statuses; print(json.dumps(statuses(json.load(sys.stdin))))'],
  { input: JSON.stringify(rows), encoding: 'utf8' });
  return JSON.parse(out);
}

// [plan id, stored status, display, the display's label]
const STARTED = [
  ['P-WR', 'IMPLEMENTED', 'WAITING_REVIEW', 'Waiting Review'],
  ['P-RV', 'REVIEWING', 'REVIEWING', 'Reviewing'],
  ['P-FX', 'FIXING', 'FIXING', 'Fixing'],
  ['P-MG', 'MERGING', 'MERGING', 'Merging'],
  ['P-CP', 'COMPLETED', 'COMPLETED', 'Completed'],
  ['P-FL', 'FAILED', 'FAILED', 'Failed'],
];
const REVIEW_LEASE = { agent_id: 'wf-rv', action: 'review' };

const ROWS = {};
const BODIES = {};
function add(r, parts) {
  ROWS[r.id] = r;
  BODIES[r.id] = body(r, parts);
}

STARTED.forEach(([id, status, display], i) => {
  const lease = id === 'P-RV' ? REVIEW_LEASE : null;
  add(row({ id, kind: 'plan', title: `Plan ${id}`, ordinal: i, status, display, child_count: 1, lease }),
    lease ? { lease: { ...lease, acquired_at: BEAT, last_heartbeat: BEAT } } : {});
  add(row({ id: `${id}-T`, kind: 'task', title: `Task of ${id}`, parent: id, status: 'COMPLETED', display: 'COMPLETED', phase: 'DONE' }));
});
add(row({ id: 'P-RD', kind: 'plan', title: 'A plan whose tasks run', ordinal: 10, display: 'IMPLEMENTING', child_count: 2 }));
add(row({ id: 'P-RD-T1', kind: 'task', title: 'Done', parent: 'P-RD', status: 'COMPLETED', display: 'COMPLETED', phase: 'DONE' }));
add(row({ id: 'P-RD-T2', kind: 'task', title: 'Running', parent: 'P-RD', ordinal: 1, status: 'IMPLEMENTING', display: 'IMPLEMENTING', phase: 'DISPATCHED' }));
add(row({ id: 'S', kind: 'spec', title: 'A spec in review', ordinal: 11, status: 'REVIEWING', display: 'REVIEWING', child_count: 1, lease: REVIEW_LEASE }),
  { lease: { ...REVIEW_LEASE, acquired_at: BEAT, last_heartbeat: BEAT } });
add(row({ id: 'S-P', kind: 'plan', title: 'Its plan', parent: 'S', status: 'COMPLETED', display: 'COMPLETED', child_count: 1 }));
add(row({ id: 'S-P-T', kind: 'task', title: 'Its task', parent: 'S-P', status: 'COMPLETED', display: 'COMPLETED', phase: 'DONE' }));

const STATIC_DATA = { rows: ROWS, edges: [], statuses: countsTree(ROWS), bodies: BODIES, decisions: [] };

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

async function open(route) {
  const page = loadPage({
    url: `file:///tmp/export.html#${route}`,
    beforeScripts: (w) => { w.STATIC_DATA = STATIC_DATA; fakeVis(w); },
  });
  await page.settle();
  return page;
}

const text = (el) => el.textContent.replace(/\s+/g, ' ').trim();
// A row's cells as it reads them, one space apart.
const cells = (el) => el.children.map(text).filter(Boolean).join(' ');
const childrenGroup = (page, id) => page.$(`#doc-node-${id} [data-detail-for="${id}"] > [data-group="children"]`);

for (const [id, , display, label] of STARTED) {
  test(`a plan in ${display} ends its Tasks with its own step row, drawn with the status icon its label names`, async () => {
    const page = await open(`/document/${id}`);
    const group = childrenGroup(page, id);
    assert.equal(text(group.querySelector('.disclosure')), 'Tasks 2', 'the step is counted with the task');
    assert.equal(group.querySelectorAll('.own-step').length, 1);
    const step = group.querySelector('.own-step');
    assert.equal(step.parentNode.children.at(-1), step, 'the step row is last');
    assert.equal(step.parentNode.children.length, 2);
    const icon = step.querySelector('[role="img"]');
    assert.equal(icon.getAttribute('title'), label);
    assert.equal(icon.getAttribute('aria-label'), label);
    assert.equal(step.querySelector('a.id-link').getAttribute('data-id'), id);
    assert.match(cells(step), new RegExp(`^${id} Plan review and landing${id === 'P-RV' ? ' \\d+s' : ''}$`));

    const done = display === 'COMPLETED' ? 2 : 1;
    const nm = page.$(`#doc-node-${id} .plan-line [aria-label="${done} of 2 done"]`);
    assert.equal(text(nm), `${done}/2`, 'the header n/m counts the step');
    assert.equal(nm.getAttribute('title'), null, 'no explaining tooltip');
  });
}

test('a READY plan whose tasks roll up to Implementing has no step row, and its n/m counts its tasks alone', async () => {
  const page = await open('/document/P-RD');
  const group = childrenGroup(page, 'P-RD');
  assert.equal(text(group.querySelector('.disclosure')), 'Tasks 2');
  assert.equal(page.$$('#doc-node-P-RD .own-step').length, 0);
  assert.ok(page.$('#doc-node-P-RD .plan-line [aria-label="1 of 2 done"]'));

  const graph = await open('/graph/P-RD');
  assert.equal(graph.$$('#tree-list [data-step-of]').length, 0);
  assert.equal(graph.$$('#inspector-body .own-step').length, 0);
});

test('a leased plan carries the pulse on its header line and on its step row, and the lease badge as its Lease fact', async () => {
  const page = await open('/document/P-RV');
  const pulse = page.$('#doc-node-P-RV .plan-line .lease-pulse');
  assert.equal(pulse.getAttribute('aria-label'), 'Reviewing · wf-rv');
  assert.match(text(pulse), /^\d+s$/, 'the heartbeat age');
  assert.equal(page.$('#doc-node-P-RV .own-step .lease-pulse').getAttribute('aria-label'), 'Reviewing · wf-rv');
  const lease = page.$('#doc-node-P-RV [data-detail-for="P-RV"] > .facts > .fact');
  assert.equal(text(lease.children[0]), 'Lease');
  assert.equal(lease.querySelector('.lease-badge [role="img"]').getAttribute('aria-label'), 'Reviewing');
  assert.equal(page.$$('#doc-node-P-WR .lease-pulse').length, 0);
});

test('a spec in its own review ends its Plans with "Spec review and landing"', async () => {
  const page = await open('/document/S');
  const group = childrenGroup(page, 'S');
  assert.equal(text(group.querySelector('.disclosure')), 'Plans 2');
  const step = group.querySelector('.own-step');
  assert.equal(step.parentNode.children.at(-1), step);
  assert.match(cells(step), /^S Spec review and landing \d+s$/);
  assert.equal(step.querySelector('[role="img"]').getAttribute('title'), 'Reviewing');
  assert.equal(page.$$('#doc-node-S .repo-pill').length, 0, 'the default repo draws no pill');
});

test('the tree and the drawer list the same step row, the tree n/m reads as the card does, and the row opens its container', async () => {
  const page = await open('/graph/P-WR');
  const tree = page.$('#tree-list');
  const planRow = tree.querySelector('[data-node-id="P-WR"]');
  assert.equal(text(planRow.querySelector('[aria-label="1 of 2 done"]')), '1/2');
  const after = [...tree.children].slice(tree.children.indexOf(planRow) + 1, tree.children.indexOf(planRow) + 3);
  assert.equal(after[0].getAttribute('data-node-id'), 'P-WR-T');
  assert.equal(after[1].getAttribute('data-step-of'), 'P-WR', 'the step row follows the plan\'s tasks');
  assert.equal(after[1].getAttribute('role'), 'treeitem');
  assert.equal(after[1].querySelector('[role="img"]').getAttribute('title'), 'Waiting Review');
  assert.equal(cells(after[1]), 'P-WR Plan review and landing');

  const drawerGroup = page.$('#inspector-body [data-group="children"]');
  assert.equal(text(drawerGroup.querySelector('.disclosure')), 'Tasks 2');
  assert.equal(cells(drawerGroup.querySelector('.own-step')), 'P-WR Plan review and landing');

  page.$('#inspector-close-btn').click();
  await page.settle();
  assert.equal(page.window.location.hash, '#/graph');
  page.$('#tree-list [data-step-of="P-WR"]').click();
  await page.settle();
  assert.equal(page.window.location.hash, '#/graph/P-WR');
  assert.equal(page.$('#graph-inspector').getAttribute('data-detail-root'), 'P-WR');
  assert.equal(page.$('#graph-inspector').classList.contains('hidden'), false);
});
