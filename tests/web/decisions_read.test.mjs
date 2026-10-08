import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, jsonResponse, key, type } from './dom.mjs';

const ago = (minutes) => new Date(Date.now() - minutes * 60000).toISOString();
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function opt(optionKey, label, extra = {}) {
  return { key: optionKey, label, description: `${label}, described.`, recommended: false, effect: 'none', ...extra };
}

function node(id, title, status, kind = 'task') {
  return { id, title, kind, status, finished: false };
}

const CODE_LINE = `$ git -C /Users/owner/Documents/TaskManager config user.name && tm decision answer decision-D43 --option a --rationale "same"`;

const CONTEXT = 'context of decision-D43';
// What marked emits for the frames' context specimen. The page loads marked from /vendor, which
// the harness does not run, so this stands in for it on that one input.
const CONTEXT_HTML = [
  '<h2>Who answered</h2>',
  '<p>An answer given from the page is stored with <code>answered_by: web</code>, so its history cannot tell who.</p>',
  '<h3>Where each name comes from</h3>',
  '<table><thead><tr><th>Source</th><th>Read</th><th>When it is missing</th></tr></thead>',
  '<tbody><tr><td>git <code>user.name</code></td><td>once, in <code>create_app</code></td><td>"web"</td></tr></tbody></table>',
  '<ol><li><code>_write_guard</code> reads the name once.</li></ol>',
  '<ul><li>Static exports carry the stored name.</li></ul>',
  `<pre><code>${CODE_LINE}</code></pre>`,
  '<blockquote><p>A name that changes per browser cannot be audited later.</p></blockquote>',
].join('\n');

const IMAGE = { asset: 'a1b2c3.png', name: 'decisions-empty-375.png', mime: 'image/png', size_bytes: 14336 };
const FILE = { asset: 'd4e5f6.md', name: 'empty-states.md', mime: 'text/markdown', size_bytes: 118 };

// The open, answered and withdrawn decisions the frames draw.
function frameDecisions() {
  return [
    {
      id: 'decision-D43', title: 'Which name should an answer given from the page carry?', created_at: ago(2), status: 'OPEN', priority: 30,
      context: CONTEXT,
      options: [opt('b', 'A name typed once per browser'), opt('a', 'The project’s git user.name', { recommended: true })],
      dependents: [node('WEBUX-DECIDE-API', 'The decisions API says was blocking', 'AWAITING_DECISION'), node('WEBUX-DECIDE-READ', 'Decision rows read at a glance', 'AWAITING_DECISION')],
    },
    {
      id: 'decision-D42', title: 'WEBUX-NODES-KIT was DEFERRED: drop the edge, defer, or abandon the dependents?', created_at: ago(5), status: 'OPEN', priority: 50,
      options: [
        opt('drop_edge', 'Drop the edge', { effect: 'drop_edge' }),
        opt('defer', 'Defer the dependents', { effect: 'defer' }),
        opt('abandon', 'Abandon the dependents', { effect: 'abandon', recommended: true }),
      ],
      dependents: [node('WEBUX-NODES-DETAIL', 'One detail renderer', 'AWAITING_DECISION'), node('WEBUX-NODES-STEP', 'A started step is a row', 'AWAITING_DECISION')],
    },
    {
      id: 'decision-D40', title: 'Which icon should an empty decisions list show?', created_at: ago(11), status: 'OPEN', priority: 70,
      options: [opt('a', 'circle-dashed', { recommended: true }), opt('b', 'inbox')],
      dependents: [node('WEBUX-DECIDE-READ', 'Decision rows read at a glance', 'AWAITING_DECISION')],
      attachments: [IMAGE, FILE],
    },
    {
      id: 'decision-D39', title: 'Approve the WEBUX-DECIDE frames?', created_at: ago(20), status: 'ANSWERED', priority: 50,
      options: [opt('approve', 'Approve', { recommended: true }), opt('redraw', 'Redraw')],
      answer: { option: 'approve', text: '', rationale: 'Every frame reads as drawn.', answered_by: 'Taígo Pedrosa', answered_at: ago(15) },
      dependents: [node('WEBUX-DECIDE-API', 'The decisions API says was blocking', 'BLOCKED_BY_TASK'), node('WEBUX-DECIDE-READ', 'Decision rows read at a glance', 'BLOCKED_BY_TASK')],
    },
    {
      id: 'decision-D41', title: 'Start plan WEBUX-DECIDE before WEBUX-NODES lands?', created_at: ago(8), status: 'WITHDRAWN', priority: 50,
      options: [opt('a', 'Wait for WEBUX-NODES'), opt('b', 'Start now')],
      withdrawn_reason: 'WEBUX-NODES landed first, so the plan starts on its own.', withdrawn_by: 'Ana', withdrawn_at: ago(7),
      dependents: [node('WEBUX-DECIDE', 'Answering a decision is fast and safe', 'BLOCKED_BY_TASK', 'plan')],
    },
  ];
}

