import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, jsonResponse, type } from './dom.mjs';

const SPEC = { id: 'S', kind: 'spec', title: 'A spec', status: 'READY', display: 'READY', parent: null, ordinal: 0, priority: 50, target_repo: null };
const SPEC_BODY = {
  node: SPEC,
  sections: [{ key: 'brief', header: '## Brief', content: 'What it is.', ordinal: 1 }],
  dependency_details: [],
  dependent_details: [],
};
const STATIC_DATA = { rows: {}, edges: [], statuses: [], bodies: {}, decisions: [] };

function shownView(page) {
  const shown = (id) => !page.$(`#${id}`).classList.contains('hidden');
  if (shown('decisions-pane')) return 'decisions';
  if (shown('document-pane')) return 'document';
  if (!shown('graph-pane')) return null;
  return shown('waves-pane') ? 'waves' : 'graph';
}

function url(page) {
  const { pathname, search, hash } = page.window.location;
  return pathname + search + hash;
}

function isWavesRequest(call) {
  return call.url.startsWith('/api/meta') || call.url.startsWith('/api/waves');
}

async function withSpec(page) {
  page.socket.open();
  const subscribe = page.socket.sent.at(-1);
  page.socket.message({ type: 'snapshot', re: subscribe && subscribe.id, rows: [SPEC], bodies: { S: SPEC_BODY }, statuses: [], decisions_open: 0 });
  await page.settle();
}

test('/ opens Document, and the URL stays /', async () => {
  const page = loadPage({ url: '/' });
  await page.settle();
  assert.equal(shownView(page), 'document');
  assert.equal(page.run('currentMode'), 'document');
  assert.ok(page.$('#view-doc-btn').classList.contains('bg-zinc-800'));
  assert.equal(url(page), '/');
});

test('the switcher reads Document, Graph, Waves, Decisions', () => {
  const page = loadPage();
  const segments = page.$('#view-doc-btn').parentNode.children;
  assert.deepEqual(segments.map((b) => b.getAttribute('aria-label')), ['Document view', 'Graph view', 'Waves view', 'Decisions view']);
});

test('a view switch pushes /graph, a filter change replaces it and keeps the path, and Back restores the view before', async () => {
  const page = loadPage({ url: '/' });
  await page.settle();
  const { history } = page.window;

  page.$('#view-graph-btn').click();
  await page.settle();
  assert.equal(url(page), '/graph');
  assert.equal(history.length, 2, 'the switch is a new history entry');
  assert.equal(shownView(page), 'graph');

  type(page.$('#search-box'), 'Auth');
  await page.settle();
  assert.equal(url(page), '/graph?q=auth');
  assert.equal(history.length, 2, 'a filter change replaces the entry');

  history.back();
  await page.settle();
  assert.equal(url(page), '/');
  assert.equal(shownView(page), 'document');
  assert.equal(page.run('filters.q'), '', 'the entry Back lands on carries its own filters');
  assert.equal(page.$('#search-box').value, '');

  history.forward();
  await page.settle();
  assert.equal(shownView(page), 'graph');
  assert.equal(page.run('filters.q'), 'auth');
  assert.equal(page.$('#search-box').value, 'auth');
});

test('loading /waves?status=REVIEWING restores the view and the filter, and subscribes with it', async () => {
  const page = loadPage({ url: '/waves?status=REVIEWING' });
  page.socket.open();
  await page.settle();
  assert.equal(shownView(page), 'waves');
  assert.equal(page.run("filters.statusMode.get('REVIEWING')"), 'include');
  assert.deepEqual(page.socket.sent.at(-1).filters, { status: 'REVIEWING' });
  assert.ok(page.fetchCalls.some((c) => c.url.startsWith('/api/waves')), 'Waves loads when it opens the page');
  assert.equal(url(page), '/waves?status=REVIEWING');
});

test('an old link with its filters in the hash loads them and is rewritten into the query', async () => {
  const page = loadPage({ url: '/#status=REVIEWING' });
  await page.settle();
  assert.equal(shownView(page), 'document');
  assert.equal(page.run("filters.statusMode.get('REVIEWING')"), 'include');
  assert.equal(url(page), '/?status=REVIEWING');
  assert.equal(page.window.history.length, 1, 'rewritten in place');
});

test('an old hash link typed over the open page applies its filters and is rewritten too', async () => {
  const page = loadPage({ url: '/graph' });
  await page.settle();
  page.window.location.hash = '#status=REVIEWING';
  page.window.dispatchEvent(new page.window.Event('popstate'));
  await page.settle();
  assert.equal(page.run("filters.statusMode.get('REVIEWING')"), 'include');
  assert.equal(url(page), '/graph?status=REVIEWING');
});

test('an unknown view segment opens Document', async () => {
  const page = loadPage({ url: '/nope/X' });
  await page.settle();
  assert.equal(shownView(page), 'document');
  assert.equal(page.run('readLocation().id'), null);
});

