import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './dom.mjs';

function row(fields) {
  return {
    status: 'READY', display: 'READY', phase: 'QUEUED', ordinal: 0, priority: 50, child_count: 0,
    acceptable_models: ['m-1'], target_repo: '.', lease: null, review: false, fix: false, merge: 'parent',
    ...fields,
  };
}

function body(node) {
  return {
    node: { review_cycles: 0, merge_attempts: 0, step_failures: 0, requires: [], land_order: [], base_chain: ['MAIN'], frontmatter: {}, ...node },
    display: node.display, phase: node.phase,
    sections: [
      { key: 'objective', header: '## Objective', content: `Why ${node.id}.`, ordinal: 1 },
      { key: 'acceptance', header: '## Acceptance', content: `When ${node.id} is done.`, ordinal: 2 },
    ],
    dependency_details: [{ id: 'X', kind: 'task', title: 'Landed work', status: 'COMPLETED', finished: true }],
    dependent_details: [],
    verifications: [{ id: 1, verification_type: 'test_command', target_path: `check-${node.id}`, expected_pattern: `pytest -k ${node.id}` }],
    conditions: [], jobs: [], lease: null,
  };
}

const S = row({ id: 'S', kind: 'spec', title: 'A spec', parent: null, child_count: 2 });
const P1 = row({ id: 'P1', kind: 'plan', title: 'The first plan', parent: 'S', child_count: 2 });
const P2 = row({ id: 'P2', kind: 'plan', title: 'The second plan', parent: 'S', ordinal: 1, child_count: 1 });
const T1 = row({ id: 'T1', kind: 'task', title: 'First task', parent: 'P1' });
const T2 = row({ id: 'T2', kind: 'task', title: 'Second task', parent: 'P1', ordinal: 1 });
const T3 = row({ id: 'T3', kind: 'task', title: 'Third task', parent: 'P2' });
const NODES = ['S', 'P1', 'P2', 'T1', 'T2', 'T3'];

// The server's side of the socket: each subscribe is answered with the rows its open set
// reveals and the bodies of the ids it watches.
function liveServer(rows) {
  const answered = new Set();
  const visible = (open) => {
    const out = [];
    const walk = (parent) => rows.filter(r => r.parent === parent).forEach((r) => {
      out.push(r);
      if (open.includes(r.id)) walk(r.id);
    });
    walk(null);
    return out;
  };
  return async function answer(page) {
    for (let i = 0; i < 20; i++) {
      await page.settle();
      const last = page.socket.sent.at(-1);
      if (!last || last.type !== 'subscribe' || answered.has(last.id)) return;
      answered.add(last.id);
      const bodies = Object.fromEntries(last.watch.map(id => rows.find(r => r.id === id)).filter(Boolean).map(r => [r.id, body(r)]));
      page.socket.message({ type: 'snapshot', re: last.id, rows: visible(last.open), bodies, statuses: [], decisions_open: 0 });
    }
    throw new Error('the page kept subscribing');
  };
}

async function live(rows = [S, P1, P2, T1, T2, T3]) {
  const page = loadPage();
  page.answer = liveServer(rows);
  page.socket.open();
  await page.answer(page);
  return page;
}

async function click(page, el) {
  el.click();
  await page.answer(page);
}

const doc = (page) => page.$('#unified-document');
const nodeOpen = (page, id) => {
  const toggle = doc(page).querySelector(`.node-toggle[data-node-id="${id}"]`);
  return toggle ? toggle.getAttribute('aria-expanded') : null;
};
const opened = (page, ids) => Object.fromEntries(ids.map(id => [id, nodeOpen(page, id)]));
const groupsIn = (page, id) => doc(page).querySelector(`#doc-node-${id}`).querySelectorAll('.disclosure[data-group-id], .section-toggle')
  .filter(el => (el.getAttribute('data-group-id') || el.getAttribute('data-section-id')).startsWith(`${id}::`));
const states = (els) => [...new Set(els.map(el => el.getAttribute('aria-expanded')))];
const toggleOf = (page, id) => doc(page).querySelector(`[data-expand-all="${id}"]`);