// An in-memory /api for the pane. `hold(kind)` keeps every request of that kind ('list', 'node'
// or 'write') in flight until its release runs; `fail(kind, status)` answers them with an error.
function fakeServer(decisions) {
  const byId = new Map(decisions.map((d) => [d.id, { attachments: [], ...d }]));
  const gates = {};
  const failures = {};
  const writes = [];
  const counts = () => {
    const c = { open: 0, answered: 0, withdrawn: 0 };
    byId.forEach((d) => { c[d.status.toLowerCase()] += 1; });
    return c;
  };
  const body = (d) => ({
    node: {
      id: d.id, kind: 'decision', title: d.title, status: d.status, display: d.status, priority: d.priority,
      frontmatter: {
        decision: {
          options: d.options, allow_custom: true, raised_by: 'WEBUX-DECIDE-API', answer: d.answer || null, custom_effect: 'none',
          withdrawn_reason: d.withdrawn_reason || '', withdrawn_by: d.withdrawn_by || null, withdrawn_at: d.withdrawn_at || null,
        },
        attachments: d.attachments,
      },
    },
    sections: d.context ? [{ key: 'context', header: '## Context', content: d.context, ordinal: 1 }] : [],
    dependent_details: d.dependents,
  });
  const listItem = (d) => ({
    id: d.id, title: d.title, status: d.status, priority: d.priority, created_at: d.created_at,
    ...(d.status === 'OPEN' ? {} : { closed_at: d.answer ? d.answer.answered_at : d.withdrawn_at, was_blocking: d.dependents.length }),
  });
  async function answer(kind, respond) {
    if (gates[kind]) await gates[kind];
    if (failures[kind]) return jsonResponse(failures[kind].status, { detail: failures[kind].detail });
    return respond();
  }
  const fetch = async (url, opts) => {
    const method = (opts && opts.method) || 'GET';
    const nodeGet = url.match(/^\/api\/nodes\/([^/?]+)$/);
    if (nodeGet && method === 'GET') {
      return answer('node', () => {
        const d = byId.get(nodeGet[1]);
        return d ? jsonResponse(200, body(d)) : jsonResponse(404, { detail: 'Node not found' });
      });
    }
    const candidates = url.match(/^\/api\/decisions\/([^/]+)\/candidates$/);
    if (candidates) {
      const waiting = new Set(byId.get(candidates[1]).dependents.map((n) => n.id));
      const items = ['WEBUX-DECIDE-API', 'WEBUX-DECIDE-READ', 'WEBUX-SHIP'].filter((id) => !waiting.has(id))
        .map((id) => ({ id, title: `${id} title`, display: 'READY' }));
      return jsonResponse(200, { items });
    }
    if (url.startsWith('/api/decisions?')) {
      return answer('list', () => {
        const status = new URLSearchParams(url.split('?')[1]).get('status');
        const items = [...byId.values()].filter((d) => d.status === status)
          .sort((a, b) => b.created_at.localeCompare(a.created_at)).map(listItem);
        return jsonResponse(200, { items, next: null, counts: counts() });
      });
    }
    const write = url.match(/^\/api\/(decisions|nodes)\/([^/]+)\/(blocks|attachments|reopen)(?:\/([^/]+))?$/);
    if (write && method !== 'GET') {
      const sent = JSON.parse((opts && opts.body) || '{}');
      writes.push({ id: write[2], verb: write[3], method, body: sent });
      return answer('write', () => {
        const d = byId.get(write[2]);
        if (write[3] === 'blocks') {
          d.dependents = d.dependents.filter((n) => !(sent.remove || []).includes(n.id))
            .concat((sent.add || []).map((id) => node(id, `${id} title`, 'AWAITING_DECISION')));
          return jsonResponse(200, { id: d.id });
        }
        if (write[3] === 'reopen') {
          Object.assign(d, { status: 'OPEN', answer: null });
          return jsonResponse(200, { id: d.id });
        }
        if (method === 'DELETE') {
          d.attachments = d.attachments.filter((a) => a.asset !== write[4]);
          return jsonResponse(200, { asset: write[4] });
        }
        const entry = { asset: `new-${sent.filename}`, name: sent.filename, mime: 'text/plain', size_bytes: 3 };
        d.attachments = d.attachments.concat([entry]);
        return jsonResponse(201, entry);
      });
    }
    return undefined;
  };
  return {
    fetch, byId, writes, counts,
    hold(kind) {
      let release;
      gates[kind] = new Promise((resolve) => { release = resolve; });
      return () => { delete gates[kind]; release(); };
    },
    fail(kind, status, detail) { failures[kind] = { status, detail }; },
    heal(kind) { delete failures[kind]; },
  };
}

