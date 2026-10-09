import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { loadPage } from './dom.mjs';

const REPO_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '../..');

function row(fields) {
  return {
    status: 'READY', display: 'READY', phase: 'QUEUED', ordinal: 0, priority: 50, child_count: 0,
    acceptable_models: ['m-1'], target_repo: '.', lease: null, review: true, fix: true, merge: 'parent',
    parent: null, archived: false, ...fields,
  };
}

// S-OLD finished long enough ago to be archived; S-NEW is current work.
const DONE = { status: 'COMPLETED', display: 'COMPLETED' };
const OLD = [
  row({ id: 'S-OLD', kind: 'spec', title: 'Finished spec', child_count: 3, archived: true, ...DONE }),
  ...[1, 2, 3].map((i) => row({ id: `S-OLD-T${i}`, kind: 'task', title: `Old task ${i}`, parent: 'S-OLD', ordinal: i, archived: true, ...DONE })),
];
const NEW = [
  row({ id: 'S-NEW', kind: 'spec', title: 'Current spec', child_count: 2 }),
  row({ id: 'S-NEW-T1', kind: 'task', title: 'New task 1', parent: 'S-NEW', ordinal: 1 }),
  row({ id: 'S-NEW-T2', kind: 'task', title: 'New task 2', parent: 'S-NEW', ordinal: 2, ...DONE }),
];

const byId = (rows) => Object.fromEntries(rows.map((r) => [r.id, r]));

// The counts tree exactly as the server builds it from these rows.
function countsTree(rows) {
  const out = execFileSync('uv', ['run', '--project', REPO_ROOT, '--quiet', 'python', '-c',
    'import json, sys; from taskmanager.web.rows import statuses; print(json.dumps(statuses(json.load(sys.stdin))))'],
  { input: JSON.stringify(byId(rows)), encoding: 'utf8' });
  return JSON.parse(out);
}

const DEFAULT_STATUSES = countsTree(NEW);
const ARCHIVED_STATUSES = countsTree(OLD);

function snapshotOf(page, rows, statuses) {
  const status = {};
  // A container in a work cycle counts as work too (rows.py's counts_as_work).
  const work = (r) => r.kind === 'task' || (r.child_count > 0 && !['READY', 'DEFERRED', 'ABANDONED', 'SUPERSEDED'].includes(r.status));
  rows.filter(work).forEach((r) => { status[r.display] = (status[r.display] || 0) + 1; });
  return {
    type: 'snapshot', re: page.socket.sent.at(-1).id, rows: rows.filter((r) => r.parent === null),
    statuses, facets: { status }, bodies: {}, decisions_open: 0,
  };
}

const DEFAULT = (page) => snapshotOf(page, NEW, DEFAULT_STATUSES);
const ARCHIVED = (page) => snapshotOf(page, OLD, ARCHIVED_STATUSES);
const NOTHING_ARCHIVED = (page) => snapshotOf(page, [], []);

async function live(url = '/') {
  const page = loadPage({ url });
  page.socket.open();
  return page;
}

async function answer(page, snapshot) {
  page.socket.message(snapshot(page));
  await page.settle();
}

const toggle = (page) => page.$('#archive-btn');
const pressed = (page) => toggle(page).getAttribute('aria-pressed');
const lastFilters = (page) => page.socket.sent.at(-1).filters;
const allWork = (page) => page.$('#stats-digest [data-status-code="__all__"]').getAttribute('aria-label');
const completedChip = (page) => page.$('#stats-digest [data-status-code="COMPLETED"]').textContent;
const docRoots = (page) => page.$('#unified-document').children.map((el) => el.getAttribute('data-detail-root')).filter(Boolean);
const docText = (page) => page.$('#unified-document').textContent;
const address = (page) => page.window.location.pathname + page.window.location.search;

test('toggling Archive on subscribes with only and shows only the archived specs and their counts', async () => {
  const page = await live();
  await answer(page, DEFAULT);
  assert.equal(pressed(page), 'false');
  assert.equal(lastFilters(page).archived, undefined);

  toggle(page).click();
  assert.equal(lastFilters(page).archived, 'only');
  assert.equal(address(page), '/?archive=1');
  await answer(page, ARCHIVED);

  assert.equal(pressed(page), 'true');
  assert.deepEqual(docRoots(page), ['S-OLD']);
  assert.equal(allWork(page), 'All work: 4');
  assert.equal(completedChip(page), '4');
  assert.match(docText(page), /4 completed/);
});

