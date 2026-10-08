import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, jsonResponse, key, type } from './dom.mjs';

const at = (minute) => `2026-10-06T10:${String(minute).padStart(2, '0')}:00+00:00`;

function opt(optionKey, label, extra = {}) {
  return { key: optionKey, label, description: `${label}, described.`, recommended: false, effect: 'none', ...extra };
}

function waitingNode(id, title, kind = 'task') {
  return { id, title, kind, status: 'AWAITING_DECISION', finished: false };
}

// The open decisions the frames draw, D43 the newest, plus one answered decision.
function frameDecisions() {
  return [
    {
      id: 'decision-D40', title: 'Which icon should an empty decisions list show?', created_at: at(40), status: 'OPEN',
      options: [opt('a', 'circle-dashed', { recommended: true }), opt('b', 'inbox')],
      dependents: [waitingNode('WEBUX-DECIDE-READ', 'Decision rows read at a glance')],
    },
    {
      id: 'decision-D41', title: 'Start plan WEBUX-DECIDE before WEBUX-NODES lands?', created_at: at(41), status: 'OPEN',
      options: [opt('a', 'Wait for WEBUX-NODES', { recommended: true }), opt('b', 'Start now')],
      dependents: [waitingNode('WEBUX-DECIDE', 'Answering a decision is fast and safe', 'plan')],
    },
    {
      id: 'decision-D42', title: 'WEBUX-NODES-KIT was DEFERRED: drop the edge, defer, or abandon the dependents?', created_at: at(42), status: 'OPEN',
      options: [
        opt('drop_edge', 'Drop the edge', { effect: 'drop_edge' }),
        opt('defer', 'Defer the dependents', { effect: 'defer' }),
        opt('abandon', 'Abandon the dependents', { effect: 'abandon', recommended: true }),
      ],
      dependents: [waitingNode('WEBUX-NODES-DETAIL', 'One detail renderer'), waitingNode('WEBUX-NODES-STEP', 'A started step is a row')],
    },
    {
      id: 'decision-D43', title: 'Which name should an answer given from the page carry?', created_at: at(43), status: 'OPEN',
      options: [
        opt('b', 'A name typed once per browser'),
        opt('a', 'The project’s git user.name', { recommended: true }),
        opt('c', 'The OS login name'),
        opt('d', 'Keep “web”'),
      ],
      dependents: [waitingNode('WEBUX-DECIDE-API', 'The decisions API says was blocking'), waitingNode('WEBUX-DECIDE-READ', 'Decision rows read at a glance')],
    },
    {
      id: 'decision-D39', title: 'Approve the WEBUX-DECIDE frames?', created_at: at(39), status: 'ANSWERED',
      options: [opt('approve', 'Approve', { recommended: true }), opt('redraw', 'Redraw')],
      answer: { option: 'approve', text: '', rationale: '', answered_by: 'Taígo Pedrosa', answered_at: at(50) },
      dependents: [],
    },
  ];
}

// The tasks + Add task can offer, in id order as the server sends them.
const TASKS = [
  { id: 'WEBUX-DECIDE-API', title: 'The decisions API says was blocking', display: 'AWAITING_DECISION' },
  { id: 'WEBUX-DECIDE-FLOW', title: 'Answering stays on Open and moves to the next decision', display: 'AWAITING_DECISION' },
  { id: 'WEBUX-DECIDE-LINKS', title: 'Every node waiting on a decision links to it', display: 'AWAITING_DECISION' },
  { id: 'WEBUX-DECIDE-READ', title: 'Decision rows read at a glance', display: 'AWAITING_DECISION' },
  { id: 'WEBUX-NODES-KIT', title: 'Shared renderers carry the design rules', display: 'DEFERRED' },
  { id: 'WEBUX-SHIP', title: 'Ship 0.3.5', display: 'BLOCKED_BY_TASK' },
];

// An in-memory /api for the pane. Writes apply to the decisions and are recorded; `hold()`
// keeps every write in flight until its release runs, `holdCandidates()` the same for the Add
// task picker's read, and `failNext` answers the next write with an error instead.
function fakeServer(decisions) {
  const byId = new Map(decisions.map((d) => [d.id, { custom_effect: 'none', ...d }]));
  const writes = [];
  const replies = [];
  const nodeFailures = new Map();
  let gate = null;
  let candidatesGate = null;
  const counts = () => {
    const c = { open: 0, answered: 0, withdrawn: 0 };
    byId.forEach((d) => { c[d.status.toLowerCase()] += 1; });
    return c;
  };
  const body = (d) => ({
    node: {
      id: d.id, kind: 'decision', title: d.title, status: d.status, display: d.status, frontmatter: {
        decision: {
          options: d.options, allow_custom: true, raised_by: null, answer: d.answer || null, custom_effect: d.custom_effect,
          withdrawn_reason: d.withdrawn_reason || '', withdrawn_by: d.withdrawn_by || null, withdrawn_at: d.withdrawn_at || null,
        },
      },
    },
    sections: [],
    dependent_details: d.dependents,
  });
  const fetch = async (url, opts) => {
    const method = (opts && opts.method) || 'GET';
    const node = url.match(/^\/api\/nodes\/([^/?]+)$/);
    if (node) {
      const failure = nodeFailures.get(node[1]);
      if (failure) return jsonResponse(failure, { detail: 'Bad Gateway' });
      const d = byId.get(node[1]);
      return d ? jsonResponse(200, body(d)) : jsonResponse(404, { detail: 'Node not found' });
    }
    const candidates = url.match(/^\/api\/decisions\/([^/]+)\/candidates$/);
    if (candidates) {
      if (candidatesGate) await candidatesGate;
      if (nodeFailures.get('candidates')) return jsonResponse(nodeFailures.get('candidates'), { detail: 'Bad Gateway' });
      const waiting = new Set(byId.get(candidates[1]).dependents.map((n) => n.id));
      return jsonResponse(200, { items: TASKS.filter((t) => !waiting.has(t.id)) });
    }
    if (url.startsWith('/api/decisions?')) {
      if (nodeFailures.get('list')) return jsonResponse(nodeFailures.get('list'), { detail: 'Bad Gateway' });
      const status = new URLSearchParams(url.split('?')[1]).get('status');
      const items = [...byId.values()].filter((d) => d.status === status)
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .map((d) => ({ id: d.id, title: d.title, status: d.status, priority: 50, created_at: d.created_at }));
      return jsonResponse(200, { items, next: null, counts: counts() });
    }
    const write = url.match(/^\/api\/decisions\/([^/]+)\/(answer|withdraw|reopen|blocks)$/);
    if (write && method === 'POST') {
      const sent = JSON.parse((opts && opts.body) || '{}');
      writes.push({ id: write[1], verb: write[2], body: sent });
      if (gate) await gate;
      const reply = replies.shift();
      if (reply) return jsonResponse(reply.status, { detail: reply.detail });
      const d = byId.get(write[1]);
      if (write[2] === 'answer') Object.assign(d, { status: 'ANSWERED', answer: { ...sent, answered_by: 'Taígo Pedrosa', answered_at: at(55) } });
      if (write[2] === 'withdraw') Object.assign(d, { status: 'WITHDRAWN', withdrawn_reason: sent.reason, withdrawn_by: 'Taígo Pedrosa', withdrawn_at: at(55) });
      if (write[2] === 'reopen') Object.assign(d, { status: 'OPEN', answer: null });
      if (write[2] === 'blocks') {
        d.dependents = d.dependents.filter((n) => !(sent.remove || []).includes(n.id))
          .concat((sent.add || []).map((id) => waitingNode(id, `${id} title`)));
      }
      return jsonResponse(200, { id: d.id });
    }
    return undefined;
  };
  return {
    fetch, byId, writes, nodeFailures, counts,
    hold() {
      let release;
      gate = new Promise((resolve) => { release = resolve; });
      return () => { gate = null; release(); };
    },
    holdCandidates() {
      let release;
      candidatesGate = new Promise((resolve) => { release = resolve; });
      return () => { candidatesGate = null; release(); };
    },
    failNext(status, detail) { replies.push({ status, detail }); },
  };
}

