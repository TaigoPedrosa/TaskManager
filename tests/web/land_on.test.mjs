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

function body(node) {
  return {
    node: {
      review: false, fix: false, merge: 'spec', review_cycles: 0, merge_attempts: 0, step_failures: 0,
      requires: [], land_order: [], frontmatter: {}, ...node,
    },
    display: node.display, phase: node.phase,
    sections: [], dependency_details: [], dependent_details: [], verifications: [], conditions: [], jobs: [], lease: null,
  };
}

const SPEC = row({ id: 'S', kind: 'spec', title: 'A spec on a release branch', parent: null, child_count: 1 });
const PLAN = row({ id: 'P', kind: 'plan', title: 'A plan', parent: 'S', child_count: 1 });
const TASK = row({ id: 'T', kind: 'task', title: 'A task on its plan', parent: 'P', merge: 'parent' });
const ROWS = [SPEC, PLAN, TASK];
const BODIES = {
  S: body({ ...SPEC, base_chain: ['release/0.4'], frontmatter: { land_on: 'release/0.4' } }),
  P: body({ ...PLAN, base_chain: ['release/0.4'] }),
  T: body({ ...TASK, base_chain: ['P', 'release/0.4'] }),
};
const ROWS_BY_ID = Object.fromEntries(ROWS.map((r) => [r.id, r]));

// The API around the socket: rows and bodies by id, /api/meta with the merge targets it is
// handed, and a PATCH that records its body and answers with `patch`'s response.
function server({ mergeTargets = ['parent', 'spec'], patch = () => jsonResponse(200, {}) } = {}) {
  const patches = [];
  const fetch = async (url, opts = {}) => {
    const u = new URL(url, 'http://test');
    if (u.pathname === '/api/meta') return jsonResponse(200, { merge_targets: mergeTargets, plans: [], specs: [] });
    if (u.pathname === '/api/nodes' && u.searchParams.has('ids')) {
      const found = ROWS_BY_ID[u.searchParams.get('ids')];
      return jsonResponse(200, { items: found ? [found] : [], next: null });
    }
    const m = u.pathname.match(/^\/api\/nodes\/([^/]+)$/);
    if (m && opts.method === 'PATCH') {
      const sent = JSON.parse(opts.body);
      patches.push({ id: decodeURIComponent(m[1]), body: sent });
      return patch(sent);
    }
    if (m) {
      const found = BODIES[decodeURIComponent(m[1])];
      return found ? jsonResponse(200, found) : jsonResponse(404, { detail: 'Node not found' });
    }
    return undefined;
  };
  return { fetch, patches };
}

async function openAt(url, fetch) {
  const page = loadPage({ url, fetch });
  await page.settle();
  if (page.socket.readyState !== 1) page.socket.open();
  const subscribe = page.socket.sent.at(-1);
  page.socket.message({ type: 'snapshot', re: subscribe && subscribe.id, rows: ROWS, bodies: BODIES, statuses: [], decisions_open: 0 });
  await page.settle();
  return page;
}

function text(el) {
  return el.textContent.replace(/\s+/g, ' ').trim();
}

function lands(page, id) {
  const fact = page.$$(`#doc-node-${id} [data-detail-for="${id}"] > .facts > .fact`).find((f) => text(f.children[0]) === 'Lands');
  return fact && text(fact.children[1]);
}

async function openFlags(page, id) {
  page.$(`#doc-node-${id} .actions-btn`).click();
  page.$(`#doc-node-${id} .actions-menu [data-act="flags"]`).click();
  await page.settle();
  return page.$('#dialog-root [role="dialog"]');
}

async function save(page) {
  page.$('#dialog-root .dlg-submit').click();
  await page.settle();
}

test('the Lands fact names the branch the chain lands on, after the branches it builds on', async () => {
  const { fetch } = server();
  assert.equal(lands(await openAt('/document/T', fetch), 'T'), 'tm/P → release/0.4');
  assert.equal(lands(await openAt('/document/S', fetch), 'S'), 'release/0.4');
});

test("a spec's Flags dialog holds its land_on, has no merge choice, and writes the branch typed", async () => {
  const { fetch, patches } = server();
  const page = await openAt('/document/S', fetch);
  const dialog = await openFlags(page, 'S');
  assert.equal(dialog.querySelector('.fl-land-on').value, 'release/0.4');
  assert.equal(dialog.querySelector('.fl-merge'), null);
  type(dialog.querySelector('.fl-land-on'), ' release/0.5 ');
  await save(page);
  assert.deepEqual(patches, [{ id: 'S', body: { review: false, fix: false, requires: [], land_on: 'release/0.5', land_order: [] } }]);
  assert.equal(page.$('#dialog-root [role="dialog"]'), null, 'a saved dialog closes');
});

test("clearing a spec's Lands on unsets land_on, so the spec lands on its default branch", async () => {
  const { fetch, patches } = server();
  const page = await openAt('/document/S', fetch);
  type((await openFlags(page, 'S')).querySelector('.fl-land-on'), '');
  await save(page);
  assert.deepEqual(patches.map((p) => p.body), [{ review: false, fix: false, requires: [], frontmatter_unset: ['land_on'], land_order: [] }]);
});

test("a task's Flags dialog offers the API's merge_targets and writes the one picked, never land_on", async () => {
  const { fetch, patches } = server({ mergeTargets: ['parent', 'spec', 'elsewhere'] });
  const page = await openAt('/document/T', fetch);
  const dialog = await openFlags(page, 'T');
  assert.equal(dialog.querySelector('.fl-land-on'), null);
  const options = dialog.querySelectorAll('.fl-merge option');
  assert.deepEqual(options.map((o) => o.getAttribute('value')), ['parent', 'spec', 'elsewhere']);
  assert.deepEqual(options.filter((o) => o.hasAttribute('selected')).map((o) => o.getAttribute('value')), ['parent']);
  assert.equal(text(dialog).includes('Merge'), true);
  dialog.querySelector('.fl-merge').value = 'spec';
  await save(page);
  assert.deepEqual(patches.map((p) => p.body), [{ review: false, fix: false, requires: [], merge: 'spec' }]);
});

test('a refused branch keeps the dialog open with the text as typed and shows the refusal with Retry', async () => {
  const detail = "Nothing changed: S: land_on 'release..0.4' is not a branch name git accepts";
  const { fetch, patches } = server({ patch: () => jsonResponse(400, { detail }) });
  const page = await openAt('/document/S', fetch);
  type((await openFlags(page, 'S')).querySelector('.fl-land-on'), 'release..0.4');
  await save(page);
  assert.equal(patches.length, 1);
  const dialog = page.$('#dialog-root [role="dialog"]');
  assert.ok(dialog, 'the dialog stays open');
  assert.equal(dialog.querySelector('.fl-land-on').value, 'release..0.4');
  const toastEl = page.$('#toast-root .toast');
  assert.equal(toastEl.getAttribute('data-tone'), 'error');
  assert.match(text(toastEl), /land_on 'release\.\.0\.4' is not a branch name git accepts/);
  assert.ok(toastEl.querySelector('.toast-retry'));
});

test('below sm a toast keeps 16px off both edges, on every view', async () => {
  const page = await openAt('/document/S', server().fetch);
  const root = page.$('#toast-root');
  assert.ok(['right-4', 'max-sm:left-4'].every((c) => root.classList.contains(c)));
  page.run("liftToasts('empty'); liftToasts(null)");
  assert.ok(root.classList.contains('max-sm:left-4'), 'the decisions pane leaves it in place');
});
