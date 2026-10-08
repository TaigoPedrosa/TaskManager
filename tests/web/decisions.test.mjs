import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, jsonResponse, key, type } from './dom.mjs';

const CONTEXT = [
  '## DECIDE — decisions UX',
  '',
  'The decisions pane is redesigned for the owner\'s queue:',
  'answering, reading options and their effects.',
  '',
  '### What the audit found',
  '',
  '1. Reading a decision\'s context takes two clicks.',
  '2. Option cards are `role="radio"` without arrow keys.',
].join('\n');

function d5(overrides = {}) {
  return {
    id: 'decision-D5',
    title: 'Authorize spec DECIDE (decisions UX, 0.3.3) as designed?',
    status: 'OPEN',
    context: CONTEXT,
    options: [
      { key: 'authorize', label: 'Authorize as designed', description: 'Every DECIDE node starts once WAVE has shipped', recommended: true, effect: 'none' },
      { key: 'changes', label: 'Authorize with changes', description: 'Write the changes in your answer', recommended: false, effect: 'none' },
      { key: 'abandon', label: 'Abandon it', description: 'The work is dropped', recommended: false, effect: 'abandon' },
    ],
    dependents: [{ id: 'DECIDE', title: 'Decisions UX', kind: 'spec', status: 'AWAITING_DECISION', finished: false }],
    ...overrides,
  };
}

function d6() {
  return { id: 'decision-D6', title: 'Approve the decisions pane frames?', status: 'OPEN', options: [], dependents: [] };
}

// An in-memory /api for the pane: decisions by id, list pages per status with `counts`, and
// per-id failures a test can switch on (a status code, or a thrown network error).
function fakeServer(decisions) {
  const byId = new Map(decisions.map((d) => [d.id, d]));
  const failures = new Map();
  const writes = [];
  function body(d) {
    return {
      node: {
        id: d.id, kind: 'decision', title: d.title, status: d.status, display: d.status,
        frontmatter: {
          decision: {
            options: d.options, allow_custom: true, raised_by: d.raised_by || null,
            answer: d.answer || null, withdrawn_reason: d.withdrawn_reason || '',
            withdrawn_by: d.withdrawn_by || null, withdrawn_at: d.withdrawn_at || null,
          },
        },
      },
      sections: d.context ? [{ key: 'context', header: '## Context', content: d.context, ordinal: 1 }] : [],
      dependent_details: d.dependents,
    };
  }
  const counts = () => {
    const c = { open: 0, answered: 0, withdrawn: 0 };
    byId.forEach((d) => { c[d.status.toLowerCase()] += 1; });
    return c;
  };
  const fetch = async (url, opts) => {
    const method = (opts && opts.method) || 'GET';
    const nodeMatch = url.match(/^\/api\/nodes\/([^/?]+)$/);
    if (nodeMatch) {
      const failure = failures.get(nodeMatch[1]);
      if (failure === 'network') throw new TypeError('Failed to fetch');
      if (failure) return jsonResponse(failure, { detail: 'Service Unavailable' });
      const d = byId.get(nodeMatch[1]);
      return d ? jsonResponse(200, body(d)) : jsonResponse(404, { detail: 'Node not found' });
    }
    if (url.startsWith('/api/decisions?')) {
      const status = new URLSearchParams(url.split('?')[1]).get('status');
      const items = [...byId.values()].filter((d) => d.status === status).map((d) => ({
        id: d.id, title: d.title, status: d.status, priority: 50,
        created_at: '2026-09-26T10:00:00+00:00',
      }));
      return jsonResponse(200, { items, next: null, counts: counts() });
    }
    const write = url.match(/^\/api\/decisions\/([^/]+)\/(answer|withdraw|reopen)$/);
    if (write && method === 'POST') {
      writes.push({ id: write[1], verb: write[2], body: JSON.parse(opts.body || '{}') });
      const d = byId.get(write[1]);
      if (write[2] === 'answer') {
        const sent = JSON.parse(opts.body);
        d.status = 'ANSWERED';
        d.answer = { option: sent.option, text: sent.text, rationale: sent.rationale, answered_by: 'owner', answered_at: '2026-09-26T10:26:20+00:00' };
      }
      return jsonResponse(200, {});
    }
    return undefined;
  };
  return { fetch, byId, failures, writes, counts };
}

