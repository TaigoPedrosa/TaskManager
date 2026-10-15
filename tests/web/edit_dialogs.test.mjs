import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, jsonResponse, type } from './dom.mjs';

function row(fields) {
  return {
    status: 'READY', display: 'READY', phase: 'QUEUED', ordinal: 0, priority: 50, child_count: 0,
    acceptable_models: [], target_repo: '.', lease: null, review: false, fix: false, merge: 'spec',
    ...fields,
  };
}

const SPEC = row({ id: 'S', kind: 'spec', title: 'Spec', parent: null, child_count: 1 });
const PLAN = row({ id: 'P', kind: 'plan', title: 'Plan', parent: 'S', child_count: 1 });
const TASK = row({ id: 'T', kind: 'task', title: 'Task', parent: 'P', merge: 'parent' });

function fullNode(r, extra = {}) {
  return { review_cycles: 0, merge_attempts: 0, step_failures: 0, requires: [], land_order: [], frontmatter: {}, ...r, ...extra };
}

// A live page whose socket has answered with `rows`; every non-socket request is answered by
// `handler(method, pathname, body)` and recorded in page.fetchCalls.
async function openPage(rows, handler = () => jsonResponse(200, {}), meta = {}) {
  const page = loadPage({
    url: '/',
    fetch: async (url, opts = {}) => {
      const u = new URL(url, 'http://test');
      if (u.pathname === '/api/meta') return jsonResponse(200, { merge_targets: ['parent', 'spec'], plans: [], specs: [], models: [], ...meta });
      return handler(opts.method || 'GET', u.pathname, opts.body ? JSON.parse(opts.body) : undefined, url);
    },
  });
  await page.settle();
  if (page.socket.readyState !== 1) page.socket.open();
  const subscribe = page.socket.sent.at(-1);
  page.socket.message({ type: 'snapshot', re: subscribe && subscribe.id, rows, bodies: {}, statuses: [], decisions_open: 0 });
  await page.settle();
  return page;
}

const writes = (page) => page.fetchCalls.filter((c) => c.method !== 'GET').map((c) => `${c.method} ${c.url}`);

async function click(page, selector) {
  page.$(selector).click();
  await page.settle();
}

test('a confirm cancelled for one node does not run its action when the same confirm is opened for another', async () => {
  const page = await openPage([SPEC]);
  page.run("removeSection({ id: 'A' }, 'notes')");
  await click(page, '#dialog-root .dlg-cancel');
  page.run("removeSection({ id: 'B' }, 'notes')");
  await click(page, '#dialog-root .dlg-submit');
  assert.deepEqual(writes(page), ['DELETE /api/nodes/B/sections/notes']);
});

test('reopening a cancelled edit form shows the node as it is now, not the values it was first opened with', async () => {
  const page = await openPage([SPEC]);
  const node = (priority, frontmatter) => `{ id: 'T', kind: 'task', title: 'Task', priority: ${priority}, frontmatter: ${JSON.stringify(frontmatter)} }`;
  page.run(`openEditNodeDialog(${node(50, { x: 'old' })})`);
  await click(page, '#dialog-root .dlg-cancel');
  page.run(`openEditNodeDialog(${node(90, { x: 'new' })})`);
  const dialog = page.$('#dialog-root [role="dialog"]');
  assert.equal(dialog.querySelector('.ed-priority').value, '90');
  assert.equal(dialog.querySelector('.fm-value').value, 'new');
  type(dialog.querySelector('.ed-title'), 'Renamed');
  await click(page, '#dialog-root .dlg-submit');
  const patch = page.fetchCalls.find((c) => c.method === 'PATCH');
  assert.equal(JSON.parse(patch.body).priority, 90);
});

test('an edit form that is reopened unchanged keeps what was typed', async () => {
  const page = await openPage([SPEC]);
  const open = () => page.run("openEditNodeDialog({ id: 'T', kind: 'task', title: 'Task', priority: 50, frontmatter: {} })");
  open();
  type(page.$('#dialog-root .ed-title'), 'Typed');
  await click(page, '#dialog-root .dlg-cancel');
  open();
  assert.equal(page.$('#dialog-root .ed-title').value, 'Typed');
});

test('the edit form re-sends only frontmatter rows the user changed, so strings that read as JSON and nulls survive', async () => {
  const page = await openPage([SPEC]);
  page.run(`openEditNodeDialog({ id: 'T', kind: 'task', title: 'Task', priority: 50,
    frontmatter: { version: '1.0', flag: 'true', gone: null, count: 3, edited: 'a' } })`);
  const rows = page.$$('#dialog-root .fm-row');
  type(rows.find((r) => r.querySelector('.fm-key').value === 'edited').querySelector('.fm-value'), '7');
  type(page.$('#dialog-root .ed-title'), 'Renamed');
  await click(page, '#dialog-root .dlg-submit');
  const body = JSON.parse(page.fetchCalls.find((c) => c.method === 'PATCH').body);
  assert.deepEqual(body.frontmatter_set, { edited: 7 });
});