function pushOpenCount(page, server) {
  page.socket.message({ type: 'update', items: [{ op: 'decisions_open', count: server.counts().open }] });
}

async function openPage(server, { url = '/decisions', width = 1440, before } = {}) {
  const page = loadPage({
    url,
    fetch: server.fetch,
    beforeScripts: (w) => {
      w.innerWidth = width;
      if (before) before(w);
    },
  });
  page.socket.open();
  pushOpenCount(page, server);
  await page.settle();
  return page;
}

const path = (page) => page.window.location.pathname;
const selected = (page) => page.run('selectedDecisionId');
const rowIds = (page) => page.$$('#decisions-list .dec-row').map((r) => r.getAttribute('data-decision-id'));
const currentRow = (page) => {
  const row = page.$('.dec-row[aria-current="page"]');
  return row && row.getAttribute('data-decision-id');
};
const title = (page) => page.$('#decisions-detail .dec-title').textContent;
const card = (page, optionKey) => page.$(`.dec-answer-form .dec-option-card[data-option-key="${optionKey}"]`);
const checked = (page) => page.$$('.dec-answer-form .dec-option-card[aria-checked="true"]').map((c) => c.getAttribute('data-option-key'));
const focused = (page) => page.document.activeElement;
const submitBtn = (page) => page.$('.dec-answer-submit');
const dialog = (page) => page.$('#dialog-root [role="dialog"]');

// Element identity compared as a short description: a failed assert on two DOM nodes would
// otherwise print both whole trees.
const describe = (el) => (el ? `<${el.localName} ${el.getAttribute('class') ? `.${el.getAttribute('class').split(' ')[0]}` : ''}${el.getAttribute('data-option-key') ? `[${el.getAttribute('data-option-key')}]` : ''}${el.id ? `#${el.id}` : ''}>` : 'nothing');
function assertFocus(page, el, message = 'focus') {
  const now = page.document.activeElement;
  assert.ok(el && now === el, `${message}: focus is on ${describe(now)}, not ${describe(el)}`);
}

function press(page, el, keyName, mods = {}) {
  const event = new page.window.Event('keydown', { key: keyName, ...mods });
  el.dispatchEvent(event);
  return event;
}

async function select(page, id) {
  page.$(`.dec-row[data-decision-id="${id}"]`).click();
  await page.settle();
}

async function answerWith(page, optionKey) {
  card(page, optionKey).click();
  submitBtn(page).click();
  await page.settle();
}

async function withdraw(page, reason = '') {
  page.$('.dec-withdraw-btn').click();
  type(page.$('#dialog-root .wd-reason'), reason);
  page.$('#dialog-root .dlg-submit').click();
  await page.settle();
}

// Another session answers or withdraws: the record changes and the live open count drops,
// which is the only push the pane gets.
async function closeElsewhere(page, server, id, change) {
  Object.assign(server.byId.get(id), change);
  pushOpenCount(page, server);
  await page.settle();
}

function fakeClock() {
  let now = 0;
  let next = 0;
  const timers = new Map();
  return {
    setTimeout: (fn, ms = 0) => { timers.set(++next, { at: now + ms, fn }); return next; },
    clearTimeout: (id) => { timers.delete(id); },
    tick(ms) {
      now += ms;
      [...timers].sort((a, b) => a[1].at - b[1].at).forEach(([id, t]) => {
        if (t.at <= now && timers.delete(id)) t.fn();
      });
    },
  };
}


// Selection and the router -----------------------------------------------------------------

test('from sm up, /decisions selects the top open decision, newest first, in place of the bare path', async () => {
  const page = await openPage(fakeServer(frameDecisions()));
  assert.deepEqual(rowIds(page), ['decision-D43', 'decision-D42', 'decision-D41', 'decision-D40']);
  assert.equal(path(page), '/decisions/decision-D43');
  assert.equal(page.window.history.length, 1, 'resolved in place, so Back never lands on the bare path');
  assert.equal(currentRow(page), 'decision-D43');
  assert.equal(title(page), 'Which name should an answer given from the page carry?');
});

test('selecting a decision pushes /decisions/<id>, loading that path selects it, and Back returns to the one selected before', async () => {
  const page = await openPage(fakeServer(frameDecisions()));
  await select(page, 'decision-D41');
  assert.equal(path(page), '/decisions/decision-D41');
  assert.equal(page.window.history.length, 2);
  assert.equal(currentRow(page), 'decision-D41');
  assert.equal(title(page), 'Start plan WEBUX-DECIDE before WEBUX-NODES lands?');

  page.window.history.back();
  await page.settle();
  assert.equal(selected(page), 'decision-D43');
  assert.equal(currentRow(page), 'decision-D43');
  assert.equal(title(page), 'Which name should an answer given from the page carry?');

  const direct = await openPage(fakeServer(frameDecisions()), { url: '/decisions/decision-D41' });
  assert.equal(selected(direct), 'decision-D41');
  assert.equal(currentRow(direct), 'decision-D41');
  assert.equal(direct.window.history.length, 1);

  const answered = await openPage(fakeServer(frameDecisions()), { url: '/decisions/decision-D39' });
  assert.equal(answered.$('#dec-tab-answered').getAttribute('aria-selected'), 'true', 'a decision opened by its path brings its tab');
  assert.equal(currentRow(answered), 'decision-D39');
});

test('an unknown id keeps the list and reads "decision-D99 not found." with no control', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { url: '/decisions/decision-D99' });
  assert.equal(rowIds(page).length, 4, 'the list is kept');
  assert.equal(currentRow(page), null);
  const state = page.$('#decisions-detail [data-pane-state]');
  assert.equal(state.getAttribute('data-pane-state'), 'empty');
  assert.equal(state.textContent.trim(), 'decision-D99 not found.');
  assert.equal(state.querySelector('button'), null);
  assert.equal(path(page), '/decisions/decision-D99');
});


// The answer form ------------------------------------------------------------------------------

test('the recommended option is listed first with its badge, and nothing is checked', async () => {
  const page = await openPage(fakeServer(frameDecisions()));
  const cards = page.$$('.dec-answer-form .dec-option-card');
  assert.deepEqual(cards.map((c) => c.getAttribute('data-option-key')), ['a', 'b', 'c', 'd']);
  assert.match(cards[0].textContent, /Recommended/);
  assert.deepEqual(checked(page), []);
  assert.ok(submitBtn(page).disabled);
});

test('picking an option keeps the custom text typed before it, and both are sent', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server);
  type(page.$('.dec-custom-text'), 'Ask once, then keep it');
  card(page, 'a').click();
  assert.equal(page.$('.dec-custom-text').value, 'Ask once, then keep it');
  assert.deepEqual(checked(page), ['a']);
  assert.equal(page.$('.dec-answer-pick').textContent, 'The project’s git user.name');
  submitBtn(page).click();
  await page.settle();
  assert.deepEqual(server.writes.map((w) => w.body), [{ option: 'a', text: 'Ask once, then keep it', rationale: '' }]);
});

