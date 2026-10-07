import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './dom.mjs';

const SPEC = { id: 'S', kind: 'spec', title: 'A spec', status: 'READY', display: 'READY', parent: null, ordinal: 0, priority: 50, target_repo: null };
const SPEC_BODY = {
  node: SPEC,
  sections: [
    { key: 'brief', header: '## Brief', content: 'What it is.', ordinal: 1 },
    { key: 'acceptance', header: '## Acceptance', content: 'What done means.', ordinal: 2 },
  ],
  dependency_details: [],
  dependent_details: [],
};

// The page on the Document view with one spec expanded, its two sections rendered from a
// live snapshot.
async function documentWithOneSpec() {
  const page = loadPage();
  page.socket.open();
  const subscribe = page.socket.sent.at(-1);
  page.socket.message({ type: 'snapshot', re: subscribe && subscribe.id, rows: [SPEC], bodies: { S: SPEC_BODY }, statuses: [], decisions_open: 0 });
  page.$('#view-doc-btn').click();
  page.run("expandedIds.add('S')");
  page.run('renderUnifiedDocument()');
  await page.settle();
  return page;
}

test('rendering the document again never grows the set of sections the toggle-all button acts on', async () => {
  const page = await documentWithOneSpec();
  assert.equal(page.$$('details[data-section-id]').length, 2, 'the spec\'s two sections rendered');
  for (let i = 0; i < 3; i++) page.run('renderUnifiedDocument()');
  assert.equal(page.run('allSectionIds.length'), 2);

  page.$('#toggle-sections-btn').click();
  await page.settle();
  assert.deepEqual(JSON.parse(page.run('JSON.stringify([...expandedSections].sort())')), ['S::acceptance', 'S::brief'], 'expand-all opens exactly the rendered sections');
  assert.ok(page.$$('details[data-section-id]').every((d) => d.open));
  assert.equal(page.$('#toggle-sections-btn').getAttribute('aria-label'), 'Collapse all sections');
});

test('the toggle-all-sections button shows only in the Document view', async () => {
  const page = await documentWithOneSpec();
  const hidden = () => page.$('#toggle-sections-btn').classList.contains('hidden');
  const shown = {};
  for (const [view, button] of [['waves', 'view-waves-btn'], ['graph', 'view-graph-btn'], ['document', 'view-doc-btn'], ['decisions', 'view-decisions-btn']]) {
    page.$(`#${button}`).click();
    await page.settle();
    shown[view] = !hidden();
  }
  assert.deepEqual(shown, { waves: false, graph: false, document: true, decisions: false });
});

test('a title, a model name and a lease agent render as text in the Document cards and the tree', async () => {
  const nasty = '<img src=x onerror=alert(1)>';
  const task = {
    id: 'T', kind: 'task', title: nasty, status: 'READY', display: 'IMPLEMENTING', parent: null, ordinal: 0, priority: 50,
    child_count: 0, acceptable_models: ['<b>model</b>'], lease: { agent_id: '<i>agent</i>', action: 'implement' },
  };
  const page = loadPage();
  page.socket.open();
  const subscribe = page.socket.sent.at(-1);
  page.socket.message({ type: 'snapshot', re: subscribe && subscribe.id, rows: [SPEC, task], bodies: {}, statuses: [], decisions_open: 0 });
  await page.settle();
  for (const root of ['#unified-document', '#tree-list']) {
    const el = page.$(root);
    assert.equal(el.querySelectorAll('img, b, i').length, 0, `${root} parsed an interpolated value as markup`);
    assert.ok(el.textContent.includes(nasty), `${root} shows the title as text`);
  }
  assert.ok(page.$('#unified-document').textContent.includes('<b>model</b>'));
  assert.equal(page.$('#unified-document .lease-pulse').getAttribute('aria-label'), 'Implementing · <i>agent</i>');
});
