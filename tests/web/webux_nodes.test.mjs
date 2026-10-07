import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { loadPage } from './dom.mjs';

// One estate, driven through the page's own scripts as a static export runs them: the store
// computes visibility and the toolbar's facets itself, and the counts tree is the server's.
const REPO_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '../..');
const BEAT = new Date(Date.now() - 6000).toISOString();
const LEASE = { agent_id: 'wf-tm-review-P', action: 'review' };

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

const ROWS = Object.fromEntries([
  row({ id: 'S', kind: 'spec', title: 'The spec', display: 'IMPLEMENTING', child_count: 2 }),
  row({ id: 'P', kind: 'plan', title: 'The plan in review', parent: 'S', status: 'REVIEWING', display: 'REVIEWING', phase: 'DISPATCHED', child_count: 2, lease: LEASE }),
  row({ id: 'P-T1', kind: 'task', title: 'First', parent: 'P', status: 'COMPLETED', display: 'COMPLETED', phase: 'DONE' }),
  row({ id: 'P-T2', kind: 'task', title: 'Second', parent: 'P', ordinal: 1, status: 'COMPLETED', display: 'COMPLETED', phase: 'DONE' }),
  row({ id: 'Q', kind: 'plan', title: 'The plan not started', parent: 'S', ordinal: 1, child_count: 1 }),
  row({ id: 'Q-T', kind: 'task', title: 'Waiting', parent: 'Q' }),
].map((r) => [r.id, r]));

const BODIES = Object.fromEntries(Object.values(ROWS).map((r) => [r.id, body(r)]));
BODIES.P = body(ROWS.P, {
  sections: [{ key: 'context', header: null, content: 'Why.', ordinal: 1 }],
  verifications: [{ id: 1, verification_type: 'test_command', target_path: 'joined', expected_pattern: 'node --test' }],
  lease: { ...LEASE, acquired_at: BEAT, last_heartbeat: BEAT },
});

const STATUSES = JSON.parse(execFileSync('uv', ['run', '--project', REPO_ROOT, '--quiet', 'python', '-c',
  'import json, sys; from taskmanager.web.rows import statuses; print(json.dumps(statuses(json.load(sys.stdin))))'],
{ input: JSON.stringify(ROWS), encoding: 'utf8' }));

function fakeVis(w) {
  class DataSet {
    constructor(items) { this.items = new Map(items.map((i) => [i.id, i])); }
    getIds() { return [...this.items.keys()]; }
    update(list) { list.forEach((i) => this.items.set(i.id, i)); }
    remove(ids) { ids.forEach((id) => this.items.delete(id)); }
  }
  class Network {
    on() {}
    getBoundingBox() { return { left: 0, top: 0, right: 0, bottom: 0 }; }
    canvasToDOM(p) { return p; }
    fit() {}
    selectNodes() {}
    unselectAll() {}
    focus() {}
  }
  w.vis = { DataSet, Network };
}

async function open(route = '') {
  const page = loadPage({
    url: `file:///tmp/export.html${route ? `#${route}` : ''}`,
    beforeScripts: (w) => {
      w.STATIC_DATA = { rows: ROWS, edges: [], statuses: STATUSES, bodies: BODIES, decisions: [] };
      fakeVis(w);
    },
  });
  await page.settle();
  return page;
}

const text = (el) => el.textContent.replace(/\s+/g, ' ').trim();
const cells = (el) => el.children.map(text).filter(Boolean).join(' ');
const chipCount = (page, code) => text(page.$(`#stats-digest [data-status-code="${code}"] strong`));

function groupsIn(page, scope) {
  return page.$$(`${scope} [data-detail-for="P"] > .detail-group`).map((g) => {
    const header = g.querySelector('.disclosure');
    return [g.getAttribute('data-group'), text(header), header.getAttribute('aria-expanded')];
  });
}

test('a plan in its own review, through the page: Document first, counted in the toolbar, found by its status, its step row, one detail layout', async () => {
  const home = await open();
  assert.equal(home.$('#document-pane').classList.contains('hidden'), false, 'the page opens on Document');
  assert.ok(home.$('#view-doc-btn').classList.contains('bg-zinc-800'));
  const all = home.$('#stats-digest [data-status-code="__all__"]');
  assert.equal(cells(all), 'All work 4');
  assert.equal(all.getAttribute('aria-label'), 'All work: 4');
  assert.equal(chipCount(home, 'REVIEWING'), '1', 'the plan in review is counted');
  assert.equal(chipCount(home, 'COMPLETED'), '2');
  assert.ok(home.$('#doc-node-S'));

  const reviewing = await open('/document?status=REVIEWING');
  assert.equal(chipCount(reviewing, 'REVIEWING'), '1');
  reviewing.$('.node-toggle[data-node-id="S"]').click();
  await reviewing.settle();
  assert.ok(reviewing.$('#doc-node-P'), 'the plan in review is shown');
  assert.equal(reviewing.$('#doc-node-Q'), null, 'the plan not started is not');

  const card = await open('/document/P');
  const line = card.$('#doc-node-P .plan-line');
  assert.equal(line.querySelector('[role="img"]').getAttribute('title'), 'Reviewing');
  assert.equal(text(line.querySelector('[aria-label="2 of 3 done"]')), '2/3');
  assert.equal(line.querySelector('.lease-pulse').getAttribute('aria-label'), 'Reviewing · wf-tm-review-P');
  const step = card.$('#doc-node-P [data-group="children"] .own-step');
  assert.equal(step.parentNode.children.at(-1), step);
  assert.equal(step.querySelector('[role="img"]').getAttribute('title'), 'Reviewing');
  assert.ok(text(step).includes('Plan review and landing'));
  assert.ok(step.querySelector('.lease-pulse'));

  const drawer = await open('/graph/P');
  const expected = [
    ['sections', 'Sections 1', 'true'],
    ['children', 'Tasks 3', 'false'],
    ['verifications', 'Verifications 1', 'false'],
  ];
  assert.deepEqual(groupsIn(card, '#doc-node-P'), expected);
  assert.deepEqual(groupsIn(drawer, '#inspector-body'), expected);
  assert.ok(drawer.$('#inspector-body [data-group="children"] .own-step [aria-label="Reviewing"]'));
});