test('an abandon or defer effect is named on Answer and confirmed before anything is sent', async () => {
  const server = fakeServer([...frameDecisions(), {
    id: 'decision-D44', title: 'Defer it?', created_at: at(44), status: 'OPEN', custom_effect: 'defer',
    options: [], dependents: [waitingNode('T-1', 'One task')],
  }]);
  const page = await openPage(server, { url: '/decisions/decision-D42' });
  const btn = submitBtn(page);
  card(page, 'abandon').click();
  assert.equal(btn.textContent, 'Answer — abandon 2 nodes');
  assert.ok(btn.classList.contains('bg-red-600') && !btn.classList.contains('bg-emerald-600'));
  card(page, 'defer').click();
  assert.equal(btn.textContent, 'Answer — defer 2 nodes');
  card(page, 'drop_edge').click();
  assert.equal(btn.textContent, 'Answer');
  assert.ok(btn.classList.contains('bg-emerald-600') && !btn.classList.contains('bg-red-600'));

  card(page, 'abandon').click();
  btn.click();
  await page.settle();
  assert.deepEqual(server.writes, [], 'nothing is sent before the confirm');
  const confirm = dialog(page);
  assert.equal(confirm.querySelector('h2').textContent, 'Abandon 2 nodes?');
  assert.deepEqual(confirm.querySelectorAll('.dec-effect-node a.id-link').map((a) => a.textContent), ['WEBUX-NODES-DETAIL', 'WEBUX-NODES-STEP']);
  assert.ok(confirm.querySelectorAll('.dec-effect-node [role="img"]').length === 2, 'each node carries its status icon');
  const go = confirm.querySelector('.dlg-submit');
  assert.equal(go.textContent, 'Abandon');
  go.click();
  await page.settle();
  assert.deepEqual(server.writes.map((w) => [w.verb, w.body.option]), [['answer', 'abandon']]);

  await select(page, 'decision-D44');
  type(page.$('.dec-custom-text'), 'Later');
  assert.equal(submitBtn(page).textContent, 'Answer — defer 1 node', 'a custom answer carries the decision\'s custom effect');
});

test('the confirm opens in #dialog-root on Cancel and keeps Tab inside', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { url: '/decisions/decision-D42' });
  card(page, 'abandon').click();
  submitBtn(page).focus();
  submitBtn(page).click();
  const confirm = dialog(page);
  assert.ok(page.$('#dialog-root').contains(confirm));
  assert.equal(confirm.getAttribute('aria-modal'), 'true');
  assert.ok(focused(page).classList.contains('dlg-cancel'));
  const last = confirm.querySelector('.dlg-submit');
  last.focus();
  key(last, 'Tab');
  assert.ok(focused(page).classList.contains('dlg-close'), 'Tab wraps from the last control to the first');
  press(page, focused(page), 'Tab', { shiftKey: true });
  assertFocus(page, last, 'Shift+Tab wraps from the first control to the last');
});


// Keyboard map -----------------------------------------------------------------------------------

test('keys: ↓ and ↑ select the next and previous row from the list or the detail, keep focus on the row, and type in a text field', async () => {
  const page = await openPage(fakeServer(frameDecisions()));
  page.$('.dec-row[data-decision-id="decision-D43"]').focus();
  const down = press(page, focused(page), 'ArrowDown');
  assert.ok(down.defaultPrevented);
  assert.equal(path(page), '/decisions/decision-D42');
  assert.equal(focused(page).getAttribute('data-decision-id'), 'decision-D42');
  await page.settle();
  assert.equal(title(page), 'WEBUX-NODES-KIT was DEFERRED: drop the edge, defer, or abandon the dependents?');
  assert.equal(focused(page).getAttribute('data-decision-id'), 'decision-D42', 'focus stays on the row once the detail loads');

  card(page, 'abandon').focus();
  press(page, focused(page), 'ArrowUp');
  await page.settle();
  assert.equal(selected(page), 'decision-D43', '↑ from an option in the detail selects the previous row');
  assert.equal(focused(page).getAttribute('data-decision-id'), 'decision-D43');

  const field = page.$('.dec-rationale');
  field.focus();
  const typed = press(page, field, 'ArrowDown');
  assert.ok(!typed.defaultPrevented);
  assert.equal(selected(page), 'decision-D43', 'an arrow in a text field moves the caret, not the selection');
});

// vis-network draws on the first store change whatever the view, and its keyboard bound to the
// window takes every arrow ahead of the page, the way this stand-in does from the root element.
function visTakingWindowKeys(w) {
  class DataSet {
    constructor(items) { this.items = new Map(items.map((i) => [i.id, i])); }
    getIds() { return [...this.items.keys()]; }
    update(list) { list.forEach((i) => this.items.set(i.id, i)); }
    remove(ids) { ids.forEach((id) => this.items.delete(id)); }
  }
  class Network {
    constructor(container, data, options) {
      this.handlers = {};
      const keyboard = options.interaction.keyboard;
      if (keyboard === true || (keyboard.enabled && keyboard.bindToWindow !== false)) {
        w.document.documentElement.addEventListener('keydown', (e) => {
          if (e.key.startsWith('Arrow')) e.preventDefault();
        });
      }
    }
    on(event, fn) { (this.handlers[event] ||= []).push(fn); }
    getBoundingBox() { return { left: 0, top: 0, right: 0, bottom: 0 }; }
    canvasToDOM(p) { return p; }
    fit() {}
    selectNodes() {}
    unselectAll() {}
    focus() {}
  }
  w.vis = { DataSet, Network };
}

test('keys: ↓ reaches the Decisions pane with the graph drawn, its keys bound to the canvas', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { before: visTakingWindowKeys });
  page.socket.message({ type: 'snapshot', re: page.socket.sent.at(-1).id, rows: [], bodies: {}, statuses: [], decisions_open: 4 });
  await page.settle();
  assert.ok(page.run('networkInstance'), 'the graph is drawn');
  page.$('.dec-row[data-decision-id="decision-D43"]').focus();
  press(page, focused(page), 'ArrowDown');
  assert.equal(path(page), '/decisions/decision-D42');
});

test('keys: 1–9 pick option n in the order shown, and type as usual in a text field', async () => {
  const page = await openPage(fakeServer(frameDecisions()));
  press(page, page.document.body, '2');
  assert.deepEqual(checked(page), ['b'], '2 picks the second option shown (the recommended one leads)');
  assertFocus(page, card(page, 'b'));
  press(page, card(page, 'b'), '1');
  assert.deepEqual(checked(page), ['a']);
  press(page, page.document.body, '9');
  assert.deepEqual(checked(page), ['a'], 'a number past the last option does nothing');

  const field = page.$('.dec-custom-text');
  field.focus();
  const typed = press(page, field, '3');
  assert.ok(!typed.defaultPrevented);
  assert.deepEqual(checked(page), ['a']);
});

test('keys: Ctrl or Cmd+Enter submits from anywhere in the form, text fields included, and opens the confirm for abandon', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server);
  card(page, 'a').click();
  type(page.$('.dec-rationale'), 'Same author as the landing commits');
  const ev = press(page, page.$('.dec-rationale'), 'Enter', { ctrlKey: true });
  assert.ok(ev.defaultPrevented);
  await page.settle();
  assert.deepEqual(server.writes.map((w) => [w.id, w.body.option, w.body.rationale]), [['decision-D43', 'a', 'Same author as the landing commits']]);

  assert.equal(selected(page), 'decision-D42');
  card(page, 'abandon').click();
  press(page, page.$('.dec-custom-text'), 'Enter', { metaKey: true });
  await page.settle();
  assert.equal(server.writes.length, 1, 'abandon waits for the confirm');
  assert.equal(dialog(page).querySelector('h2').textContent, 'Abandon 2 nodes?');
  assertFocus(page, page.$('#dialog-root .dlg-cancel'));
});

