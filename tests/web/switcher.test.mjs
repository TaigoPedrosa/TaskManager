import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './dom.mjs';

const SEGMENTS = ['view-doc-btn', 'view-graph-btn', 'view-waves-btn', 'view-decisions-btn'];

function paneVisibility(page) {
  const shown = (id) => !page.$(`#${id}`).classList.contains('hidden');
  return {
    document: shown('document-pane'),
    graph: shown('graph-pane'),
    waves: shown('graph-pane') && shown('waves-pane'),
    networkCanvas: shown('network-canvas'),
    sidebar: shown('sidebar-pane'),
    decisions: shown('decisions-pane'),
  };
}

// The selected segment carries the zinc-800 fill and white text; every other one carries neither.
function selectedSegments(page) {
  return SEGMENTS.filter((id) => {
    const cls = page.$(`#${id}`).classList;
    return cls.contains('bg-zinc-800') && cls.contains('text-white');
  });
}

const ONLY = {
  document: { document: true, graph: false, waves: false, networkCanvas: false, sidebar: false, decisions: false },
  graph: { document: false, graph: true, waves: false, networkCanvas: true, sidebar: true, decisions: false },
  waves: { document: false, graph: true, waves: true, networkCanvas: false, sidebar: false, decisions: false },
  decisions: { document: false, graph: false, waves: false, networkCanvas: false, sidebar: false, decisions: true },
};

test('the switcher opens on Document, and clicking each of the four segments shows its own pane and selects only itself', async () => {
  const page = loadPage();
  await page.settle();
  assert.deepEqual(paneVisibility(page), ONLY.document);
  assert.deepEqual(selectedSegments(page), ['view-doc-btn']);
  assert.equal(page.$('#expand-all-btn').classList.contains('hidden'), false);

  for (const [view, id] of [['graph', 'view-graph-btn'], ['waves', 'view-waves-btn'], ['decisions', 'view-decisions-btn'], ['document', 'view-doc-btn']]) {
    page.$(`#${id}`).click();
    await page.settle();
    assert.deepEqual(paneVisibility(page), ONLY[view], `${view} shows only its own pane`);
    assert.deepEqual(selectedSegments(page), [id]);
    assert.equal(page.$('#expand-all-btn').classList.contains('hidden'), !['document', 'graph'].includes(view), 'the expand-all toggle shows only in Document and Graph');
  }
  assert.ok(page.fetchCalls.some((c) => c.url.startsWith('/api/decisions')), 'opening Decisions fetches its data');
});

test('the switcher is one four-segment control: Document, Graph, Waves, Decisions in order, each named "<View> view"', () => {
  const page = loadPage();
  const switcher = page.$('#view-doc-btn').parentNode;
  assert.deepEqual(switcher.children.map((b) => b.id), SEGMENTS);
  assert.deepEqual(switcher.children.map((b) => b.getAttribute('title')), ['Document view', 'Graph view', 'Waves view', 'Decisions view']);
  assert.deepEqual(
    switcher.children.map((b) => b.querySelector('use').getAttribute('href')),
    ['#icon-file-text', '#icon-network', '#icon-layers', '#icon-help-circle'],
  );
  assert.ok(page.$('#view-decisions-btn #decisions-badge'), 'the badge sits on the Decisions segment');
});

test('the Decisions badge is hidden at zero and updates live off window.tmStore, capping at 99+', () => {
  const page = loadPage();
  const badge = page.$('#decisions-badge');
  assert.equal(badge.classList.contains('hidden'), true, 'no open decisions yet');

  page.socket.message({ type: 'update', items: [{ op: 'decisions_open', count: 2 }] });
  assert.equal(badge.classList.contains('hidden'), false);
  assert.equal(badge.textContent, '2');

  page.socket.message({ type: 'update', items: [{ op: 'decisions_open', count: 150 }] });
  assert.equal(badge.textContent, '99+');

  page.socket.message({ type: 'update', items: [{ op: 'decisions_open', count: 0 }] });
  assert.equal(badge.classList.contains('hidden'), true, 'back to zero hides it again');
});
