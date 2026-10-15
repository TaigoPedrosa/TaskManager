import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, type } from './dom.mjs';

test('a live status push while a wave size is being typed keeps the text and the focus', async () => {
  const page = loadPage({ url: '/waves' });
  await page.settle();
  page.socket.open();
  page.socket.message({ type: 'snapshot', re: page.socket.sent.at(-1).id, rows: [], bodies: {}, statuses: [], decisions_open: 0 });
  await page.settle();

  const input = page.$('#wave-size-input');
  input.focus();
  type(input, '12');
  page.socket.message({ type: 'snapshot', rows: [], bodies: {}, statuses: [], decisions_open: 0 });
  await page.settle();

  const after = page.$('#wave-size-input');
  assert.equal(after.value, '12');
  assert.equal(page.document.activeElement, after);
});

test('an input the user is not in is redrawn from the committed size', async () => {
  const page = loadPage({ url: '/waves' });
  await page.settle();
  page.socket.open();
  page.socket.message({ type: 'snapshot', re: page.socket.sent.at(-1).id, rows: [], bodies: {}, statuses: [], decisions_open: 0 });
  await page.settle();

  const committed = page.$('#wave-size-input').value;
  page.$('#wave-size-input').value = '12';
  page.socket.message({ type: 'snapshot', rows: [], bodies: {}, statuses: [], decisions_open: 0 });
  await page.settle();
  assert.equal(page.$('#wave-size-input').value, committed);
});