function pushOpenCount(page, server) {
  page.socket.message({ type: 'update', items: [{ op: 'decisions_open', count: server.counts().open }] });
}

function withStubs(w) {
  w.marked = { parse: (md) => (md === CONTEXT ? CONTEXT_HTML : `<p>${md}</p>`) };
  w.FileReader = class {
    readAsDataURL(file) {
      this.result = `data:text/plain;base64,${file.b64}`;
      this.onload();
    }
  };
}

async function openPage(server, { url = '/decisions', settle = true } = {}) {
  const page = loadPage({ url, fetch: server.fetch, beforeScripts: withStubs });
  page.socket.open();
  pushOpenCount(page, server);
  if (settle) await page.settle();
  return page;
}

async function select(page, id) {
  page.$(`.dec-row[data-decision-id="${id}"]`).click();
  await page.settle();
}

async function showTab(page, tab) {
  page.$(`#dec-tab-${tab}`).click();
  await page.settle();
}

const rowLine = (page, id) => page.$(`.dec-row[data-decision-id="${id}"]`).parentNode;
const detail = (page) => page.$('#decisions-detail');
const text = (el) => el.textContent.replace(/\s+/g, ' ').trim();
const paneStateOf = (root) => root.querySelector('[data-pane-state]');


// Objective --------------------------------------------------------------------------------------

test('a row is one line: the id link, the title truncating with the whole question as its tooltip, the priority only when not 50, and the short age; no status icon', async () => {
  const page = await openPage(fakeServer(frameDecisions()));
  const line = rowLine(page, 'decision-D43');
  assert.ok(line.classList.contains('h-9'), 'one 36px line');
  const link = line.querySelector('a.id-link');
  assert.equal(link.getAttribute('data-id'), 'decision-D43');
  assert.equal(link.getAttribute('href'), '/decisions/decision-D43');
  const title = line.querySelector('.dec-row');
  assert.equal(title.textContent, 'Which name should an answer given from the page carry?');
  assert.equal(title.getAttribute('title'), 'Which name should an answer given from the page carry?');
  assert.ok(title.classList.contains('truncate'));
  assert.equal(line.querySelector('.priority-pill').textContent, 'P30');
  assert.equal(line.querySelector('.dec-age').textContent, '2m');
  assert.equal(rowLine(page, 'decision-D40').querySelector('.priority-pill').textContent, 'P70');
  assert.equal(rowLine(page, 'decision-D42').querySelector('.priority-pill'), null, 'P50 is not drawn on a row');
  for (const row of page.$$('.dec-row-line')) {
    assert.equal(row.querySelector('[role="img"]'), null, 'the tab is the status, so no row draws one');
    assert.equal(row.querySelector('.dec-was-blocking'), null, 'an open row says nothing of what it was blocking');
  }
  assert.equal(line.querySelector('a.id-link').closest('button'), null, 'no link nests in the row button');
});

test('answered and withdrawn rows and details say "Was blocking n" from was_blocking, each node with its current status icon', async () => {
  const page = await openPage(fakeServer(frameDecisions()));
  await showTab(page, 'answered');
  assert.equal(text(rowLine(page, 'decision-D39').querySelector('.dec-was-blocking')), 'Was blocking 2');
  const group = detail(page).querySelector('.dec-waiting');
  assert.equal(text(group.querySelector('.disclosure')), 'Was blocking 2');
  const rows = group.querySelectorAll('.dec-waiting-row');
  assert.deepEqual(rows.map((r) => r.querySelector('[role="img"]').getAttribute('aria-label')), ['Blocked by Task', 'Blocked by Task']);
  assert.equal(group.querySelector('.dec-block-remove'), null, 'nothing to remove from a closed decision');
  assert.equal(group.querySelector('.dec-block-add'), null, 'nothing to add to a closed decision');

  await showTab(page, 'withdrawn');
  assert.equal(text(rowLine(page, 'decision-D41').querySelector('.dec-was-blocking')), 'Was blocking 1');
  const withdrawn = detail(page).querySelector('.dec-waiting');
  assert.equal(text(withdrawn.querySelector('.disclosure')), 'Was blocking 1');
  const plan = withdrawn.querySelector('.dec-waiting-row');
  assert.equal(plan.querySelector('[role="img"]').getAttribute('aria-label'), 'Blocked by Task');
  assert.equal(plan.querySelector('.kind-badge').textContent, 'PLAN');
});

