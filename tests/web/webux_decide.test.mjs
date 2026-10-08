import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, jsonResponse } from './dom.mjs';

// The owner's queue, driven through the page as it ships: three open decisions, newest first,
// and task T under a spec and a plan, held by the oldest of them.
function row(fields) {
  return {
    status: 'READY', display: 'READY', phase: 'QUEUED', ordinal: 0, priority: 50, child_count: 0,
    acceptable_models: [], target_repo: '.', lease: null, review: false, fix: false, merge: 'parent', waits_on: [],
    ...fields,
  };
}

function nodeBody(node, parts = {}) {
  return {
    node: { review_cycles: 0, merge_attempts: 0, step_failures: 0, requires: [], land_order: [], base_chain: ['MAIN'], frontmatter: {}, ...node },
    display: node.display, phase: node.phase,
    sections: [], dependency_details: [], dependent_details: [], verifications: [], conditions: [], jobs: [], lease: null,
    ...parts,
  };
}

const ROWS = [
  row({ id: 'S', kind: 'spec', title: 'Decisions UX', parent: null, child_count: 1, display: 'IMPLEMENTING' }),
  row({ id: 'P', kind: 'plan', title: 'Links both ways', parent: 'S', child_count: 1, display: 'IMPLEMENTING' }),
  row({ id: 'T', kind: 'task', title: 'Every held node links to its decision', parent: 'P', display: 'AWAITING_DECISION', waits_on: ['decision-D1'] }),
];
const NODE_BODIES = Object.fromEntries(ROWS.map((r) => [r.id, nodeBody(r)]));
NODE_BODIES.T = nodeBody(ROWS[2], {
  dependency_details: [{ id: 'decision-D1', kind: 'decision', title: 'Which chip colour?', status: 'OPEN', finished: false }],
});

const at = (minute) => `2026-10-07T10:${String(minute).padStart(2, '0')}:00+00:00`;
const option = (key, label, recommended = false) => ({ key, label, description: '', recommended, effect: 'none' });

function server() {
  const decisions = new Map([
    ['decision-D1', { title: 'Which chip colour?', created_at: at(1), status: 'OPEN', blocks: ['T'] }],
    ['decision-D2', { title: 'Which toast position?', created_at: at(2), status: 'OPEN', blocks: [] }],
    ['decision-D3', { title: 'Which drawer width?', created_at: at(3), status: 'OPEN', blocks: [] }],
  ]);
  const answers = [];
  const decisionBody = (id, d) => ({
    node: {
      id, kind: 'decision', title: d.title, status: d.status, display: d.status, priority: 50,
      frontmatter: { decision: { options: [option('a', 'The first', true), option('b', 'The second')], allow_custom: true, raised_by: null, answer: d.answer || null, custom_effect: 'none' } },
    },
    sections: [],
    dependent_details: d.blocks.map((n) => ({ id: n, kind: 'task', title: ROWS[2].title, status: d.status === 'OPEN' ? 'AWAITING_DECISION' : 'READY', finished: d.status !== 'OPEN' })),
  });
  const fetch = async (url, opts) => {
    const u = new URL(url, 'http://test');
    if (u.pathname === '/api/nodes' && u.searchParams.has('ids')) {
      const found = ROWS.find((r) => r.id === u.searchParams.get('ids'));
      return jsonResponse(200, { items: found ? [found] : [], next: null });
    }
    const node = u.pathname.match(/^\/api\/nodes\/([^/]+)$/);
    if (node) {
      const id = decodeURIComponent(node[1]);
      if (decisions.has(id)) return jsonResponse(200, decisionBody(id, decisions.get(id)));
      return NODE_BODIES[id] ? jsonResponse(200, NODE_BODIES[id]) : jsonResponse(404, { detail: 'Node not found' });
    }
    if (u.pathname === '/api/decisions') {
      const status = u.searchParams.get('status');
      const counts = { open: 0, answered: 0, withdrawn: 0 };
      decisions.forEach((d) => { counts[d.status.toLowerCase()] += 1; });
      const items = [...decisions].filter(([, d]) => d.status === status)
        .sort((a, b) => b[1].created_at.localeCompare(a[1].created_at))
        .map(([id, d]) => ({ id, title: d.title, status: d.status, priority: 50, created_at: d.created_at }));
      return jsonResponse(200, { items, next: null, counts });
    }
    const answer = u.pathname.match(/^\/api\/decisions\/([^/]+)\/answer$/);
    if (answer && opts && opts.method === 'POST') {
      const sent = JSON.parse(opts.body);
      answers.push([answer[1], sent.option]);
      Object.assign(decisions.get(answer[1]), { status: 'ANSWERED', answer: { ...sent, answered_by: 'Taígo Pedrosa', answered_at: at(30) } });
      return jsonResponse(200, { id: answer[1] });
    }
    return undefined;
  };
  return { fetch, answers };
}

function press(page, el, keyName, mods = {}) {
  const event = new page.window.Event('keydown', { key: keyName, ...mods });
  el.dispatchEvent(event);
  return event;
}

// Enter on a focused link or button is its click, as a browser synthesises it.
async function activate(page, el) {
  el.focus();
  el.click();
  await page.settle();
}

test('the owner answers two decisions in a row by keyboard alone, then follows a held node to its decision and back', async () => {
  const api = server();
  const page = loadPage({ url: '/document', fetch: api.fetch });
  await page.settle();
  page.socket.open();
  const subscribe = page.socket.sent.at(-1);
  page.socket.message({ type: 'snapshot', re: subscribe.id, rows: ROWS, bodies: NODE_BODIES, statuses: [{ spec: 'S', plans: [{ plan: 'P', counts: { AWAITING_DECISION: 1 } }] }], decisions_open: 3 });
  await page.settle();
  const focused = () => page.document.activeElement;
  const pathname = () => page.window.location.pathname;

  await activate(page, page.$('#view-decisions-btn'));
  assert.equal(pathname(), '/decisions/decision-D3', 'the newest open decision is selected');

  press(page, page.document.body, '1');
  press(page, focused(), 'Enter', { ctrlKey: true });
  await page.settle();
  assert.equal(pathname(), '/decisions/decision-D2');
  assert.equal(focused().getAttribute('data-option-key'), 'a', 'focus is on the next decision\'s first option');

  press(page, focused(), '1');
  press(page, focused(), 'Enter', { metaKey: true });
  await page.settle();
  assert.deepEqual(api.answers, [['decision-D3', 'a'], ['decision-D2', 'a']]);
  assert.equal(pathname(), '/decisions/decision-D1');
  assert.equal(focused().getAttribute('data-option-key'), 'a');

  const waiting = page.$('.dec-waiting-row a.id-link[data-id="T"]');
  await activate(page, waiting);
  assert.equal(pathname(), '/document/T', 'the node opens in the view the owner came from');
  assert.ok(focused() === page.$('#doc-node-T'), 'focus is on its revealed card');
  const dependencies = page.$('#doc-node-T [data-group="dependencies"]');
  assert.equal(dependencies.querySelector('.disclosure').getAttribute('aria-expanded'), 'true');

  await activate(page, dependencies.querySelector('a.id-link[data-decision]'));
  assert.equal(pathname(), '/decisions/decision-D1');
  assert.equal(page.$('#decisions-detail .dec-title').textContent, 'Which chip colour?');

  await activate(page, page.$('.dec-waiting-row a.id-link[data-id="T"]'));
  assert.equal(pathname(), '/document/T', 'and back to the node');
  assert.ok(focused() === page.$('#doc-node-T'));
});