test('the global toggle opens every node, group and section, and closes them all again, its aria-expanded following', async () => {
  const page = await live();
  const btn = page.$('#expand-all-btn');
  assert.equal(btn.getAttribute('aria-expanded'), 'false');
  assert.equal(btn.getAttribute('aria-label'), 'Expand all');
  assert.ok(btn.querySelector('use[href="#icon-chevrons-up-down"]'));

  await click(page, btn);
  assert.deepEqual(opened(page, NODES), Object.fromEntries(NODES.map(id => [id, 'true'])));
  for (const id of NODES) {
    const kinds = groupsIn(page, id).map(el => el.getAttribute('data-group-id') || el.getAttribute('data-section-id'));
    assert.ok(kinds.includes(`${id}::sections`) && kinds.includes(`${id}::objective`) && kinds.includes(`${id}::verification::1`), `${id} draws its groups`);
    assert.deepEqual(states(groupsIn(page, id)), ['true'], `every group, check and section of ${id} is open`);
    assert.equal(toggleOf(page, id).getAttribute('aria-expanded'), 'true');
  }
  assert.equal(doc(page).querySelectorAll('.section-body.hidden').length, 0);
  assert.deepEqual(page.$$('#tree-list [role="treeitem"][aria-expanded]').map(r => r.getAttribute('aria-expanded')), ['true', 'true', 'true']);
  assert.equal(btn.getAttribute('aria-expanded'), 'true');
  assert.equal(btn.getAttribute('aria-label'), 'Collapse all');
  assert.ok(btn.querySelector('use[href="#icon-chevrons-down-up"]'));

  await click(page, btn);
  assert.deepEqual(opened(page, NODES), { S: 'false', P1: null, P2: null, T1: null, T2: null, T3: null });
  assert.equal(doc(page).querySelectorAll('.disclosure[data-group-id], .section-toggle').length, 0);
  assert.equal(page.run('expandedIds.size + collapsedGroups.size + toggledSections.size + expandAllState.size'), 0);
  assert.equal(btn.getAttribute('aria-expanded'), 'false');
  assert.equal(btn.getAttribute('aria-label'), 'Expand all');

  // A node opened by hand afterwards shows its groups' own defaults again.
  await click(page, doc(page).querySelector('.node-toggle[data-node-id="S"]'));
  assert.equal(doc(page).querySelector('[data-group-id="S::sections"]').getAttribute('aria-expanded'), 'true');
  assert.equal(doc(page).querySelector('[data-group-id="S::children"]').getAttribute('aria-expanded'), 'false');
  assert.equal(doc(page).querySelector('[data-section-id="S::objective"]').getAttribute('aria-expanded'), 'false');
});

test('a plan\'s control opens only that plan\'s subtree, and Collapse all closes everything under it while it stays open', async () => {
  const page = await live();
  await click(page, doc(page).querySelector('.node-toggle[data-node-id="S"]'));
  await click(page, doc(page).querySelector('[data-group-id="S::children"]'));
  const plan = toggleOf(page, 'P1');
  assert.equal(plan.getAttribute('aria-expanded'), 'false');
  assert.equal(plan.getAttribute('aria-label'), 'Expand all in P1');

  await click(page, plan);
  assert.deepEqual(opened(page, NODES), { S: 'true', P1: 'true', P2: 'false', T1: 'true', T2: 'true', T3: null });
  for (const id of ['P1', 'T1', 'T2']) assert.deepEqual(states(groupsIn(page, id)), ['true'], `${id} is open throughout`);
  assert.equal(doc(page).querySelector('[data-group-id="S::dependencies"]').getAttribute('aria-expanded'), 'false', 'the spec keeps its own defaults');
  assert.equal(doc(page).querySelector('[data-section-id="S::objective"]').getAttribute('aria-expanded'), 'false');
  assert.equal(toggleOf(page, 'P1').getAttribute('aria-expanded'), 'true');
  assert.equal(toggleOf(page, 'P1').getAttribute('aria-label'), 'Collapse all in P1');
  assert.ok(toggleOf(page, 'P1').querySelector('use[href="#icon-chevrons-down-up"]'));
  assert.equal(toggleOf(page, 'S').getAttribute('aria-expanded'), 'false');
  assert.equal(toggleOf(page, 'P2').getAttribute('aria-expanded'), 'false');
  assert.equal(page.$('#expand-all-btn').getAttribute('aria-expanded'), 'false', 'the global toggle covers only its own click');
  const treeToggles = page.$$('#tree-list [data-expand-all]').map(b => [b.getAttribute('data-expand-all'), b.getAttribute('aria-expanded')]);
  assert.deepEqual(treeToggles, [['S', 'false'], ['P1', 'true'], ['P2', 'false']], 'a tree row with children carries the toggle, a task row none');

  await click(page, toggleOf(page, 'P1'));
  assert.deepEqual(opened(page, NODES), { S: 'true', P1: 'true', P2: 'false', T1: 'false', T2: 'false', T3: null });
  assert.deepEqual(states(groupsIn(page, 'P1')), ['false'], 'every group and section under the plan is closed');
  assert.equal(doc(page).querySelector('[data-group-id="S::sections"]').getAttribute('aria-expanded'), 'true', 'the spec keeps its own defaults');
  assert.equal(toggleOf(page, 'P1').getAttribute('aria-expanded'), 'false');
  assert.equal(toggleOf(page, 'P1').getAttribute('aria-label'), 'Expand all in P1');
});