test('keys: Escape on the confirm cancels it and returns focus to Answer, the form as it was', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server, { url: '/decisions/decision-D42' });
  card(page, 'abandon').click();
  type(page.$('.dec-rationale'), 'Redraw later');
  press(page, page.$('.dec-rationale'), 'Enter', { ctrlKey: true });
  assert.ok(dialog(page));
  key(focused(page), 'Escape');
  assert.equal(dialog(page), null);
  assertFocus(page, submitBtn(page));
  assert.deepEqual(checked(page), ['abandon']);
  assert.equal(page.$('.dec-rationale').value, 'Redraw later');
  assert.deepEqual(server.writes, []);
});

test('keys: ← and → move between options and check them, with one tab stop in the group', async () => {
  const page = await openPage(fakeServer(frameDecisions()));
  const stops = () => page.$$('.dec-answer-form .dec-option-card').filter((c) => c.getAttribute('tabindex') === '0');
  assert.deepEqual(stops().map(describe), [describe(card(page, 'a'))]);
  card(page, 'a').focus();
  for (const [k, expected] of [['ArrowRight', 'b'], ['ArrowRight', 'c'], ['ArrowLeft', 'b'], ['ArrowLeft', 'a'], ['ArrowLeft', 'd']]) {
    assert.ok(press(page, focused(page), k).defaultPrevented);
    assertFocus(page, card(page, expected), `${k} moves focus`);
    assert.deepEqual(checked(page), [expected], `${k} checks the focused option`);
    assert.deepEqual(stops().map(describe), [describe(card(page, expected))]);
  }
});

test('keys: Enter and Space activate a row, an option or a link, each a native button or link', async () => {
  const page = await openPage(fakeServer(frameDecisions()));
  for (const row of page.$$('.dec-row')) assert.equal(row.localName, 'button');
  for (const c of page.$$('.dec-answer-form .dec-option-card')) {
    assert.equal(c.localName, 'button');
    assert.equal(c.getAttribute('type'), 'button', 'an option never submits the form');
  }
  for (const link of page.$$('#decisions-detail .id-link')) {
    assert.ok(link.localName === 'button' || (link.localName === 'a' && link.getAttribute('href')));
  }
});


// Writes ----------------------------------------------------------------------------------------

test('a write disables the control that fired it with a spinner until the response, and nothing else changes before it', async () => {
  const cases = [
    ['Answer', '/decisions/decision-D43', async (page) => { card(page, 'a').click(); submitBtn(page).click(); return submitBtn(page); }],
    ['Withdraw', '/decisions/decision-D43', async (page) => {
      page.$('.dec-withdraw-btn').click();
      page.$('#dialog-root .dlg-submit').click();
      return page.$('#dialog-root .dlg-submit');
    }],
    ['Reopen', '/decisions/decision-D39', async (page) => { page.$('.dec-reopen-btn').click(); return page.$('.dec-reopen-btn'); }],
    ['Add task', '/decisions/decision-D43', async (page) => {
      page.$('.dec-block-add').click();
      await page.settle();
      type(page.$('#dialog-root .dbk-query'), 'SHIP');
      press(page, page.$('#dialog-root .dbk-query'), 'ArrowDown');
      page.$('#dialog-root .dlg-submit').click();
      return page.$('#dialog-root .dlg-submit');
    }],
    ['remove ×', '/decisions/decision-D43', async (page) => { page.$('.dec-block-remove').click(); return page.$('.dec-block-remove'); }],
  ];
  for (const [name, url, act] of cases) {
    const server = fakeServer(frameDecisions());
    const page = await openPage(server, { url });
    const before = { rows: rowIds(page).join(), path: path(page), detail: page.$('#decisions-detail .dec-title').textContent };
    const release = server.hold();
    if (page.$('.dec-rationale')) type(page.$('.dec-rationale'), 'as typed');
    const control = await act(page);
    await page.settle();
    assert.equal(control.disabled, true, `${name} is disabled in flight`);
    assert.equal(control.getAttribute('aria-busy'), 'true');
    assert.ok(control.querySelector('.animate-spin'), `${name} shows the spinner in place of its label`);
    assert.deepEqual({ rows: rowIds(page).join(), path: path(page), detail: page.$('#decisions-detail .dec-title').textContent }, before, `${name}: nothing else moved`);
    if (page.$('.dec-rationale')) assert.equal(page.$('.dec-rationale').value, 'as typed');
    assert.equal(page.$('#toast-root .toast'), null);
    release();
    await page.settle();
    assert.equal(page.$('#toast-root .toast').getAttribute('data-tone'), 'success', `${name} answers`);
  }
});

test('a failed write re-enables the form as typed with an error toast, and its Retry resends the same payload; a 409 fails the same way', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server);
  card(page, 'a').click();
  type(page.$('.dec-custom-text'), 'Kept');
  card(page, 'a').click();
  type(page.$('.dec-rationale'), 'Why');
  server.failNext(409, "decision 'decision-D43' is not open");
  submitBtn(page).click();
  await page.settle();
  assert.equal(submitBtn(page).disabled, false);
  assert.equal(submitBtn(page).textContent, 'Answer');
  assert.deepEqual(checked(page), ['a']);
  assert.equal(page.$('.dec-custom-text').value, 'Kept');
  assert.equal(page.$('.dec-rationale').value, 'Why');
  assert.equal(path(page), '/decisions/decision-D43', 'nothing moved');
  const toastEl = page.$('#toast-root .toast');
  assert.equal(toastEl.getAttribute('data-tone'), 'error');
  assert.match(toastEl.textContent, /decision 'decision-D43' is not open/);

  server.failNext(500, 'Disk full');
  toastEl.querySelector('.toast-retry').click();
  await page.settle();
  assert.match(page.$('#toast-root .toast').textContent, /Disk full/);
  page.$('#toast-root .toast-retry').click();
  await page.settle();
  assert.equal(server.writes.length, 3);
  assert.ok(server.writes.every((w) => JSON.stringify(w.body) === JSON.stringify(server.writes[0].body)), 'Retry resends the same payload');
  assert.equal(server.byId.get('decision-D43').status, 'ANSWERED');
  assert.equal(selected(page), 'decision-D42');
});

test('a successful write shows one polite success toast naming the decision and the result; it never takes focus and leaves on Escape, × or after 6 s', async () => {
  const clock = fakeClock();
  const server = fakeServer(frameDecisions());
  const page = await openPage(server, { before: (w) => { w.setTimeout = clock.setTimeout; w.clearTimeout = clock.clearTimeout; } });
  const root = page.$('#toast-root');
  assert.equal(root.getAttribute('aria-live'), 'polite');

  await answerWith(page, 'a');
  let toastEl = root.querySelector('.toast');
  assert.equal(toastEl.getAttribute('data-tone'), 'success');
  assert.equal(toastEl.textContent.trim(), 'decision-D43 answered: The project’s git user.name');
  assert.ok(toastEl.querySelector('use[href="#icon-check-circle-2"]'));
  assert.ok(!root.contains(focused(page)), 'the toast never takes focus');

  await withdraw(page, 'Superseded');
  assert.equal(root.children.length, 1, 'a new toast replaces the last');
  assert.equal(root.textContent.trim(), 'decision-D42 withdrawn');
  key(page.document.body, 'Escape');
  assert.equal(root.children.length, 0, 'Escape closes it');

  await answerWith(page, 'a');
  root.querySelector('.toast-close').click();
  assert.equal(root.children.length, 0, 'its × closes it');

  await answerWith(page, 'a');
  clock.tick(5999);
  assert.equal(root.children.length, 1);
  clock.tick(1);
  assert.equal(root.children.length, 0, 'it leaves after 6 s');
});

