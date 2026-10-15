import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, key } from './dom.mjs';

async function openRepoPopover() {
  const page = loadPage({ url: '/document' });
  await page.settle();
  page.socket.open();
  page.socket.message({
    type: 'snapshot', re: page.socket.sent.at(-1).id, rows: [], bodies: {}, statuses: [], decisions_open: 0,
    facets: { repo: { api: 2 } },
  });
  await page.settle();
  page.$('#repo-filter .tri-btn-main').click();
  return page;
}

const buttons = (page) => page.$$('#repo-filter .tri-row button[data-mode]');
const repoMode = (page) => page.run("filters.repoMode.get('api') || null");

test('Enter and Space on a popover row\'s Include and Exclude buttons are left to the button\'s own click', async () => {
  const page = await openRepoPopover();
  const [include, exclude] = buttons(page);
  for (const button of [include, exclude]) {
    for (const k of ['Enter', ' ']) {
      assert.equal(key(button, k).defaultPrevented, false, `${k} on ${button.dataset.mode}`);
    }
  }
  assert.equal(repoMode(page), null);
});

test('the buttons set their own mode on click', async () => {
  const page = await openRepoPopover();
  buttons(page)[1].click();
  assert.equal(repoMode(page), 'exclude');
});

test('Enter on a status chip still toggles it, and Shift+Enter excludes it', async () => {
  const page = await openRepoPopover();
  const chip = page.$('#stats-digest [data-status-code="READY"]');
  assert.equal(key(chip, 'a').defaultPrevented, false);
  assert.equal(page.run("filters.statusMode.get('READY') || null"), null);
  key(chip, 'Enter');
  assert.equal(page.run("filters.statusMode.get('READY') || null"), 'include');
  const again = page.$('#stats-digest [data-status-code="READY"]');
  again.dispatchEvent(new page.window.Event('keydown', { key: 'Enter', shiftKey: true }));
  assert.equal(page.run("filters.statusMode.get('READY') || null"), 'exclude');
});
