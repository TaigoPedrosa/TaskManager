import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, jsonResponse, type } from './dom.mjs';

const AT = '2026-10-08T10:00:00+00:00';
const REFUSAL = 'Nothing changed: refused';
const OPTION = { key: 'a', label: 'Take a', description: '', recommended: false, effect: 'none' };
const WAITING = { id: 'T', title: 'A task', kind: 'task', status: 'AWAITING_DECISION', finished: false };
const DECISIONS = {
  'decision-D1': { title: 'Pick one', status: 'OPEN', answer: null, dependents: [WAITING] },
  'decision-D2': {
    title: 'Picked one', status: 'ANSWERED', dependents: [],
    answer: { option: 'a', text: '', rationale: '', answered_by: 'owner', answered_at: AT },
  },
};

function decisionBody(id) {
  const d = DECISIONS[id];
  return {
    node: {
      id, kind: 'decision', title: d.title, status: d.status, display: d.status, frontmatter: {
        decision: {
          options: [OPTION], allow_custom: true, raised_by: null, answer: d.answer, custom_effect: 'none',
          withdrawn_reason: '', withdrawn_by: null, withdrawn_at: null,
        },
      },
    },
    sections: [],
    dependent_details: d.dependents,
  };
}

function row(fields) {
  return {
    status: 'READY', display: 'READY', phase: 'QUEUED', ordinal: 0, priority: 50, child_count: 0, acceptable_models: [],
    target_repo: '.', lease: null, review: false, fix: false, merge: 'parent', ...fields,
  };
}

const ROWS = [
  row({ id: 'S', kind: 'spec', title: 'A spec', parent: null, child_count: 1, merge: 'spec' }),
  row({ id: 'P', kind: 'plan', title: 'A plan', parent: 'S', child_count: 1 }),
  row({ id: 'T', kind: 'task', title: 'A task', parent: 'P' }),
];
const TASK_BODY = {
  node: {
    ...ROWS[2], review_cycles: 0, merge_attempts: 0, step_failures: 0, requires: [], land_order: [], base_chain: ['P', 'main'],
    frontmatter: { attachments: [{ asset: 'a1.png', name: 'shot.png', mime: 'image/png', size_bytes: 2048, source: { state: 'fresh' } }] },
  },
  display: 'READY', phase: 'QUEUED', sections: [], dependency_details: [], dependent_details: [], conditions: [], jobs: [], lease: null,
  verifications: [{ id: 1, verification_type: 'file_exists', target_path: 'src/app.py', expected_pattern: null }],
};

// Reads answer from the fixtures above; every write waits until `release` runs, then is refused.
function server() {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const fetch = async (url, opts) => {
    const u = new URL(url, 'http://test');
    if (opts && opts.method && opts.method !== 'GET') {
      await gate;
      return jsonResponse(409, { detail: REFUSAL });
    }
    if (u.pathname === '/api/decisions') {
      const status = u.searchParams.get('status').toUpperCase();
      const items = Object.entries(DECISIONS).filter(([, d]) => d.status === status)
        .map(([id, d]) => ({ id, title: d.title, status: d.status, priority: 50, created_at: AT }));
      return jsonResponse(200, { items, next: null, counts: { open: 1, answered: 1, withdrawn: 0 } });
    }
    if (u.pathname === '/api/nodes' && u.searchParams.has('ids')) {
      return jsonResponse(200, { items: ROWS.filter((r) => r.id === u.searchParams.get('ids')), next: null });
    }
    const node = u.pathname.match(/^\/api\/nodes\/([^/]+)$/);
    if (node && DECISIONS[node[1]]) return jsonResponse(200, decisionBody(node[1]));
    if (node && node[1] === 'T') return jsonResponse(200, TASK_BODY);
    return undefined;
  };
  return { fetch, release: () => release() };
}

function fakeVis(w) {
  class DataSet {
    constructor(items) { this.items = new Map(items.map((i) => [i.id, i])); }
    getIds() { return [...this.items.keys()]; }
    update(list) { list.forEach((i) => this.items.set(i.id, i)); }
    remove(ids) { ids.forEach((id) => this.items.delete(id)); }
  }
  class Network {
    on() {}
    getBoundingBox() { return { left: 10, top: 20, right: 180, bottom: 54 }; }
    canvasToDOM(p) { return p; }
    fit() {}
    selectNodes() {}
    unselectAll() {}
    focus() {}
  }
  w.vis = { DataSet, Network };
  w.FileReader = class {
    readAsDataURL() {
      this.result = 'data:image/png;base64,AAAA';
      this.onload();
    }
  };
}