test('toggling Archive off subscribes for the default view and restores its counts', async () => {
  const page = await live();
  await answer(page, DEFAULT);
  toggle(page).click();
  await answer(page, ARCHIVED);

  toggle(page).click();
  assert.equal(lastFilters(page).archived, undefined);
  assert.equal(address(page), '/');
  await answer(page, DEFAULT);

  assert.equal(pressed(page), 'false');
  assert.deepEqual(docRoots(page), ['S-NEW']);
  assert.equal(allWork(page), 'All work: 2');
  assert.match(docText(page), /1 ready · 1 completed/);
});

test('?archive=1 on load turns the archive on before the first subscribe', async () => {
  const page = await live('/?archive=1');
  assert.equal(page.socket.sent.length, 1);
  assert.equal(lastFilters(page).archived, 'only');
  await answer(page, ARCHIVED);
  assert.equal(pressed(page), 'true');
  assert.equal(address(page), '/?archive=1');
  assert.deepEqual(docRoots(page), ['S-OLD']);
});

test('the archive keeps the other filters in the URL beside it', async () => {
  const page = await live('/graph?status=COMPLETED');
  await answer(page, DEFAULT);
  toggle(page).click();
  assert.equal(address(page), '/graph?status=COMPLETED&archive=1');
  assert.deepEqual(lastFilters(page), { status: 'COMPLETED', archived: 'only' });
});

test('clicking the pressed toggle again resets the view to its default', async () => {
  const page = await live('/?archive=1');
  await answer(page, ARCHIVED);
  toggle(page).click();
  await page.settle();
  assert.equal(pressed(page), 'false');
  assert.equal(address(page), '/');
  assert.equal(lastFilters(page).archived, undefined);
});

test('with nothing archived the archive shows its empty state', async () => {
  const page = await live('/?archive=1');
  await answer(page, NOTHING_ARCHIVED);
  assert.equal(allWork(page), 'All work: 0');
  assert.match(docText(page), /No archived specs\./);
});

test('a search with nothing archived matching still says nothing matches', async () => {
  const page = await live('/?archive=1&q=zzz');
  await answer(page, NOTHING_ARCHIVED);
  assert.match(docText(page), /No specs, plans or tasks match\./);
});

test('below sm the closed Filters toggle counts the archive', async () => {
  const page = await live('/?archive=1');
  await answer(page, ARCHIVED);
  assert.equal(page.$('#filters-toggle-btn .filters-toggle-label').textContent, 'Filters (1)');
});

function exported(route, archivedStatuses = ARCHIVED_STATUSES, rows = [...OLD, ...NEW]) {
  return loadPage({
    url: `file:///tmp/export.html#${route}`,
    beforeScripts: (w) => {
      w.STATIC_DATA = { rows: byId(rows), edges: [], statuses: DEFAULT_STATUSES, archived_statuses: archivedStatuses, bodies: {}, decisions: [] };
    },
  });
}

test('the static export filters its own rows and counts by the archive and keeps it in the hash', async () => {
  const page = exported('/document');
  await page.settle();
  assert.deepEqual(docRoots(page), ['S-NEW']);
  assert.equal(allWork(page), 'All work: 2');

  toggle(page).click();
  await page.settle();
  assert.equal(page.window.location.hash, '#/document?archive=1');
  assert.deepEqual(docRoots(page), ['S-OLD']);
  assert.equal(allWork(page), 'All work: 4');
  assert.match(docText(page), /4 completed/);

  toggle(page).click();
  await page.settle();
  assert.equal(page.window.location.hash, '#/document');
  assert.deepEqual(docRoots(page), ['S-NEW']);
  assert.match(docText(page), /1 ready · 1 completed/);
});

test('the static export opens on the archive from #/…?archive=1', async () => {
  const page = exported('/document?archive=1');
  await page.settle();
  assert.equal(pressed(page), 'true');
  assert.deepEqual(docRoots(page), ['S-OLD']);
});

test('the static export with nothing archived shows the empty state', async () => {
  const page = exported('/document?archive=1', [], NEW);
  await page.settle();
  assert.match(docText(page), /No archived specs\./);
});