async function openPane(server, { open = 2 } = {}) {
  const page = loadPage({ fetch: server.fetch });
  page.socket.open();
  page.socket.message({ type: 'update', items: [{ op: 'decisions_open', count: open }] });
  await page.settle();
  page.$('#view-decisions-btn').click();
  await page.settle();
  return page;
}

async function openDecision(page, id) {
  page.$(`.dec-row[data-decision-id="${id}"]`).click();
  await page.settle();
}

// Another client answers: the server's record changes and the live open count drops, which
// is the only push the pane gets.
async function answerElsewhere(page, server, id, optionKey) {
  const d = server.byId.get(id);
  d.status = 'ANSWERED';
  d.answer = { option: optionKey, text: '', rationale: '', answered_by: 'owner', answered_at: '2026-09-26T10:26:20+00:00' };
  page.socket.message({ type: 'update', items: [{ op: 'decisions_open', count: server.counts().open }] });
  await page.settle();
}

function detailText(page) {
  return page.$('#decisions-detail').textContent.replace(/\s+/g, ' ');
}

function hasHiddenAncestor(el) {
  for (let n = el; n && n.classList; n = n.parentNode) if (n.classList.contains('hidden')) return true;
  return false;
}

test('Answer and Withdraw… are one pair of controls: a bar fixed to the viewport bottom below sm, under the fields from sm up', async () => {
  const page = await openPane(fakeServer([d5(), d6()]));
  await openDecision(page, 'decision-D5');

  const bars = page.$$('.dec-answer-actions');
  assert.equal(bars.length, 1, 'one set of controls at every width');
  const bar = bars[0];
  for (const cls of ['fixed', 'bottom-0', 'sm:static']) assert.ok(bar.classList.contains(cls), `bar carries ${cls}`);
  assert.deepEqual(bar.querySelectorAll('button').map((b) => b.textContent.trim()), ['Answer', 'Withdraw…']);
  assert.equal(page.$$('.dec-answer-submit').length, 1);
  assert.equal(page.$$('.dec-withdraw-btn').length, 1);
  assert.ok(bar.querySelector('.dec-answer-pick'), 'the bar names the picked answer');
  for (const sel of ['[role="radiogroup"]', '.dec-custom-text', '.dec-rationale']) {
    assert.equal(bar.querySelector(sel), null, `${sel} scrolls with the detail, not in the bar`);
    assert.ok(page.$(`#decisions-detail ${sel}`), `${sel} is still in the detail`);
  }
  assert.ok(page.$('#decisions-detail').classList.contains('pb-[140px]'), 'the detail keeps 140px clear of the bar below sm');
});

test('below sm the page is the pane\'s one scroll with the list out of it; from sm up the list and the detail scroll on their own', () => {
  const page = loadPage();
  const pane = page.$('#decisions-pane');
  const aside = pane.querySelector('aside');
  const list = page.$('#decisions-list');
  const detail = page.$('#decisions-detail');
  assert.ok(pane.classList.contains('overflow-y-auto') && pane.classList.contains('sm:overflow-hidden'));
  assert.ok(aside.classList.contains('hidden') && aside.classList.contains('sm:flex'), 'the list column shows from sm up only');
  assert.ok(list.classList.contains('overflow-y-auto'), 'the list scrolls on its own, beside the detail or in the drawer');
  assert.ok(!detail.classList.contains('overflow-y-auto') && detail.classList.contains('sm:overflow-y-auto'));
});