test('nothing is optimistic: the row, the counts and the selection move only from the write\'s response', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server);
  const counts = () => [page.$('#dec-tab-open .dec-tab-count').textContent, page.$('#decisions-badge').textContent];
  assert.deepEqual(counts(), ['4', '4']);
  const release = server.hold();
  card(page, 'a').click();
  submitBtn(page).click();
  await page.settle();
  assert.deepEqual(rowIds(page), ['decision-D43', 'decision-D42', 'decision-D41', 'decision-D40']);
  assert.deepEqual(counts(), ['4', '4']);
  assert.equal(currentRow(page), 'decision-D43');
  release();
  await page.settle();
  pushOpenCount(page, server);
  await page.settle();
  assert.deepEqual(rowIds(page), ['decision-D42', 'decision-D41', 'decision-D40']);
  assert.deepEqual(counts(), ['3', '3']);
  assert.equal(currentRow(page), 'decision-D42');
});

test('a decision another session closes keeps its form, disabled with the draft, and a toast\'s Show loads the closed record', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server);
  const entries = page.window.history.length;
  card(page, 'b').click();
  type(page.$('.dec-rationale'), 'Mine');
  await closeElsewhere(page, server, 'decision-D43', {
    status: 'ANSWERED', answer: { option: 'a', text: '', rationale: '', answered_by: 'Ana', answered_at: at(52) },
  });

  assert.equal(path(page), '/decisions/decision-D43', 'nothing navigates');
  assert.equal(page.window.history.length, entries);
  const form = page.$('.dec-answer-form');
  assert.ok(form, 'the form stays');
  assert.ok(form.querySelectorAll('button, textarea').every((el) => el.disabled), 'every control is disabled');
  assert.deepEqual(checked(page), ['b']);
  assert.equal(page.$('.dec-rationale').value, 'Mine');
  const toastEl = page.$('#toast-root .toast');
  assert.equal(toastEl.querySelector('span').textContent, 'decision-D43 answered by Ana');
  const show = toastEl.querySelector('.toast-action');
  assert.equal(show.textContent, 'Show');

  show.click();
  await page.settle();
  assert.equal(page.$('.dec-answer-form'), null);
  assert.ok(page.$('.dec-reopen-btn'), 'the closed record shows');
  assert.equal(page.$('#dec-tab-answered').getAttribute('aria-selected'), 'true');
  assert.equal(path(page), '/decisions/decision-D43');


  const otherServer = fakeServer(frameDecisions());
  const other = await openPage(otherServer, { url: '/decisions/decision-D42' });
  await closeElsewhere(other, otherServer, 'decision-D42', { status: 'WITHDRAWN', withdrawn_by: 'Ana', withdrawn_reason: 'Superseded' });
  assert.equal(other.$('#toast-root .toast span').textContent, 'decision-D42 withdrawn by Ana');
  assert.ok(other.$('.dec-answer-form'), 'a withdrawal holds the form the same way');
});

test('Answer and Withdraw end the same way: the tab stays, the next row (the previous after the last) is selected with focus on its first option, and none left focuses the tab', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server);
  const entries = page.window.history.length;
  await answerWith(page, 'a');
  assert.equal(page.$('#dec-tab-open').getAttribute('aria-selected'), 'true');
  assert.equal(path(page), '/decisions/decision-D42');
  assert.equal(page.window.history.length, entries, 'the next decision replaces the closed one\'s entry');
  assertFocus(page, card(page, 'abandon'), 'focus on the first option shown');

  await select(page, 'decision-D40');
  await withdraw(page, 'No longer needed');
  assert.equal(path(page), '/decisions/decision-D41', 'the last row closed: the one before it');
  assertFocus(page, card(page, 'a'));
  assert.equal(page.$('#dec-tab-open').getAttribute('aria-selected'), 'true');

  const single = fakeServer(frameDecisions().filter((d) => d.id === 'decision-D41'));
  const alone = await openPage(single);
  await withdraw(alone);
  assert.equal(path(alone), '/decisions');
  assert.equal(alone.$('.dec-answer-form'), null);
  assertFocus(alone, alone.$('#dec-tab-open'), 'with no row left, focus lands on the tab');
});

test('Reopen sends the reopen alone and follows the decision: the Open tab, it selected, focus on its first option', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server, { url: '/decisions/decision-D39' });
  assert.equal(page.$('#dec-tab-answered').getAttribute('aria-selected'), 'true');
  page.$('.dec-reopen-btn').click();
  await page.settle();
  assert.deepEqual(server.writes.map((w) => [w.id, w.verb]), [['decision-D39', 'reopen']], 'nothing else is written: the effect\'s nodes stay as they are');
  assert.equal(page.$('#dec-tab-open').getAttribute('aria-selected'), 'true');
  assert.equal(path(page), '/decisions/decision-D39');
  assert.equal(currentRow(page), 'decision-D39');
  assertFocus(page, card(page, 'approve'));
  assert.equal(page.$('#toast-root .toast').textContent.trim(), 'decision-D39 reopened');
});

test('Cancel, Close and Escape on the confirm, Withdraw… and Add task dialogs return focus to their opener with the draft intact', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server);

  const withdrawBtn = page.$('.dec-withdraw-btn');
  for (const how of ['cancel', 'close', 'escape']) {
    withdrawBtn.focus();
    withdrawBtn.click();
    const reason = page.$('#dialog-root .wd-reason');
    if (how === 'cancel') type(reason, 'Half a reason');
    else assert.equal(reason.value, 'Half a reason', 'the reason is kept');
    if (how === 'escape') key(reason, 'Escape');
    else page.$(`#dialog-root .dlg-${how}`).click();
    assert.equal(dialog(page), null);
    assertFocus(page, withdrawBtn, `${how} returns focus to Withdraw…`);
  }

  const addBtn = page.$('.dec-block-add');
  addBtn.focus();
  addBtn.click();
  type(page.$('#dialog-root .dbk-query'), 'WEBUX');
  key(page.$('#dialog-root .dbk-query'), 'Escape');
  assertFocus(page, addBtn);
  addBtn.click();
  assert.equal(page.$('#dialog-root .dbk-query').value, 'WEBUX', 'the query is kept');
  page.$('#dialog-root .dlg-close').click();
  assertFocus(page, addBtn);

  await select(page, 'decision-D42');
  card(page, 'abandon').click();
  for (const how of ['cancel', 'close']) {
    submitBtn(page).focus();
    submitBtn(page).click();
    page.$(`#dialog-root .dlg-${how}`).click();
    assertFocus(page, submitBtn(page), `${how} returns focus to Answer`);
    assert.deepEqual(checked(page), ['abandon']);
  }
  assert.deepEqual(server.writes, []);
});

const pickerRows = (page) => page.$$('#dialog-root .dbk-option').map((r) => r.getAttribute('data-task-id'));
const pickedRow = (page) => {
  const row = page.$('#dialog-root .dbk-option[aria-selected="true"]');
  return row && row.getAttribute('data-task-id');
};
const addControl = (page) => page.$('#dialog-root .dlg-submit');