test('a closed row ages from when it closed, an open row from when it was raised', async () => {
  const page = await openPage(fakeServer(frameDecisions()));
  assert.equal(rowLine(page, 'decision-D43').querySelector('.dec-age').textContent, '2m');
  await showTab(page, 'answered');
  assert.equal(rowLine(page, 'decision-D39').querySelector('.dec-age').textContent, '15m', 'answered 15m ago, raised 20m ago');
  await showTab(page, 'withdrawn');
  assert.equal(rowLine(page, 'decision-D41').querySelector('.dec-age').textContent, '7m', 'withdrawn 7m ago, raised 8m ago');
});

test('the effect pill hugs its text and words each effect: none, drop_edge, defer, abandon, with "node" singular at one', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { url: '/decisions/decision-D42' });
  const pill = (optionKey) => page.$(`.dec-option-card[data-option-key="${optionKey}"] .dec-effect-pill`);
  assert.equal(pill('drop_edge').textContent, '2 waiting nodes stop waiting');
  assert.equal(pill('defer').textContent, 'Defers 2 waiting nodes');
  assert.equal(pill('abandon').textContent, 'Abandons 2 waiting nodes');
  for (const cls of ['self-start', 'inline-flex', 'whitespace-nowrap']) assert.ok(pill('abandon').classList.contains(cls), `the pill hugs its text: ${cls}`);
  assert.ok(pill('abandon').classList.contains('text-red-300'));
  assert.ok(pill('defer').classList.contains('text-amber-300'));
  assert.ok(pill('drop_edge').classList.contains('text-zinc-300'));

  await select(page, 'decision-D40');
  assert.equal(pill('a').textContent, 'No effect on waiting nodes');
  assert.ok(pill('a').classList.contains('text-zinc-400'));
  page.$('.dec-withdraw-btn').click();
  assert.equal(page.$('#dialog-root .dec-effect-pill').textContent, '1 waiting node stops waiting', 'Withdraw names what it does to the one waiting node');
});

test('with no attachments, Attach sits in the header and no Attachments block shows; with some, the group sits below the answer form', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { url: '/decisions/decision-D43' });
  assert.ok(page.$('#decisions-detail .dec-page-bar .att-add-btn'), 'Attach is in the header line');
  assert.equal(page.$('#decisions-detail .dec-attachments'), null);
  assert.ok(!/Attachments/i.test(text(detail(page))));

  await select(page, 'decision-D40');
  assert.ok(page.$('#decisions-detail .dec-page-bar .att-add-btn'), 'Attach stays in the header');
  const column = detail(page).querySelector('.max-w-2xl').children;
  const at = (cls) => column.findIndex((c) => c.classList.contains(cls));
  assert.ok(at('dec-attachments') > at('dec-answer-form'), 'the group comes after the answer form');
  const group = page.$('.dec-attachments');
  assert.equal(text(group.querySelector('.disclosure')), 'Attachments 2');
  assert.deepEqual(group.querySelectorAll('.att-name').map((n) => n.textContent), ['decisions-empty-375.png', 'empty-states.md']);
  assert.equal(group.querySelector('.att-source-badge'), null, 'a card holds its name, size and detach');
  assert.equal(page.$$('#decisions-detail .att-add-btn').length, 1);
});