test('the owner\'s own answer never reads as closed elsewhere, even when its push lands first', async () => {
  const server = fakeServer([d5(), d6()]);
  const page = await openPane(server);
  await openDecision(page, 'decision-D5');
  page.$('.dec-option-card[data-option-key="changes"]').click();
  page.$('.dec-answer-form').dispatchEvent(new page.window.Event('submit'));
  page.socket.message({ type: 'update', items: [{ op: 'decisions_open', count: 1 }] });
  await page.settle();
  assert.deepEqual(server.writes.map((w) => [w.verb, w.body.option]), [['answer', 'changes']]);
  const toastEl = page.$('#toast-root .toast');
  assert.equal(toastEl.getAttribute('data-tone'), 'success');
  assert.equal(toastEl.textContent.trim(), 'decision-D5 answered: Authorize with changes');
  assert.equal(page.$('.toast-action'), null, 'no Show offered for the owner\'s own answer');
});

test('a failed detail fetch shows the request\'s message with Retry, and a real 404 reads "<id> not found."', async () => {
  const server = fakeServer([d5(), d6()]);
  const page = await openPane(server);
  server.failures.set('decision-D6', 503);
  await openDecision(page, 'decision-D6');
  const alert = page.$('#decisions-detail [role="alert"]');
  assert.equal(alert.getAttribute('data-pane-state'), 'error');
  assert.equal(alert.querySelector('p').textContent, 'Service Unavailable');

  server.failures.set('decision-D6', 'network');
  page.$('#decisions-detail .pane-retry').click();
  await page.settle();
  assert.equal(page.$('#decisions-detail [role="alert"] p').textContent, 'Network error: could not reach the server.');

  server.failures.delete('decision-D6');
  page.$('#decisions-detail .pane-retry').click();
  await page.settle();
  assert.match(detailText(page), /Approve the decisions pane frames/, 'Retry recovers');

  server.byId.delete('decision-D5');
  await openDecision(page, 'decision-D5');
  assert.equal(detailText(page).trim(), 'decision-D5 not found.');
});

test('with nothing picked, Answer is disabled with no hint; a pick or a custom answer enables it and names itself in the bar', async () => {
  const page = await openPane(fakeServer([d5(), d6()]));
  await openDecision(page, 'decision-D5');
  const submit = page.$('.dec-answer-submit');
  const pickLine = page.$('.dec-answer-pick-line');
  const pick = page.$('.dec-answer-pick');
  assert.ok(submit.disabled);
  assert.equal(page.$('.dec-answer-hint'), null, 'no hint line');
  assert.ok(!pickLine.classList.contains('max-sm:flex'), 'the bar shows no pick line with nothing picked');

  page.$('.dec-option-card[data-option-key="authorize"]').click();
  assert.ok(!submit.disabled);
  assert.ok(pickLine.classList.contains('max-sm:flex'));
  assert.equal(pick.textContent, 'Authorize as designed');

  type(page.$('.dec-custom-text'), 'Ship it after the export fix');
  assert.equal(pick.textContent, 'Ship it after the export fix', 'a custom answer is written instead of the option');
  assert.equal(page.$('.dec-option-card[aria-checked="true"]'), null);
  assert.ok(!submit.disabled);
  type(page.$('.dec-custom-text'), '');
  assert.ok(submit.disabled, 'clearing the custom answer disables Answer again');
  assert.ok(!pickLine.classList.contains('max-sm:flex'));
});

test('each blocked node is one line: status icon, kind badge on a container, id, title truncating, and a chevron; the id link spans the row', async () => {
  const page = await openPane(fakeServer([d5(), d6()]));
  await openDecision(page, 'decision-D5');
  const rows = page.$$('.dec-waiting-row');
  assert.equal(rows.length, 1);
  const icon = rows[0].querySelector('[role="img"]');
  assert.equal(icon.getAttribute('aria-label'), 'Awaiting Decision');
  assert.equal(rows[0].querySelector('.kind-badge').textContent, 'SPEC');
  const link = rows[0].querySelector('a.id-link');
  assert.equal(link.textContent, 'DECIDE');
  assert.ok(link.className.split(/\s+/).includes('after:inset-0'), 'the link covers the row');
  const title = rows[0].querySelector('.dec-waiting-title');
  assert.equal(title.textContent, 'Decisions UX');
  assert.ok(title.classList.contains('truncate'), 'the title truncates on its one line');
  assert.ok(rows[0].querySelector('use[href="#icon-chevron-right"]'), 'a chevron closes the row');
});