test('a new frontmatter field is sent', async () => {
  const page = await openPage([SPEC]);
  page.run("openEditNodeDialog({ id: 'T', kind: 'task', title: 'Task', priority: 50, frontmatter: {} })");
  page.$('#dialog-root .fm-add').click();
  type(page.$('#dialog-root .fm-key'), 'k');
  type(page.$('#dialog-root .fm-value'), 'true');
  await click(page, '#dialog-root .dlg-submit');
  assert.deepEqual(JSON.parse(page.fetchCalls.find((c) => c.method === 'PATCH').body).frontmatter_set, { k: true });
});

test("Flags opened from a plan's list row reads the plan first, so the stored land order is not cleared", async () => {
  const page = await openPage([SPEC, PLAN, TASK], (method, pathname) => {
    if (method === 'GET' && pathname === '/api/nodes/P') return jsonResponse(200, { node: fullNode(PLAN, { land_order: ['T-2', 'T-1'] }) });
    return jsonResponse(200, {});
  });
  page.run('openFlagsDialog(tmStore.rows.get("P"))');
  await page.settle();
  assert.equal(page.$('#dialog-root .fl-land-order').value, 'T-2, T-1');
  await click(page, '#dialog-root .dlg-submit');
  assert.deepEqual(JSON.parse(page.fetchCalls.find((c) => c.method === 'PATCH').body).land_order, ['T-2', 'T-1']);
});

test("Flags opened from a spec's list row shows the branch the spec lands on", async () => {
  const page = await openPage([SPEC], (method, pathname) => (method === 'GET' && pathname === '/api/nodes/S'
    ? jsonResponse(200, { node: fullNode(SPEC, { frontmatter: { land_on: 'release/1' } }) })
    : jsonResponse(200, {})));
  page.run('openFlagsDialog(tmStore.rows.get("S"))');
  await page.settle();
  assert.equal(page.$('#dialog-root .fl-land-on').value, 'release/1');
});

test('Flags stays closed with the refusal in a toast when the plan cannot be read', async () => {
  const page = await openPage([SPEC, PLAN], () => jsonResponse(404, { detail: 'Node not found' }));
  page.run('openFlagsDialog(tmStore.rows.get("P"))');
  await page.settle();
  assert.equal(page.$('#dialog-root [role="dialog"]'), null);
  assert.deepEqual(writes(page), []);
});

// Creating a task selects it through its location, whose reveal reads the task's plan and spec
// from the server: neither is open in the store, and the task's row may never arrive.
const route = (page) => page.window.location.pathname;

async function createTask(page) {
  page.run('openNewTaskDialog()');
  await page.settle();
  type(page.$('#dialog-root .nt-title'), 'New one');
  await click(page, '#dialog-root .dlg-submit');
}

function chainServer(extra) {
  return async (method, pathname, body, url) => {
    if (method === 'POST' && pathname === '/api/tasks') return jsonResponse(200, { id: 'T' });
    if (method === 'GET' && pathname === '/api/nodes') {
      const found = { T: TASK, P: PLAN, S: SPEC }[new URL(url, 'http://test').searchParams.get('ids')];
      return jsonResponse(200, { items: found ? [found] : [], next: null });
    }
    return extra ? extra(method, pathname) : jsonResponse(200, {});
  };
}

test('a created task under closed containers is selected and its plan and spec are opened', async () => {
  const page = await openPage([SPEC], chainServer(), { plans: [{ id: 'P', title: 'Plan' }] });
  await createTask(page);
  assert.match(route(page), /\/T$/);
  const open = page.socket.sent.at(-1).open;
  assert.ok(open.includes('S') && open.includes('P'), `open set ${open}`);
});

test('a created task is selected once, and nothing selects it again when its row shows up later', async () => {
  const page = await openPage([SPEC], chainServer(), { plans: [{ id: 'P', title: 'Plan' }] });
  await createTask(page);
  page.run("selectNode('S')");
  await page.settle();
  const before = route(page);
  page.socket.message({ type: 'snapshot', rows: [SPEC, PLAN, TASK], bodies: {}, statuses: [], decisions_open: 0 });
  await page.settle();
  assert.equal(route(page), before);
});

test('an API validation failure reads as its messages, not as an object', async () => {
  const page = await openPage([SPEC], () => jsonResponse(422, { detail: [{ loc: ['body', 'priority'], msg: 'Input should be a valid integer', type: 'int_parsing' }] }));
  const message = await page.run("api('POST', '/api/specs', {}).then(() => '', (e) => e.message)");
  assert.equal(message, 'Input should be a valid integer');
});

test('a progress bar for a node whose tasks are all set aside draws finite segments', async () => {
  const page = await openPage([SPEC]);
  const html = page.run("progressBar({ DEFERRED: 3 })");
  assert.doesNotMatch(html, /Infinity|NaN/);
  assert.match(html, /width:100%/);
});