test('context markdown styles table, th, td, pre, code and blockquote; the code block scrolls inside itself on zinc-950', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { url: '/decisions/decision-D43' });
  const types = page.run('Object.keys(CONTEXT_TYPE)');
  for (const tag of ['table', 'th', 'td', 'pre', 'code', 'blockquote']) assert.ok(types.includes(tag), `CONTEXT_TYPE maps ${tag}`);
  const body = page.$('.dec-context-body');
  const has = (el, classes, what) => classes.forEach((c) => assert.ok(el.classList.contains(c), `${what} carries ${c}`));
  const table = body.querySelector('table');
  has(table, ['w-full', 'table-fixed'], 'table');
  has(table.parentNode, ['rounded-lg', 'border', 'border-zinc-800', 'overflow-x-auto'], 'the table wrapper');
  has(body.querySelector('th'), ['bg-zinc-900', 'font-semibold', 'text-zinc-200'], 'th');
  has(body.querySelector('td'), ['border-t', 'border-zinc-800', 'text-zinc-300'], 'td');
  const pre = body.querySelector('pre');
  has(pre, ['bg-zinc-950', 'overflow-x-auto', 'whitespace-pre', 'font-mono', 'text-zinc-200', 'focus-visible:ring-emerald-400'], 'pre');
  assert.equal(pre.getAttribute('tabindex'), '0', 'the keyboard reaches the code block to scroll it');
  assert.equal(pre.querySelector('code').className, '', 'code in a block reads as its block');
  has(body.querySelector('p code'), ['font-mono', 'text-emerald-300'], 'inline code');
  const quote = body.querySelector('blockquote');
  has(quote, ['border-l-2', 'border-zinc-700', 'italic', 'text-zinc-400'], 'blockquote');
  assert.ok(!quote.querySelector('p').classList.contains('text-zinc-300'), 'a quoted paragraph keeps the quote\'s tone');
});

test('an answered decision names who answered it from the API, and a withdrawn one who withdrew it', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { url: '/decisions/decision-D39' });
  const by = page.$('#decisions-detail .dec-closed-by');
  assert.equal(by.querySelector('.dec-closed-name').textContent, 'Taígo Pedrosa');
  assert.match(text(by), /^Answered by Taígo Pedrosa · \S/);
  assert.deepEqual(page.$$('#decisions-detail .dec-answer .dec-option-card').map((c) => c.getAttribute('data-option-key')), ['approve'], 'the chosen option alone');

  await showTab(page, 'withdrawn');
  const withdrawn = page.$('#decisions-detail .dec-closed-by');
  assert.equal(withdrawn.querySelector('.dec-closed-name').textContent, 'Ana');
  assert.match(text(withdrawn), /^Withdrawn by Ana · \S/);
});

test('an answered decision offers Reopen and a withdrawn one does not', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { url: '/decisions/decision-D39' });
  assert.equal(page.$$('#decisions-detail .dec-reopen-btn').length, 1);
  await showTab(page, 'withdrawn');
  assert.equal(page.$('#decisions-detail .dec-title').textContent, 'Start plan WEBUX-DECIDE before WEBUX-NODES lands?');
  assert.equal(page.$$('#decisions-detail .dec-reopen-btn').length, 0);
});

test('Custom answer, Rationale and the withdraw Reason set typed text in Inter, never mono', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { url: '/decisions/decision-D43' });
  page.$('.dec-withdraw-btn').click();
  const fields = [page.$('.dec-custom-text'), page.$('.dec-rationale'), page.$('#dialog-root .wd-reason')];
  for (const field of fields) {
    assert.equal(field.localName, 'textarea');
    assert.equal(field.classList.contains('font-mono'), false, `${field.getAttribute('class').split(' ')[0]} is not mono`);
  }
});

const EXPLANATIONS = [
  'Select a decision to view it.', 'Write a custom answer instead of picking an option', '(optional)', '(optional, markdown)',
  'Which auth flow?', 'task-id', 'Pick an option or write a custom answer', 'Nothing picked yet — pick an option or write a custom answer',
  'The decision is dropped; tasks waiting on it unblock immediately.',
];

function surfaceText(root) {
  const attrs = root.querySelectorAll('*').flatMap((el) => ['placeholder', 'title', 'aria-label'].map((a) => el.getAttribute(a) || ''));
  return `${root.textContent}\n${attrs.join('\n')}`;
}

function labelOf(field) {
  return text(field.closest('label')).replace(field.textContent, '').trim();
}

test('no surface of the pane explains itself, and each field keeps the label naming it', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { url: '/decisions/decision-D43' });
  const seen = [surfaceText(page.$('#decisions-pane'))];
  assert.equal(labelOf(page.$('.dec-custom-text')), 'Custom answer');
  assert.equal(labelOf(page.$('.dec-rationale')), 'Rationale');
  for (const field of page.$$('#decisions-detail textarea')) assert.equal(field.getAttribute('placeholder'), null);

  page.$('.dec-withdraw-btn').click();
  seen.push(surfaceText(page.$('#dialog-root')));
  assert.equal(labelOf(page.$('#dialog-root .wd-reason')), 'Reason');
  key(page.$('#dialog-root .wd-reason'), 'Escape');

  page.$('.dec-block-add').click();
  await page.settle();
  seen.push(surfaceText(page.$('#dialog-root')));
  assert.equal(labelOf(page.$('#dialog-root .dbk-query')), 'Task');
  assert.equal(page.$('#dialog-root .dbk-query').getAttribute('placeholder'), null);
  key(page.$('#dialog-root .dbk-query'), 'Escape');

  page.run('openNewDecisionDialog()');
  seen.push(surfaceText(page.$('#dialog-root')));
  assert.equal(labelOf(page.$('#dialog-root .nd-question')), 'Question');
  assert.equal(labelOf(page.$('#dialog-root .nd-context')), 'Context');
  assert.equal(labelOf(page.$('#dialog-root .nd-blocks')), 'Blocks tasks');
  key(page.$('#dialog-root .nd-question'), 'Escape');

  const server = fakeServer([]);
  const empty = await openPage(server);
  seen.push(surfaceText(empty.$('#decisions-pane')));
  for (const phrase of EXPLANATIONS) {
    seen.forEach((surface, i) => assert.ok(!surface.includes(phrase), `surface ${i} says "${phrase}"`));
  }
});