test('options are a labelled radiogroup: radio dot, label and Recommended, description, then the effect pill', async () => {
  const page = await openPane(fakeServer([d5(), d6()]));
  await openDecision(page, 'decision-D5');
  const group = page.$('[role="radiogroup"]');
  assert.equal(page.$(`#${group.getAttribute('aria-labelledby')}`).textContent, 'Options');
  const cards = group.querySelectorAll('.dec-option-card');
  assert.equal(cards.length, 3);
  for (const card of cards) {
    assert.equal(card.getAttribute('role'), 'radio');
    assert.ok(card.querySelector('.dec-radio-dot'), 'each option has a radio dot');
  }
  const order = (card) => card.querySelectorAll('.dec-option-desc, span').map((el) => el.textContent.trim()).filter(Boolean);
  const first = order(cards[0]);
  assert.ok(first.indexOf('Recommended') < first.findIndex((t) => t.startsWith('Every DECIDE')));
  assert.equal(first.at(-1), 'No effect on waiting nodes', 'the effect pill comes last');
  assert.equal(order(cards[2]).at(-1), 'Abandons 1 waiting node');
});

test('every tab carries its count as a pill, Open live from the store and the rest from the endpoint\'s counts', async () => {
  const server = fakeServer([d5(), d6(), { ...d6(), id: 'decision-D1', status: 'ANSWERED' }, { ...d6(), id: 'decision-D2', status: 'ANSWERED' }]);
  const page = await openPane(server);
  const pill = (tab) => page.$(`#dec-tab-${tab} .dec-tab-count`);
  assert.equal(pill('open').textContent, '2');
  assert.equal(pill('answered').textContent, '2');
  assert.equal(pill('withdrawn').textContent, '0');
  assert.ok(pill('open').classList.contains('rounded-full'));
  assert.equal(page.$('#dec-tab-open').textContent.replace(/\s+/g, ' ').trim(), 'Open 2');
});

for (const [view, button, inDrawer] of [['Waves', 'view-waves-btn', true], ['Graph', 'view-graph-btn', true], ['Document', 'view-doc-btn', false]]) {
  test(`a blocked node opened from the Decisions view reopens ${view} on that node${inDrawer ? ', in the detail drawer' : ', with no drawer'}`, async () => {
    const page = await openPane(fakeServer([d5(), d6()]));
    page.$(`#${button}`).click();
    page.$('#view-decisions-btn').click();
    await page.settle();
    await openDecision(page, 'decision-D5');
    page.$('.dec-waiting-row a.id-link').click();
    await page.settle();

    assert.ok(page.$(`#${button}`).className.includes('bg-zinc-800'), `${view} is the selected view again`);
    assert.equal(page.window.location.pathname, `/${view.toLowerCase()}/DECIDE`);
    const drawer = page.$('#graph-inspector');
    if (inDrawer) {
      assert.ok(!hasHiddenAncestor(drawer), 'the drawer is open and nothing around it is hidden');
      assert.equal(page.$('#inspector-id').textContent, 'DECIDE');
    } else {
      assert.ok(drawer.classList.contains('hidden'), 'Document reveals the node in place');
    }
  });
}

test('entering the Decisions view closes an open drawer', async () => {
  const page = await openPane(fakeServer([d5(), d6()]));
  page.$('#view-graph-btn').click();
  page.$('#view-decisions-btn').click();
  await page.settle();
  await openDecision(page, 'decision-D5');
  page.$('.dec-waiting-row a.id-link').click();
  await page.settle();
  assert.ok(!page.$('#graph-inspector').classList.contains('hidden'));
  page.$('#view-decisions-btn').click();
  await page.settle();
  assert.ok(page.$('#graph-inspector').classList.contains('hidden'));
});

function zinc500Texts(root) {
  return root.querySelectorAll('*').filter((el) => el.classList.contains('text-zinc-500')).map((el) => el.outerHTML.slice(0, 80));
}