test('the state survives a live update without re-collapsing, and a row the update adds opens too', async () => {
  const page = await live();
  await click(page, page.$('#expand-all-btn'));
  await click(page, doc(page).querySelector('.node-toggle[data-node-id="T2"]'));
  await click(page, doc(page).querySelector('[data-group-id="T1::dependencies"]'));
  assert.equal(nodeOpen(page, 'T2'), 'false');

  const T4 = row({ id: 'T4', kind: 'task', title: 'A task the update adds', parent: 'P2', ordinal: 1 });
  page.answer = liveServer([S, P1, { ...P2, child_count: 2 }, T1, T2, T3, T4]);
  page.socket.message({
    type: 'update',
    items: [
      { op: 'row', row: { ...T1, status: 'IMPLEMENTING', display: 'IMPLEMENTING' } },
      { op: 'row', row: { ...P2, child_count: 2 } },
      { op: 'row', row: T4 },
      { op: 'section', id: 'T3', key: 'context', section: { header: '## Context', content: 'Added live.', ordinal: 3 } },
    ],
  });
  await page.answer(page);

  assert.deepEqual(opened(page, [...NODES, 'T4']), { S: 'true', P1: 'true', P2: 'true', T1: 'true', T2: 'false', T3: 'true', T4: 'true' });
  assert.equal(doc(page).querySelector('[data-group-id="T1::dependencies"]').getAttribute('aria-expanded'), 'false', 'a group closed by hand stays closed');
  assert.deepEqual(states(groupsIn(page, 'T1').filter(el => el.getAttribute('data-group-id') !== 'T1::dependencies')), ['true']);
  assert.deepEqual(states(groupsIn(page, 'T4')), ['true']);
  assert.equal(page.$('#expand-all-btn').getAttribute('aria-expanded'), 'true');
});

test('the toggle shows in Document and Graph only, and resetting the view puts it back to Expand all', async () => {
  const page = await live();
  const btn = page.$('#expand-all-btn');
  await click(page, btn);
  const shown = {};
  for (const [view, id] of [['graph', 'view-graph-btn'], ['waves', 'view-waves-btn'], ['decisions', 'view-decisions-btn'], ['document', 'view-doc-btn']]) {
    await click(page, page.$(`#${id}`));
    shown[view] = !btn.classList.contains('hidden');
  }
  assert.deepEqual(shown, { graph: true, waves: false, decisions: false, document: true });
  assert.equal(btn.getAttribute('aria-expanded'), 'true');

  await click(page, page.$('#view-doc-btn'));
  assert.equal(btn.getAttribute('aria-expanded'), 'false');
  assert.equal(nodeOpen(page, 'S'), 'false');
});

test('a clicked toggle leaves no tooltip behind once it redraws, and keyboard focus names the next action', async () => {
  const page = await live();
  const tip = page.$('#tm-tooltip');
  const Event = page.window.Event;
  toggleOf(page, 'S').dispatchEvent(new Event('mouseover'));
  assert.equal(tip.textContent, 'Expand all in S');

  toggleOf(page, 'S').dispatchEvent(new Event('pointerdown'));
  await click(page, toggleOf(page, 'S'));
  assert.ok(tip.classList.contains('hidden'), 'the tooltip of the replaced button is gone');
  toggleOf(page, 'S').dispatchEvent(new Event('focusin'));
  assert.ok(tip.classList.contains('hidden'), 'focus a click gives opens no tooltip');

  page.document.dispatchEvent(new Event('keydown', { key: 'Tab' }));
  toggleOf(page, 'S').dispatchEvent(new Event('focusin'));
  assert.equal(tip.classList.contains('hidden'), false);
  assert.equal(tip.textContent, 'Collapse all in S');
});

test('revealing a node under Expand all keeps every ancestor\'s children open, one closed by hand included', async () => {
  const page = await live();
  await click(page, page.$('#expand-all-btn'));
  await click(page, doc(page).querySelector('[data-group-id="P1::children"]'));
  assert.equal(doc(page).querySelector('[data-group-id="P1::children"]').getAttribute('aria-expanded'), 'false');

  page.run("openNode('T1')");
  await page.answer(page);

  for (const id of ['S', 'P1']) {
    assert.equal(doc(page).querySelector(`[data-group-id="${id}::children"]`).getAttribute('aria-expanded'), 'true', `${id}'s children stay open`);
  }
  let hiddenAbove = false;
  for (let el = doc(page).querySelector('#doc-node-T1'); el; el = el.parentNode) {
    if (el.classList && el.classList.contains('hidden')) hiddenAbove = true;
  }
  assert.equal(hiddenAbove, false, 'the revealed card shows');
  assert.equal(nodeOpen(page, 'T1'), 'true');
});