test('+ Add task opens its picker in the field, listing the tasks the server offers in its order as one-line options', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server, { url: '/decisions/decision-D43' });
  page.$('.dec-block-add').click();
  assertFocus(page, page.$('#dialog-root .dbk-query'), 'the dialog opens in the field');
  await page.settle();
  assert.equal(page.$('#dialog-root h2').textContent, 'Add task to decision-D43');
  assert.ok(page.$('#dialog-root .dbk-query').closest('label').querySelector('use[href="#icon-search"]'), 'the field carries the search icon');
  assert.equal(page.$('#dialog-root [role="listbox"]').getAttribute('id'), page.$('#dialog-root .dbk-query').getAttribute('aria-controls'));
  assert.deepEqual(pickerRows(page), ['WEBUX-DECIDE-FLOW', 'WEBUX-DECIDE-LINKS', 'WEBUX-NODES-KIT', 'WEBUX-SHIP'], 'every task the server offers for D43, in its order');
  const kit = page.$('#dialog-root .dbk-option[data-task-id="WEBUX-NODES-KIT"]');
  assert.equal(kit.getAttribute('role'), 'option');
  assert.equal(kit.querySelector('[role="img"]').getAttribute('aria-label'), 'Deferred', 'its status icon');
  assert.equal(kit.children[1].textContent, 'WEBUX-NODES-KIT');
  assert.equal(kit.children[2].textContent, 'Shared renderers carry the design rules');
  assert.ok(kit.children[2].classList.contains('truncate'));
  assert.equal(kit.querySelectorAll('[tabindex]').length, 0, 'an option is one focus stop');
  assert.equal(pickedRow(page), null, 'nothing is selected on open');
  assert.equal(addControl(page).disabled, true, 'Add waits for a selection');
  assert.deepEqual(page.$$('#dialog-root .dbk-option').map((r) => r.getAttribute('tabindex')), ['0', '-1', '-1', '-1'], 'Tab enters the list at its first row');
});

test('typing filters the picker by id or title; ↓ enters the list and ↑/↓ move the selection with focus; ↑ from the top returns to the field', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server, { url: '/decisions/decision-D43' });
  page.$('.dec-block-add').click();
  await page.settle();
  const field = page.$('#dialog-root .dbk-query');
  type(field, 'webux-decide');
  assert.deepEqual(pickerRows(page), ['WEBUX-DECIDE-FLOW', 'WEBUX-DECIDE-LINKS'], 'by id, in any case');
  type(field, 'design RULES');
  assert.deepEqual(pickerRows(page), ['WEBUX-NODES-KIT'], 'by title');
  type(field, 'WEBUX');

  assert.equal(press(page, field, 'ArrowDown').defaultPrevented, true);
  assertFocus(page, page.$('#dialog-root .dbk-option[data-task-id="WEBUX-DECIDE-FLOW"]'), '↓ from the field');
  assert.equal(pickedRow(page), 'WEBUX-DECIDE-FLOW');
  press(page, page.document.activeElement, 'ArrowDown');
  assertFocus(page, page.$('#dialog-root .dbk-option[data-task-id="WEBUX-DECIDE-LINKS"]'), '↓ again');
  assert.equal(pickedRow(page), 'WEBUX-DECIDE-LINKS', 'the selection follows focus');
  assert.equal(addControl(page).disabled, false, 'Add is enabled with a row selected');
  assert.deepEqual(page.$$('#dialog-root .dbk-option').map((r) => r.getAttribute('tabindex')), ['-1', '0', '-1', '-1'], 'the selected row is the tab stop');
  press(page, page.document.activeElement, 'ArrowUp');
  assert.equal(pickedRow(page), 'WEBUX-DECIDE-FLOW', '↑ moves back');
  press(page, page.document.activeElement, 'ArrowUp');
  assertFocus(page, field, '↑ from the top row returns to the field');

  type(field, 'webux-decide-l');
  assert.equal(pickedRow(page), null, 'a filter that hides the selected row clears it');
  assert.equal(addControl(page).disabled, true);
  page.$('#dialog-root .dbk-option').dispatchEvent(new page.window.Event('focusin'));
  assert.equal(pickedRow(page), 'WEBUX-DECIDE-LINKS', 'Tab into the list selects the row it lands on');
  assert.deepEqual(server.writes, [], 'moving through the list adds nothing');
});

test('a click selects a picker row without adding it; Enter on a row or Add adds the selected one', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server, { url: '/decisions/decision-D43' });
  page.$('.dec-block-add').click();
  await page.settle();
  page.$('#dialog-root .dbk-option[data-task-id="WEBUX-SHIP"]').click();
  assert.equal(pickedRow(page), 'WEBUX-SHIP');
  assertFocus(page, page.$('#dialog-root .dbk-option[data-task-id="WEBUX-SHIP"]'));
  assert.deepEqual(server.writes, [], 'a click only selects');
  press(page, page.$('#dialog-root .dbk-option[data-task-id="WEBUX-NODES-KIT"]'), ' ');
  assert.equal(pickedRow(page), 'WEBUX-NODES-KIT', 'Space selects');
  assert.deepEqual(server.writes, []);
  addControl(page).click();
  await page.settle();
  assert.deepEqual(server.writes.map((w) => [w.verb, w.body]), [['blocks', { add: ['WEBUX-NODES-KIT'] }]], 'Add adds the selected row');
  assert.equal(dialog(page), null);

  page.$('.dec-block-add').click();
  await page.settle();
  assert.equal(pickerRows(page).includes('WEBUX-NODES-KIT'), false, 'the picker reads afresh: a task now waiting is not offered');
  const ship = page.$('#dialog-root .dbk-option[data-task-id="WEBUX-SHIP"]');
  assert.equal(press(page, ship, 'Enter').defaultPrevented, true);
  await page.settle();
  assert.deepEqual(server.writes.at(-1).body, { add: ['WEBUX-SHIP'] }, 'Enter on a row adds it');
});

test('a query that matches nothing reads "No tasks match “…”." in place of the list, with Add disabled and focus in the field', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server, { url: '/decisions/decision-D43' });
  page.$('.dec-block-add').click();
  await page.settle();
  const field = page.$('#dialog-root .dbk-query');
  press(page, field, 'ArrowDown');
  field.focus();
  type(field, 'SHIP-0.4');
  const state = page.$('#dialog-root .dbk-results [data-pane-state]');
  assert.equal(state.getAttribute('data-pane-state'), 'empty');
  assert.equal(state.textContent, 'No tasks match “SHIP-0.4”.');
  assert.equal(state.querySelector('button'), null, 'no control on an empty state');
  assert.equal(addControl(page).disabled, true);
  assert.equal(press(page, field, 'ArrowDown').defaultPrevented, false, '↓ has nowhere to go');
  assertFocus(page, field);

  const none = fakeServer(frameDecisions().map((d) => (d.id === 'decision-D43' ? { ...d, dependents: TASKS.map((t) => waitingNode(t.id, t.title)) } : d)));
  const full = await openPage(none, { url: '/decisions/decision-D43' });
  full.$('.dec-block-add').click();
  await full.settle();
  assert.equal(full.$('#dialog-root .dbk-results [data-pane-state]').textContent, 'No tasks to add.', 'with nothing typed and nothing to offer');
});

test('the picker\'s read shows loading only once in flight 300 ms, and a failure reads its message with a Retry that reads again', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server, { url: '/decisions/decision-D43' });
  const release = server.holdCandidates();
  page.$('.dec-block-add').click();
  await page.settle();
  assert.equal(page.$('#dialog-root .dbk-results').children.length, 0, 'a read under 300 ms flashes nothing');
  await new Promise((r) => setTimeout(r, 320));
  assert.equal(page.$('#dialog-root .dbk-results [data-pane-state]').getAttribute('data-pane-state'), 'loading');
  release();
  await page.settle();
  assert.equal(pickerRows(page).length, 4);
  key(page.$('#dialog-root .dbk-query'), 'Escape');

  server.nodeFailures.set('candidates', 502);
  page.$('.dec-block-add').click();
  await page.settle();
  const error = page.$('#dialog-root .dbk-results [data-pane-state="error"]');
  assert.equal(error.querySelector('p').textContent, 'Bad Gateway');
  server.nodeFailures.delete('candidates');
  error.querySelector('.pane-retry').click();
  await page.settle();
  assert.equal(pickerRows(page).length, 4, 'Retry reads the list again');
});