test('no text in the pane uses zinc-500 in any state: empty, loading, placeholder, detail, answered, error', async () => {
  const server = fakeServer([d5()]);
  const page = await openPane(server, { open: 1 });
  const pane = page.$('#decisions-pane');
  assert.deepEqual(zinc500Texts(pane), [], 'open detail');
  for (const ta of pane.querySelectorAll('textarea')) assert.ok(!ta.classList.contains('placeholder-zinc-500'), 'placeholders meet AA too');
  await answerElsewhere(page, server, 'decision-D5', 'authorize');
  assert.deepEqual(zinc500Texts(pane), [], 'held form');
  page.$('#toast-root .toast-action').click();
  await page.settle();
  assert.deepEqual(zinc500Texts(pane), [], 'answered detail');
  page.$('#dec-tab-open').click();
  page.$('#dec-tab-open').click();
  assert.deepEqual(zinc500Texts(pane), [], 'loading state');
  await page.settle();
  assert.match(page.$('#decisions-list').textContent, /No open decisions\./);
  assert.deepEqual(zinc500Texts(pane), [], 'empty list');
  server.byId.set('decision-D5', d5());
  server.failures.set('decision-D5', 503);
  page.$('#dec-tab-open').click();
  await page.settle();
  assert.match(detailText(page), /Service Unavailable/);
  assert.deepEqual(zinc500Texts(pane), [], 'error state');
});

test('options follow the WAI-ARIA radio pattern: one tab stop, Right and Left move and check, wrapping', async () => {
  const page = await openPane(fakeServer([d5(), d6()]));
  await openDecision(page, 'decision-D5');
  const cards = () => page.$$('.dec-option-card');
  const checked = () => cards().map((c) => c.getAttribute('aria-checked'));
  const tabStops = () => cards().filter((c) => c.getAttribute('tabindex') === '0').length;
  assert.equal(tabStops(), 1);
  assert.equal(cards()[0].getAttribute('tabindex'), '0');

  for (const [k, expected] of [['ArrowRight', 1], ['ArrowRight', 2], ['ArrowRight', 0], ['ArrowLeft', 2], ['ArrowLeft', 1]]) {
    const ev = key(page.document.activeElement.classList?.contains('dec-option-card') ? page.document.activeElement : cards()[0], k);
    assert.ok(ev.defaultPrevented, `${k} is handled`);
    assert.equal(page.document.activeElement, cards()[expected], `${k} moves focus`);
    assert.deepEqual(checked(), cards().map((_, i) => String(i === expected)), `${k} checks the focused option`);
    assert.equal(tabStops(), 1);
  }
});

test('the context renders its markdown with the app\'s type scale as classes, not raw colours', async () => {
  const page = await openPane(fakeServer([d5(), d6()]));
  await openDecision(page, 'decision-D5');
  const has = (sel, classes) => {
    const el = page.$(`.dec-context-body ${sel}`);
    assert.ok(el, `${sel} rendered`);
    for (const c of classes) assert.ok(el.classList.contains(c), `${sel} carries ${c}`);
  };
  // The page loads marked from /vendor, which the harness does not run; this stand-in turns
  // the fixture's headings, numbered items, code spans and paragraphs into the tags marked
  // would emit.
  page.run(`var marked = { parse: (md) => md.split('\\n\\n').map((block) => {
    const inline = (t) => t.replace(/\`([^\`]+)\`/g, '<code>$1</code>');
    if (block.startsWith('### ')) return '<h3>' + inline(block.slice(4)) + '</h3>';
    if (block.startsWith('## ')) return '<h2>' + inline(block.slice(3)) + '</h2>';
    if (/^\\d+\\. /.test(block)) return '<ol>' + block.split('\\n').map((l) => '<li>' + inline(l.replace(/^\\d+\\. /, '')) + '</li>').join('') + '</ol>';
    return '<p>' + block.split('\\n').map(inline).join('<br>') + '</p>';
  }).join('') };`);
  page.$('.dec-row[data-decision-id="decision-D6"]').click();
  await page.settle();
  await openDecision(page, 'decision-D5');
  has('h2', ['text-base', 'leading-6', 'font-bold', 'text-white']);
  has('h3', ['text-sm', 'leading-5', 'font-semibold', 'text-zinc-100']);
  has('p', ['text-xs', 'leading-5', 'text-zinc-300']);
  has('ol', ['list-decimal', 'pl-5', 'marker:text-zinc-400']);
  has('li', ['text-xs', 'text-zinc-300']);
  has('code', ['font-mono', 'text-emerald-300']);
  const paragraph = page.$('.dec-context-body p');
  assert.equal(paragraph.querySelector('br'), null, 'a hard-wrapped paragraph reflows');
  assert.equal(paragraph.textContent, 'The decisions pane is redesigned for the owner\'s queue: answering, reading options and their effects.');
  assert.equal(page.document.querySelector('.dec-context-body').closest('details'), null, 'the context is never folded into a collapsed section');
  const column = page.$('#decisions-detail .max-w-2xl');
  const order = column.children.map((c) => (c.querySelector('.dec-context-body') ? 'context' : c.querySelector('.dec-waiting-row') ? 'waiting' : c.classList.contains('dec-answer-form') ? 'form' : null)).filter(Boolean);
  assert.deepEqual(order, ['context', 'waiting', 'form'], 'context reads first, above the blocked nodes and the options');
});