test('every status in the pane is an icon named on hover: the header by its decision status, Waiting rows by their node status; statusChip is gone', async () => {
  const page = await openPage(fakeServer(frameDecisions()), { url: '/decisions/decision-D43' });
  const header = page.$('#decisions-detail .dec-page-bar');
  const icon = header.querySelector('[role="img"]');
  assert.equal(icon.getAttribute('aria-label'), 'Open');
  assert.equal(icon.getAttribute('title'), 'Open');
  assert.ok(icon.querySelector('use[href="#icon-help-circle"]'));
  assert.ok(!/\bOpen\b/.test(header.textContent), 'no status word beside the icon');
  assert.equal(header.querySelector('.kind-badge'), null, 'a decision\'s id names its kind');
  for (const row of page.$$('.dec-waiting-row')) {
    const status = row.querySelector('[role="img"]');
    assert.ok(status.classList.contains('st-text') && status.classList.contains('st-AWAITING_DECISION'));
    assert.equal(status.getAttribute('aria-label'), 'Awaiting Decision');
  }
  await showTab(page, 'answered');
  assert.ok(page.$('#decisions-detail .dec-page-bar [role="img"] use[href="#icon-check-circle-2"]'));
  assert.equal(page.run('typeof statusChip'), 'undefined');
});


// [READ] behaviour -------------------------------------------------------------------------------

test('a read shows its loading pane state only once in flight 300 ms, a fast one never; a write spins from the click', async () => {
  const server = fakeServer(frameDecisions());
  const releaseList = server.hold('list');
  const page = await openPage(server);
  assert.equal(paneStateOf(page.$('#decisions-list')), null, 'nothing flashes before 300 ms');
  await wait(350);
  assert.equal(paneStateOf(page.$('#decisions-list')).getAttribute('data-pane-state'), 'loading');
  releaseList();
  await page.settle();
  assert.equal(paneStateOf(page.$('#decisions-list')), null);

  const releaseNode = server.hold('node');
  page.$('.dec-row[data-decision-id="decision-D40"]').click();
  await page.settle();
  assert.equal(paneStateOf(detail(page)), null, 'the detail shows nothing loading before 300 ms');
  await wait(350);
  assert.equal(paneStateOf(detail(page)).getAttribute('data-pane-state'), 'loading');
  releaseNode();
  await page.settle();
  assert.equal(paneStateOf(detail(page)), null);
  assert.equal(page.$('#decisions-detail .dec-title').textContent, 'Which icon should an empty decisions list show?');

  await select(page, 'decision-D42');
  await wait(350);
  assert.equal(paneStateOf(detail(page)), null, 'a fast read never shows loading, even after 300 ms');

  await showTab(page, 'answered');
  server.hold('write');
  const reopen = page.$('.dec-reopen-btn');
  reopen.click();
  assert.ok(reopen.disabled && reopen.querySelector('.animate-spin'), 'the write spins from the click');
});

const tabCounts = (page) => page.$$('#decisions-tabs .dec-tab-btn').map((b) => {
  const pill = b.querySelector('.dec-tab-count');
  return pill ? pill.textContent : null;
});