test('Waves makes no request and pends no load bar before it is shown, even as statuses change', async () => {
  const page = loadPage({ url: '/' });
  await withSpec(page);
  page.socket.message({ type: 'update', items: [{ op: 'decisions_open', count: 1 }] });
  type(page.$('#search-box'), 'a');
  await page.settle();
  assert.deepEqual(page.fetchCalls.filter(isWavesRequest), []);
  assert.equal(page.$('#waves-content').innerHTML, '');

  page.$('#view-waves-btn').click();
  await page.settle();
  assert.deepEqual(page.fetchCalls.filter(isWavesRequest).map((c) => c.url.split('?')[0]), ['/api/meta', '/api/waves']);
});

test('a static export carries the path and query after #/ both ways', async () => {
  const page = loadPage({
    url: 'file:///tmp/export.html#/decisions?status=READY',
    beforeScripts: (w) => { w.STATIC_DATA = STATIC_DATA; },
  });
  await page.settle();
  assert.equal(shownView(page), 'decisions');
  assert.equal(page.run("filters.statusMode.get('READY')"), 'include');

  page.$('#view-graph-btn').click();
  await page.settle();
  assert.equal(page.window.location.hash, '#/graph?status=READY');
  assert.equal(page.window.location.pathname, '/tmp/export.html', 'the file itself never changes');
  assert.equal(page.window.history.length, 2);
  assert.equal(page.run("pathFor('graph', 'S')"), '#/graph/S');

  page.window.location.hash = '#/document';
  page.window.dispatchEvent(new page.window.Event('hashchange'));
  await page.settle();
  assert.equal(shownView(page), 'document');
  assert.equal(page.run('filters.statusMode.size'), 0);

  page.window.history.back();
  await page.settle();
  assert.equal(shownView(page), 'decisions');
});

test('a refused history write still switches the view', async () => {
  const page = loadPage({
    url: 'file:///tmp/export.html',
    beforeScripts: (w) => { w.STATIC_DATA = STATIC_DATA; },
  });
  await page.settle();
  page.window.history.pushState = () => { throw new Error('SecurityError'); };
  page.$('#view-graph-btn').click();
  await page.settle();
  assert.equal(shownView(page), 'graph');
});

test('clicking the current view segment scrolls its pane to the top, collapses it, clears the selection and keeps the entry', async () => {
  const page = loadPage({ url: '/' });
  await withSpec(page);
  page.$('.spec-header[data-node-id="S"]').click();
  await page.settle();
  page.run("selectNode('S')");
  await page.settle();
  page.$('#document-pane').scrollTop = 240;
  page.$('[data-group-id="S::sections"]').click();
  await page.settle();
  assert.equal(page.run('expandedIds.has("S")'), true);
  assert.equal(page.run('collapsedGroups.size'), 1, 'the sections group is open, away from its default');

  page.$('#view-doc-btn').click();
  await page.settle();
  assert.equal(page.$('#document-pane').scrollTop, 0);
  assert.equal(page.run('expandedIds.size'), 0);
  assert.equal(page.run('collapsedGroups.size'), 0);
  assert.equal(page.$('[data-group-id="S::sections"]'), null, 'the spec is collapsed again');
  assert.equal(page.run('selectedNodeId'), null);
  assert.equal(shownView(page), 'document');
  assert.equal(url(page), '/');
  assert.equal(page.window.history.length, 1, 'a reset replaces the entry');
});

test('clicking the Decisions segment while on it drops the selected decision from the path', async () => {
  const decision = { id: 'decision-D1', title: 'Pick one', status: 'OPEN', priority: 50, created_at: '2026-10-01T10:00:00+00:00', waiting_count: 0 };
  const page = loadPage({
    url: '/',
    fetch: async (u) => (u.startsWith('/api/decisions?')
      ? jsonResponse(200, { items: [decision], next: null, counts: { open: 1, answered: 0, withdrawn: 0 } })
      : undefined),
  });
  page.$('#view-decisions-btn').click();
  await page.settle();
  page.$('.dec-row[data-decision-id="decision-D1"]').click();
  await page.settle();
  assert.equal(url(page), '/decisions/decision-D1');
  assert.equal(page.window.history.length, 3, 'opening a decision is a new entry');
  page.$('#decisions-list').scrollTop = 120;

  page.$('#view-decisions-btn').click();
  await page.settle();
  assert.equal(url(page), '/decisions');
  assert.equal(page.window.history.length, 3);
  assert.equal(page.run('selectedDecisionId'), null);
  assert.equal(page.$('.dec-row[aria-current]'), null);
  assert.equal(page.$('#decisions-list').scrollTop, 0);
});

test('Back from a selected decision clears it, and Forward selects it again', async () => {
  const decision = { id: 'decision-D1', title: 'Pick one', status: 'OPEN', priority: 50, created_at: '2026-10-01T10:00:00+00:00', waiting_count: 0 };
  const page = loadPage({
    url: '/decisions',
    fetch: async (u) => (u.startsWith('/api/decisions?')
      ? jsonResponse(200, { items: [decision], next: null, counts: { open: 1, answered: 0, withdrawn: 0 } })
      : undefined),
  });
  await page.settle();
  page.$('.dec-row[data-decision-id="decision-D1"]').click();
  await page.settle();
  page.window.history.back();
  await page.settle();
  assert.equal(page.run('selectedDecisionId'), null);
  page.window.history.forward();
  await page.settle();
  assert.equal(page.run('selectedDecisionId'), 'decision-D1');
  assert.ok(page.$('.dec-row[aria-current]'));
});