test('rows, tabs, Answer, Withdraw and Reopen each show a themed focus ring and a pressed state', async () => {
  const ring = (el) => (el.classList.contains('dec-row')
    ? el.classList.contains('focus-visible:after:ring-2') && el.classList.contains('focus-visible:after:ring-emerald-400')
    : el.classList.contains('focus-visible:ring-2') && el.classList.contains('focus-visible:ring-emerald-500'));
  // A row's pressed state is its line's, which its title's overlay covers.
  const pressable = (el) => (el.classList.contains('dec-row') ? el.parentNode : el);
  const server = fakeServer([d5(), d6()]);
  const page = await openPane(server);
  await openDecision(page, 'decision-D5');
  const controls = [
    ['row', page.$$('.dec-row').find((r) => r.getAttribute('data-decision-id') === 'decision-D6')],
    ['tab', page.$('#dec-tab-answered')],
    ['Answer', page.$('.dec-answer-submit')],
    ['Withdraw', page.$('.dec-withdraw-btn')],
  ];
  await answerElsewhere(page, server, 'decision-D5', 'authorize');
  page.$('#toast-root .toast-action').click();
  await page.settle();
  controls.push(['Reopen', page.$('.dec-reopen-btn')]);
  for (const [name, el] of controls) {
    assert.ok(ring(el), `${name} has a focus ring`);
    assert.ok(pressable(el).className.split(/\s+/).some((c) => c.startsWith('active:')), `${name} has a pressed state`);
  }
});

test('switching tabs selects the new tab\'s top row in place of the old tab\'s', async () => {
  const server = fakeServer([d5(), d6(), { ...d6(), id: 'decision-D1', status: 'ANSWERED', title: 'An answered one' }]);
  const page = await openPane(server);
  page.$('#dec-tab-answered').click();
  await page.settle();
  assert.equal(page.window.location.pathname, '/decisions/decision-D1');
  assert.match(detailText(page), /An answered one/);
  page.$('#dec-tab-open').click();
  await page.settle();
  assert.equal(page.window.location.pathname, '/decisions/decision-D5');
  assert.match(detailText(page), /Authorize spec DECIDE/);
});

test('the Decisions segment\'s accessible name carries the live open count', () => {
  const page = loadPage();
  page.socket.open();
  page.socket.message({ type: 'update', items: [{ op: 'decisions_open', count: 2 }] });
  assert.equal(page.$('#view-decisions-btn').getAttribute('aria-label'), 'Decisions view, 2 open');
  page.socket.message({ type: 'update', items: [{ op: 'decisions_open', count: 0 }] });
  assert.equal(page.$('#view-decisions-btn').getAttribute('aria-label'), 'Decisions view, 0 open');
});