test('the tabs show no count while the list loads or has failed, and a fast tab switch keeps them', async () => {
  const server = fakeServer(frameDecisions());
  const releaseList = server.hold('list');
  const page = await openPage(server);
  assert.deepEqual(tabCounts(page), [null, null, null], 'the first load, before the loading state shows');
  await wait(350);
  assert.equal(paneStateOf(page.$('#decisions-list')).getAttribute('data-pane-state'), 'loading');
  assert.deepEqual(tabCounts(page), [null, null, null], 'beside the loading state');
  releaseList();
  await page.settle();
  assert.deepEqual(tabCounts(page), ['3', '1', '1']);

  page.$('#dec-tab-answered').click();
  assert.deepEqual(tabCounts(page), ['3', '1', '1'], 'a tab switch in flight under 300 ms keeps the counts');
  await page.settle();
  const releaseAgain = server.hold('list');
  page.$('#dec-tab-withdrawn').click();
  await wait(350);
  assert.deepEqual(tabCounts(page), [null, null, null], 'a tab switch past 300 ms shows loading without counts');
  releaseAgain();
  await page.settle();
  assert.deepEqual(tabCounts(page), ['3', '1', '1']);

  server.fail('list', 502, 'Bad Gateway');
  page.run('refreshDecisionsData()');
  await page.settle();
  assert.equal(paneStateOf(page.$('#decisions-list')).getAttribute('data-pane-state'), 'error');
  assert.deepEqual(tabCounts(page), [null, null, null], 'beside the error state');
});

test('a failed list load is stated once, by its error pane state with Retry, and raises no toast', async () => {
  const server = fakeServer(frameDecisions());
  server.fail('list', 502, 'Bad Gateway');
  const page = await openPage(server);
  assert.equal(paneStateOf(page.$('#decisions-list')).querySelector('p').textContent, 'Could not load decisions.');
  assert.equal(page.$$('#toast-root .toast').length, 0);
});

test('a live re-render keeps the selected decision, both scroll offsets, the focused field and its draft; an arriving decision takes its place and is not selected', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server, { url: '/decisions/decision-D40' });
  const rationale = page.$('.dec-rationale');
  type(rationale, 'kept as typed');
  rationale.focus();
  page.$('#decisions-list').scrollTop = 120;
  detail(page).scrollTop = 300;

  server.byId.set('decision-D44', { id: 'decision-D44', title: 'A decision raised elsewhere', created_at: ago(0), status: 'OPEN', priority: 50, options: [opt('a', 'Yes')], dependents: [], attachments: [] });
  server.byId.get('decision-D40').dependents.unshift(node('WEBUX-SHIP', 'Ship it', 'AWAITING_DECISION'));
  pushOpenCount(page, server);
  await page.settle();

  assert.equal(page.window.location.pathname, '/decisions/decision-D40');
  assert.equal(page.$('.dec-row[aria-current="page"]').getAttribute('data-decision-id'), 'decision-D40');
  assert.deepEqual(page.$$('#decisions-list .dec-row').map((r) => r.getAttribute('data-decision-id')), ['decision-D44', 'decision-D43', 'decision-D42', 'decision-D40']);
  assert.equal(page.$('#dec-tab-open .dec-tab-count').textContent, '4', 'the count rose with it');
  assert.equal(page.$$('.dec-waiting-row').length, 2, 'the detail was drawn again with the new waiting row');
  assert.notEqual(page.$('.dec-rationale'), rationale, 'Rationale was drawn again');
  assert.equal(page.document.activeElement, page.$('.dec-rationale'), 'focus is in the redrawn Rationale');
  assert.equal(page.$('.dec-rationale').value, 'kept as typed');
  assert.equal(page.$('#decisions-list').scrollTop, 120);
  assert.equal(detail(page).scrollTop, 300);

  const remove = page.$('.dec-block-remove[data-task-id="WEBUX-SHIP"]');
  remove.click();
  await page.settle();
  assert.equal(page.$('.dec-rationale').value, 'kept as typed', 'a write keeps the draft too');
  assert.equal(page.window.location.pathname, '/decisions/decision-D40');
});