test('the confirm is the only gate before an abandon: it opens on Cancel, a plain button Enter activates, and no undo follows', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server, { url: '/decisions/decision-D42' });
  card(page, 'abandon').click();
  submitBtn(page).click();
  const cancel = page.$('#dialog-root .dlg-cancel');
  assertFocus(page, cancel);
  assert.equal(cancel.getAttribute('type'), 'button', 'Enter on Cancel cancels; it never submits');
  cancel.click();
  assert.deepEqual(server.writes, []);

  submitBtn(page).click();
  page.$('#dialog-root .dlg-submit').click();
  await page.settle();
  assert.equal(server.writes.length, 1);
  const toastEl = page.$('#toast-root .toast');
  assert.equal(toastEl.querySelector('.toast-action'), null, 'no Undo is offered');
});

test('every pointer action is a focusable button or link: rows, options, ids, ×, Attach, Copy ID, + Add task, Reopen, toast controls and the Filters toggle', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server);
  const reachable = (el, name) => {
    assert.ok(el, `${name} is on the page`);
    assert.ok(['button', 'a'].includes(el.localName) || el.getAttribute('tabindex') === '0', `${name} is focusable`);
    assert.notEqual(el.getAttribute('tabindex'), '-1', `${name} is in the tab order`);
  };
  page.$$('.dec-row').forEach((r) => reachable(r, 'a row'));
  reachable(page.$('.dec-answer-form .dec-option-card[tabindex="0"]'), 'the options group');
  const waiting = page.$$('.dec-waiting-row a.id-link');
  assert.ok(waiting.length > 0);
  waiting.forEach((r) => reachable(r, 'a waiting row'));
  page.$$('.dec-block-remove').forEach((r) => reachable(r, 'a remove ×'));
  reachable(page.$('.copy-id-btn'), 'Copy ID');
  reachable(page.$('.dec-block-add'), '+ Add task');
  reachable(page.$('#filters-toggle-btn'), 'the Filters toggle');

  const attach = page.$('.att-add-btn');
  reachable(attach, 'Attach');
  assert.equal(attach.localName, 'button', 'Attach is a native button');
  let picked = 0;
  const create = page.document.createElement.bind(page.document);
  page.document.createElement = (tag, ...rest) => {
    const el = create(tag, ...rest);
    if (tag === 'input') el.click = () => { picked += 1; };
    return el;
  };
  // The DOM shim has no native activation: a browser clicks a button on Enter or Space unless a
  // keydown handler prevented it.
  const activate = (el, k) => { if (!key(el, k).defaultPrevented && el.localName === 'button') el.click(); };
  activate(attach, 'Enter');
  activate(attach, ' ');
  page.document.createElement = create;
  assert.equal(picked, 2, 'Enter and Space open the file picker');

  await closeElsewhere(page, server, 'decision-D43', { status: 'WITHDRAWN', withdrawn_by: 'Ana' });
  page.$$('#toast-root button').forEach((b) => reachable(b, 'a toast control'));
  page.$('#toast-root .toast-action').click();
  await page.settle();
  reachable(page.$('.dec-reopen-btn'), 'Reopen');
});

test('a draft belongs to its decision: it comes back with the decision, a successful answer clears it, and nothing reaches browser storage', async () => {
  const stored = [];
  const storage = { getItem: () => null, setItem: (k) => stored.push(k), removeItem() {} };
  const server = fakeServer(frameDecisions());
  const page = await openPage(server, { before: (w) => { w.localStorage = storage; w.sessionStorage = storage; } });
  card(page, 'b').click();
  type(page.$('.dec-rationale'), 'Because');
  await select(page, 'decision-D42');
  assert.deepEqual(checked(page), []);
  assert.equal(page.$('.dec-rationale').value, '');
  type(page.$('.dec-custom-text'), 'Later');
  await select(page, 'decision-D43');
  assert.deepEqual(checked(page), ['b']);
  assert.equal(page.$('.dec-rationale').value, 'Because');
  await select(page, 'decision-D42');
  assert.equal(page.$('.dec-custom-text').value, 'Later');

  await select(page, 'decision-D43');
  submitBtn(page).click();
  await page.settle();
  page.run("selectDecision('decision-D43')");
  await page.settle();
  page.$('.dec-reopen-btn').click();
  await page.settle();
  assert.deepEqual(checked(page), [], 'the answered draft is gone');
  assert.equal(page.$('.dec-rationale').value, '');
  assert.deepEqual(stored, [], 'nothing is written to browser storage');

  const reloaded = await openPage(server, { url: '/decisions/decision-D42' });
  assert.equal(reloaded.$('.dec-custom-text').value, '', 'a reload discards the draft');
});

test('at 375 the answer bar holds the form\'s only Answer and Withdraw…, and Ctrl or Cmd+Enter submits there too', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server, { width: 375 });
  const bars = page.$$('.dec-answer-actions');
  assert.equal(bars.length, 1);
  assert.ok(bars[0].classList.contains('fixed') && bars[0].classList.contains('bottom-0'));
  assert.equal(page.$$('.dec-answer-submit').length, 1);
  assert.equal(page.$$('.dec-withdraw-btn').length, 1);
  assert.ok(bars[0].contains(submitBtn(page)) && bars[0].contains(page.$('.dec-withdraw-btn')));
  const toastRoot = page.$('#toast-root');
  assert.ok(toastRoot.classList.contains('max-sm:bottom-[69px]'), 'a toast sits above the empty bar');

  card(page, 'a').click();
  assert.ok(page.$('.dec-answer-pick-line').classList.contains('max-sm:flex'));
  assert.equal(page.$('.dec-answer-pick').textContent, 'The project’s git user.name');
  assert.ok(toastRoot.classList.contains('max-sm:bottom-[93px]'), 'and above the bar once it names the pick');
  assert.ok(!submitBtn(page).disabled);

  press(page, submitBtn(page), 'Enter', { ctrlKey: true });
  await page.settle();
  assert.deepEqual(server.writes.map((w) => [w.id, w.verb]), [['decision-D43', 'answer']]);
});


// Below sm ---------------------------------------------------------------------------------------

test('below sm /decisions opens the top open decision\'s page in place, with the list out of the page', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { width: 375 });
  assert.equal(path(page), '/decisions/decision-D43');
  assert.equal(page.window.history.length, 1);
  const aside = page.$('#decisions-pane aside');
  assert.ok(aside.classList.contains('hidden') && aside.classList.contains('sm:flex'), 'the list column is not on the page');
  assert.ok(aside.contains(page.$('#decisions-list')));
  const bar = page.$('#decisions-detail .dec-page-bar');
  assert.ok(bar.classList.contains('max-sm:sticky'), 'the header line is the sticky page bar');
  const drawerBtn = bar.querySelector('.dec-drawer-btn');
  assert.equal(drawerBtn.getAttribute('aria-label'), 'Decisions');
  assert.equal(drawerBtn.getAttribute('aria-haspopup'), 'dialog');
  assert.equal(drawerBtn.getAttribute('aria-expanded'), 'false');
  assert.equal(drawerBtn.getAttribute('aria-controls'), 'decisions-drawer');
  assert.equal(page.$('#decisions-detail .dec-title').getAttribute('tabindex'), '-1');
});

