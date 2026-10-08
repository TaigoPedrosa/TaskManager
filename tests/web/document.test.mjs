import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './dom.mjs';

const SPEC = { id: 'S', kind: 'spec', title: 'A spec', status: 'READY', display: 'READY', parent: null, ordinal: 0, priority: 50, target_repo: null };

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