test('Attach opens the native picker and writes on choice; focus then goes to the new name, after Detach to Attach, after Add task to the new row, after remove × to + Add task', async () => {
  const server = fakeServer(frameDecisions());
  const page = await openPage(server, { url: '/decisions/decision-D40' });
  const inputs = [];
  const create = page.document.createElement.bind(page.document);
  page.document.createElement = (tag) => {
    const el = create(tag);
    if (tag === 'input') {
      el.click = () => inputs.push(el);
    }
    return el;
  };
  page.$('.dec-page-bar .att-add-btn').click();
  page.document.createElement = create;
  assert.equal(inputs.length, 1, 'one native file picker opened');
  assert.equal(inputs[0].getAttribute('type'), 'file');
  assert.equal(server.writes.length, 0, 'nothing is sent before a file is chosen');
  inputs[0].files = [{ name: 'notes.txt', b64: 'AAAA' }];
  inputs[0].dispatchEvent(new page.window.Event('change'));
  await page.settle();
  assert.deepEqual(server.writes.map((w) => [w.verb, w.method]), [['attachments', 'POST']]);
  assert.ok(page.document.activeElement.classList.contains('att-name'));
  assert.equal(page.document.activeElement.textContent, 'notes.txt');

  page.$('.att-card[data-asset="d4e5f6.md"] .att-detach-btn').click();
  page.$('#dialog-root .dlg-submit').click();
  await page.settle();
  assert.equal(server.writes.at(-1).method, 'DELETE');
  assert.ok(page.document.activeElement.classList.contains('att-add-btn'), 'after Detach focus is on Attach');

  page.$('.dec-block-add').click();
  await page.settle();
  type(page.$('#dialog-root .dbk-query'), 'WEBUX-SHIP');
  key(page.$('#dialog-root .dbk-query'), 'ArrowDown');
  page.$('#dialog-root .dlg-submit').click();
  await page.settle();
  assert.equal(page.document.activeElement.getAttribute('data-id'), 'WEBUX-SHIP', 'after Add task focus is on its row');
  assert.ok(page.document.activeElement.closest('.dec-waiting-row'), 'the new row\'s own link');

  page.$('.dec-block-remove[data-task-id="WEBUX-SHIP"]').click();
  await page.settle();
  assert.ok(page.document.activeElement.classList.contains('dec-block-add'), 'after remove × focus is on + Add task');
});

test('Copy ID shows check-circle-2 for 1.5 s and says "Copied" through its live region, with no toast', async () => {
  const copied = [];
  const server = fakeServer(frameDecisions());
  const page = await openPage(server, { url: '/decisions/decision-D43' });
  page.window.navigator.clipboard.writeText = async (t) => { copied.push(t); };
  const button = page.$('#decisions-detail .copy-id-btn');
  const live = button.querySelector('.copy-id-live');
  assert.equal(live.getAttribute('aria-live'), 'polite');
  assert.equal(live.textContent, '', 'the live region is on the page, empty, before the copy');
  button.click();
  await page.settle();
  assert.deepEqual(copied, ['decision-D43']);
  assert.equal(button.querySelector('use').getAttribute('href'), '#icon-check-circle-2');
  assert.equal(live.textContent, 'Copied');
  assert.equal(page.$('#toast-root .toast'), null, 'no toast');
  await wait(1300);
  assert.equal(button.querySelector('use').getAttribute('href'), '#icon-check-circle-2', 'still shown at 1.3 s');
  await wait(300);
  assert.equal(button.querySelector('use').getAttribute('href'), '#icon-copy');
  assert.equal(live.textContent, '');
});

test('a pane state says the fact alone, and its one control is Retry on an error, none on empty or not found', async () => {
  const controls = (state) => state.querySelectorAll('button, a').map((b) => b.textContent.trim());

  const empty = await openPage(fakeServer([]));
  const none = paneStateOf(empty.$('#decisions-list'));
  assert.equal(text(none), 'No open decisions.');
  assert.deepEqual(controls(none), []);

  const failing = fakeServer(frameDecisions());
  failing.fail('list', 502, 'Bad Gateway');
  const broken = await openPage(failing);
  const listError = paneStateOf(broken.$('#decisions-list'));
  assert.equal(listError.querySelector('p').textContent, 'Could not load decisions.');
  assert.deepEqual(controls(listError), ['Retry']);
  failing.heal('list');
  listError.querySelector('.pane-retry').click();
  await broken.settle();
  assert.ok(broken.$('.dec-row'), 'Retry reads the list again');

  const server = fakeServer(frameDecisions());
  server.fail('node', 502, '502 Bad Gateway');
  const page = await openPage(server, { url: '/decisions/decision-D43' });
  await wait(350);
  const detailError = paneStateOf(detail(page));
  assert.equal(detailError.getAttribute('data-pane-state'), 'error');
  assert.equal(detailError.querySelector('p').textContent, '502 Bad Gateway', 'the request\'s own message');
  assert.deepEqual(controls(detailError), ['Retry']);
  server.heal('node');
  detailError.querySelector('.pane-retry').click();
  await page.settle();
  assert.equal(page.$('#decisions-detail .dec-title').textContent, 'Which name should an answer given from the page carry?');

  const missing = await openPage(fakeServer(frameDecisions()), { url: '/decisions/decision-D99' });
  const notFound = paneStateOf(detail(missing));
  assert.equal(text(notFound), 'decision-D99 not found.');
  assert.deepEqual(controls(notFound), []);
});