async function open(url, fetch) {
  const page = loadPage({ url, fetch, beforeScripts: fakeVis });
  await page.settle();
  if (page.socket.readyState !== 1) page.socket.open();
  const subscribe = page.socket.sent.at(-1);
  page.socket.message({ type: 'snapshot', re: subscribe && subscribe.id, rows: ROWS, bodies: { T: TASK_BODY }, statuses: [], decisions_open: 1 });
  await page.settle();
  return page;
}

// The file input a pick opens, handed a file as the browser's picker would.
function pickFile(page, fire) {
  const inputs = [];
  const create = page.document.createElement.bind(page.document);
  page.document.createElement = (tag) => {
    const el = create(tag);
    if (tag === 'input') inputs.push(el);
    return el;
  };
  fire();
  inputs[0].files = [{ name: 'shot.png' }];
  inputs[0].dispatchEvent(new page.window.Event('change'));
}

// A control is focused while it fires, by the click or the key that fired it.
function press(control) {
  control.focus();
  control.click();
}

async function openGroup(page, group) {
  page.$(`#inspector-body [data-group-id="T::${group}"]`).click();
  await page.settle();
}

// Each write control, fired the way the owner fires it; each returns where focus belongs after
// the refusal: on the control, or on the field the write was fired from.
const CASES = [
  ['Answer', '/decisions/decision-D1', async (page) => {
    page.$('.dec-option-card').click();
    const submit = page.$('.dec-answer-submit');
    press(submit);
    return { control: submit, focus: submit };
  }],
  ['Answer by Ctrl+Enter from the rationale', '/decisions/decision-D1', async (page) => {
    page.$('.dec-option-card').click();
    const rationale = page.$('.dec-rationale');
    type(rationale, 'Because');
    rationale.focus();
    rationale.dispatchEvent(new page.window.Event('keydown', { key: 'Enter', ctrlKey: true }));
    return { control: page.$('.dec-answer-submit'), focus: rationale };
  }],
  ['Reopen', '/decisions/decision-D2', async (page) => {
    const reopen = page.$('.dec-reopen-btn');
    press(reopen);
    return { control: reopen, focus: reopen };
  }],
  ['remove ×', '/decisions/decision-D1', async (page) => {
    const remove = page.$('.dec-block-remove');
    press(remove);
    return { control: remove, focus: remove };
  }],
  ['Attach on a decision', '/decisions/decision-D1', async (page) => {
    const attach = page.$('#decisions-detail .att-add-btn');
    pickFile(page, () => press(attach));
    return { control: attach, focus: attach };
  }],
  ['Run all', '/graph/T', async (page) => {
    await openGroup(page, 'verifications');
    const run = page.$('#inspector-body [data-act="run-verifications"]');
    press(run);
    return { control: run, focus: run };
  }],
  ['Re-check', '/graph/T', async (page) => {
    await openGroup(page, 'attachments');
    const recheck = page.$('#inspector-body [data-act="recheck-attachments"]');
    press(recheck);
    return { control: recheck, focus: recheck };
  }],
  ['Attach file…', '/graph/T', async (page) => {
    const actions = page.$('#inspector-actions .actions-btn');
    pickFile(page, () => {
      press(actions);
      press(page.$('#inspector-actions [data-act="attach-file"]'));
    });
    return { control: actions, focus: actions };
  }],
];

for (const [name, url, fire] of CASES) {
  test(`a refused ${name} leaves focus where the owner corrects it, not on the page`, async () => {
    const { fetch, release } = server();
    const page = await open(url, fetch);
    const { control, focus } = await fire(page);
    await page.settle();
    assert.equal(control.disabled, true, 'the write is in flight');
    // A browser moves focus off a control the moment it is disabled.
    if (page.document.activeElement === control) page.document.activeElement = page.document.body;

    release();
    await page.settle();

    assert.equal(page.$('#toast-root .toast').getAttribute('data-tone'), 'error');
    assert.equal(control.disabled, false);
    assert.ok(page.document.activeElement === focus, page.document.activeElement.outerHTML.slice(0, 200));
  });
}