test('below sm the Drawer button opens the list as a dialog: focus on the current row, Tab kept inside, its own scroll, and Escape back to the button', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { width: 375 });
  const drawerBtn = () => page.$('.dec-drawer-btn');
  drawerBtn().click();
  const drawer = page.$('#dialog-root #decisions-drawer');
  assert.equal(drawer.getAttribute('role'), 'dialog');
  assert.equal(drawer.getAttribute('aria-modal'), 'true');
  assert.equal(page.$(`#${drawer.getAttribute('aria-labelledby')}`).textContent, 'Decisions');
  assert.ok(drawer.contains(page.$('#decisions-tabs')) && drawer.contains(page.$('#decisions-list')));
  assert.ok(page.$('#decisions-list').classList.contains('overflow-y-auto'), 'the list scrolls on its own');
  assert.equal(drawerBtn().getAttribute('aria-expanded'), 'true');
  assert.equal(focused(page).getAttribute('data-decision-id'), 'decision-D43');
  assert.equal(focused(page).getAttribute('aria-current'), 'page');

  const rows = page.$$('#decisions-drawer .dec-row');
  rows.at(-1).focus();
  key(rows.at(-1), 'Tab');
  assert.ok(focused(page).classList.contains('dlg-close'), 'Tab wraps inside the drawer');

  page.$('#decisions-list').scrollTop = 200;
  key(focused(page), 'Escape');
  assert.equal(page.$('#decisions-drawer'), null);
  assert.ok(page.$('#decisions-pane aside').contains(page.$('#decisions-list')), 'the list goes back out of the page');
  assertFocus(page, drawerBtn());
  assert.equal(drawerBtn().getAttribute('aria-expanded'), 'false');
  assert.equal(path(page), '/decisions/decision-D43');

  drawerBtn().click();
  assert.equal(page.$('#decisions-list').scrollTop, 200, 'the list keeps its scroll for the next open');
  page.$('#dialog-root .dlg-close').click();
  assertFocus(page, drawerBtn());
});

test('below sm a pick in the drawer closes it, pushes /decisions/<id> and focuses the page heading; Back returns to the decision before', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { width: 375 });
  page.$('.dec-drawer-btn').click();
  page.$('#decisions-drawer .dec-row[data-decision-id="decision-D41"]').click();
  assert.equal(page.$('#decisions-drawer'), null);
  await page.settle();
  assert.equal(path(page), '/decisions/decision-D41');
  assert.equal(page.window.history.length, 2);
  assertFocus(page, page.$('#decisions-detail .dec-title'));
  assert.equal(title(page), 'Start plan WEBUX-DECIDE before WEBUX-NODES lands?');

  page.$('.dec-drawer-btn').click();
  page.$('#decisions-drawer .dec-row[data-decision-id="decision-D41"]').click();
  assert.equal(page.$('#decisions-drawer'), null, 'picking the current row only closes the drawer');
  assertFocus(page, page.$('#decisions-detail .dec-title'));
  assert.equal(page.window.history.length, 2);

  page.window.history.back();
  await page.settle();
  assert.equal(path(page), '/decisions/decision-D43');
  assert.equal(title(page), 'Which name should an answer given from the page carry?');
});

test('below sm a drawer tab changes only the list, ↑/↓ move focus between rows, the active tab again scrolls to the top, and the scrim closes it', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { width: 375 });
  page.$('.dec-drawer-btn').click();
  const row = page.$('#decisions-drawer .dec-row[data-decision-id="decision-D43"]');
  press(page, row, 'ArrowDown');
  assert.equal(focused(page).getAttribute('data-decision-id'), 'decision-D42');
  press(page, focused(page), 'ArrowUp');
  assert.equal(focused(page).getAttribute('data-decision-id'), 'decision-D43');
  assert.equal(path(page), '/decisions/decision-D43', 'moving focus never changes the page');

  page.$('#dec-tab-answered').focus();
  page.$('#dec-tab-answered').click();
  await page.settle();
  assert.deepEqual(rowIds(page), ['decision-D39']);
  assert.equal(currentRow(page), null, 'another tab shows no selected row');
  assert.equal(path(page), '/decisions/decision-D43');
  assert.equal(title(page), 'Which name should an answer given from the page carry?');

  page.$('#decisions-list').scrollTop = 120;
  page.$('#dec-tab-answered').click();
  await page.settle();
  assert.equal(page.$('#decisions-list').scrollTop, 0);
  assertFocus(page, page.$('#dec-tab-answered'), 'focus stays on the tab');
  assert.equal(path(page), '/decisions/decision-D43');

  page.$('#dialog-root').firstChild.click();
  assert.equal(page.$('#decisions-drawer'), null, 'a tap on the scrim closes it');
  page.$('.dec-drawer-btn').click();
  assertFocus(page, page.$('#dec-tab-answered'), 'the current decision is not in the tab: focus on the tab');
});

test('below sm Answer and Withdraw move the page to the next decision in place, and with none left focus lands on the Drawer button over the empty page', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server, { width: 375 });
  await answerWith(page, 'a');
  assert.equal(path(page), '/decisions/decision-D42');
  assert.equal(page.window.history.length, 1, 'the URL is replaced');
  assertFocus(page, card(page, 'abandon'));

  const single = fakeServer(frameDecisions().filter((d) => d.id === 'decision-D41'));
  const alone = await openPage(single, { width: 375 });
  await withdraw(alone);
  assert.equal(path(alone), '/decisions');
  assertFocus(alone, alone.$('.dec-drawer-btn'));
  const state = alone.$('#decisions-detail [data-pane-state]');
  assert.equal(state.textContent.trim(), 'No open decisions.');
  assert.equal(state.querySelector('button'), null);
});

test('below sm a failed or empty list shows in the page and in the drawer, the error with Retry', async () => {
  const server = fakeServer(frameDecisions());
  server.nodeFailures.set('list', 502);
  const page = await openPage(server, { width: 375 });
  const pageState = page.$('#decisions-detail [data-pane-state]');
  assert.equal(pageState.getAttribute('data-pane-state'), 'error');
  assert.equal(pageState.querySelector('p').textContent, 'Could not load decisions.');
  assert.ok(pageState.querySelector('.pane-retry'));
  page.$('.dec-drawer-btn').click();
  assert.equal(page.$('#decisions-drawer [data-pane-state]').getAttribute('data-pane-state'), 'error');
  assertFocus(page, page.$('#dec-tab-open'));
  key(focused(page), 'Escape');

  server.nodeFailures.delete('list');
  page.$('#decisions-detail .pane-retry').click();
  await page.settle();
  assert.equal(path(page), '/decisions/decision-D43', 'Retry loads the list and resolves the page');
});

test('from sm up the list stays beside the detail and the Drawer button is hidden; a resize past sm closes an open drawer', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { width: 375 });
  page.$('.dec-drawer-btn').click();
  assert.ok(page.$('#decisions-drawer'));
  page.window.innerWidth = 1440;
  page.window.dispatchEvent(new page.window.Event('resize'));
  assert.equal(page.$('#decisions-drawer'), null);
  assert.ok(page.$('#decisions-pane aside').contains(page.$('#decisions-list')));

  const wide = await openPage(fakeServer(frameDecisions()));
  assert.ok(wide.$('.dec-drawer-btn').classList.contains('sm:hidden'));
  assert.ok(wide.$('#decisions-pane aside').classList.contains('sm:flex'));
  await select(wide, 'decision-D41');
  assert.equal(wide.$('#dialog-root').children.length, 0, 'a row selects in place, with no drawer');
  assert.equal(path(wide), '/decisions/decision-D41');
});
