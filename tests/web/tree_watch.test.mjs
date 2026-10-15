import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './dom.mjs';

function row(fields) {
  return {
    status: 'READY', display: 'READY', phase: 'QUEUED', ordinal: 0, priority: 50, child_count: 0,
    acceptable_models: [], target_repo: '.', lease: null, review: false, fix: false, merge: 'spec',
    ...fields,
  };
}

const SPEC = row({ id: 'S', kind: 'spec', title: 'Spec', parent: null, child_count: 1 });
const PLAN = row({ id: 'P', kind: 'plan', title: 'Plan', parent: 'S', child_count: 1 });
const TASK = row({ id: 'T', kind: 'task', title: 'Task', parent: 'P' });

async function open() {
  const page = loadPage({ url: '/document' });
  await page.settle();
  page.socket.open();
  page.socket.message({ type: 'snapshot', re: page.socket.sent.at(-1).id, rows: [SPEC, PLAN, TASK], bodies: {}, statuses: [], decisions_open: 0 });
  await page.settle();
  return page;
}

const watched = (page) => page.socket.sent.at(-1).watch;

test('an expanded row the server drops and sends back is watched again', async () => {
  const page = await open();
  page.run("expandId(tmStore.rows.get('T'))");
  assert.ok(watched(page).includes('T'));

  page.socket.message({ type: 'update', items: [{ op: 'drop', id: 'T' }] });
  await page.settle();
  assert.ok(!watched(page).includes('T'), 'the store forgets the watch of a dropped row');

  page.socket.message({ type: 'update', items: [{ op: 'row', row: TASK }] });
  await page.settle();
  assert.ok(watched(page).includes('T'));
});

test('a row that stays does not resubscribe each time it changes', async () => {
  const page = await open();
  page.run("expandId(tmStore.rows.get('T'))");
  const sent = page.socket.sent.length;
  page.socket.message({ type: 'update', items: [{ op: 'row', row: { ...TASK, title: 'Renamed' } }] });
  await page.settle();
  assert.equal(page.socket.sent.length, sent);
});

test('a dropped row that was never expanded is not watched when it returns', async () => {
  const page = await open();
  page.socket.message({ type: 'update', items: [{ op: 'drop', id: 'T' }] });
  page.socket.message({ type: 'update', items: [{ op: 'row', row: TASK }] });
  await page.settle();
  assert.ok(!watched(page).includes('T'));
});

test("collapsing a plan in the tree keeps the watch of the plan the drawer shows, and closing the drawer drops it", async () => {
  const page = await open();
  page.run("openDrawer('P')");
  assert.ok(watched(page).includes('P'));
  page.run("toggleExpand(tmStore.rows.get('P'))");
  assert.equal(page.run("expandedIds.has('P')"), false);
  assert.ok(watched(page).includes('P'), 'the drawer still shows P');
  page.run('closeDetailDrawer()');
  assert.ok(!watched(page).includes('P'));
});

test('collapsing a plan the drawer does not show unwatches it', async () => {
  const page = await open();
  page.run("expandId(tmStore.rows.get('P'))");
  assert.ok(watched(page).includes('P'));
  page.run("toggleExpand(tmStore.rows.get('P'))");
  assert.ok(!watched(page).includes('P'));
});

test('Collapse all keeps the drawer node watched', async () => {
  const page = await open();
  page.run("openDrawer('P')");
  page.run("expandId(tmStore.rows.get('S'))");
  page.run("collapseAllUnder('S')");
  assert.ok(watched(page).includes('P'));
});

test('a reset keeps the drawer node watched', async () => {
  const page = await open();
  page.run("openDrawer('P')");
  page.run('resetExpansion()');
  assert.ok(watched(page).includes('P'));
});
