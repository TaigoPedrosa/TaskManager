import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const jsDir = path.join(here, '../../src/taskmanager/web/static/js');
const STORE_SRC = fs.readFileSync(path.join(jsDir, 'store.js'), 'utf8');
const CORE_SRC = fs.readFileSync(path.join(jsDir, 'core.js'), 'utf8');
const DECISIONS_SRC = fs.readFileSync(path.join(jsDir, 'decisions.js'), 'utf8');
const INDEX_HTML = fs.readFileSync(
  path.join(here, '../../src/taskmanager/web/static/index.html'),
  'utf8',
);

// A minimal stand-in for the browser's WebSocket (store.test.mjs's own shape): opens and
// carries a message on request, so the live badge path (decisions_open arriving with a
// snapshot/update) can be driven without a real server.
class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = FakeSocket.CONNECTING;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    FakeSocket.instances.push(this);
  }

  send() {}

  open() {
    this.readyState = FakeSocket.OPEN;
    return this.onopen && this.onopen({});
  }

  message(obj) {
    return this.onmessage && this.onmessage({ data: JSON.stringify(obj) });
  }

  close() {
    this.readyState = FakeSocket.CLOSED;
    return this.onclose && this.onclose({});
  }
}
FakeSocket.CONNECTING = 0;
FakeSocket.OPEN = 1;
FakeSocket.CLOSED = 3;

// A real element's className and classList read/write the same underlying token set; the
// page's own code mixes both styles (a switcher segment's className is reassigned wholesale,
// a pane's hidden class is toggled), so keeping them independent would let a broken toggle
// pass silently.
function makeElement(id) {
  const classes = new Set();
  const listeners = {};
  const el = {
    id,
    innerHTML: '',
    textContent: '',
    style: {},
    get className() { return [...classes].join(' '); },
    set className(v) { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
    classList: {
      add: (...c) => c.forEach((x) => classes.add(x)),
      remove: (...c) => c.forEach((x) => classes.delete(x)),
      toggle: (c, force) => {
        const on = force === undefined ? !classes.has(c) : force;
        el.classList[on ? 'add' : 'remove'](c);
        return on;
      },
      contains: (c) => classes.has(c),
    },
    addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
    removeEventListener() {},
    click() { (listeners.click || []).forEach((fn) => fn()); },
    querySelector: () => null,
    querySelectorAll: () => [],
    setAttribute() {}, getAttribute: () => null,
    appendChild() {}, remove() { el.removed = true; },
    getBoundingClientRect: () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }),
  };
  return el;
}

// Loads store.js, core.js and decisions.js into one vm context, same relationship the real
// page gives them (classic scripts sharing a global scope, in this same order) -- the switcher
// is core.js's own three buttons plus decisions.js's wrap for the fourth, so neither file alone
// exercises the real behaviour.
function freshContext() {
  FakeSocket.instances = [];
  const elementsById = new Map();
  const fetchCalls = [];
  const sandbox = {
    console,
    window: null,
    document: {
      getElementById(id) {
        if (!elementsById.has(id)) elementsById.set(id, makeElement(id));
        return elementsById.get(id);
      },
      createElement: () => makeElement(),
    },
    WebSocket: FakeSocket,
    location: { protocol: 'http:', host: 'test' },
    requestAnimationFrame: () => {},
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
    URLSearchParams,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    fetch: async (url) => {
      fetchCalls.push(url);
      return { ok: true, status: 200, text: async () => JSON.stringify({ items: [], next: null }) };
    },
  };
  sandbox.window = sandbox;
  sandbox.window.VIEW_MODES = { DOCUMENT: 'document', GRAPH: 'graph', WAVES: 'waves' };
  const context = vm.createContext(sandbox);
  vm.runInContext(STORE_SRC, context, { filename: 'store.js' });
  vm.runInContext(CORE_SRC, context, { filename: 'core.js' });
  vm.runInContext(DECISIONS_SRC, context, { filename: 'decisions.js' });
  context.el = (id) => elementsById.get(id);
  context.fetchCalls = fetchCalls;
  context.socket = FakeSocket.instances[0];
  return context;
}

function paneVisibility(ctx) {
  return {
    waves: !ctx.el('waves-pane').classList.contains('hidden'),
    document: !ctx.el('document-pane').classList.contains('hidden'),
    networkCanvas: !ctx.el('network-canvas').classList.contains('hidden'),
    sidebar: !ctx.el('sidebar-pane').classList.contains('hidden'),
    decisions: !ctx.el('decisions-pane').classList.contains('hidden'),
  };
}

function selected(ctx, id) {
  // The active segment's className carries bg-zinc-800 + text-white (VIEW_BTN_ACTIVE); every
  // other segment carries neither.
  return ctx.el(id).className.includes('bg-zinc-800') && ctx.el(id).className.includes('text-white');
}

test('the switcher opens on Waves, and clicking each of the four segments shows its own pane and selects only itself', async () => {
  const ctx = freshContext();
  // The page's default pane visibility comes from index.html's own baked classes, which this
  // JS-only harness does not load; calling setViewMode(WAVES) once reaches the exact state a
  // freshly loaded page shows, through the real function rather than a seeded guess.
  ctx.setViewMode(ctx.window.VIEW_MODES.WAVES);

  assert.deepEqual(paneVisibility(ctx), { waves: true, document: false, networkCanvas: false, sidebar: false, decisions: false });
  assert.ok(selected(ctx, 'view-waves-btn'));
  for (const id of ['view-graph-btn', 'view-doc-btn', 'view-decisions-btn']) assert.ok(!selected(ctx, id), `${id} should not start selected`);

  ctx.el('view-graph-btn').click();
  assert.deepEqual(paneVisibility(ctx), { waves: false, document: false, networkCanvas: true, sidebar: true, decisions: false });
  assert.ok(selected(ctx, 'view-graph-btn'));
  assert.ok(!selected(ctx, 'view-waves-btn') && !selected(ctx, 'view-doc-btn') && !selected(ctx, 'view-decisions-btn'));

  ctx.el('view-doc-btn').click();
  assert.deepEqual(paneVisibility(ctx), { waves: false, document: true, networkCanvas: false, sidebar: false, decisions: false });
  assert.ok(selected(ctx, 'view-doc-btn'));
  assert.equal(ctx.el('toggle-sections-btn').classList.contains('hidden'), false, 'the section-toggle only reappears in Document view');

  ctx.el('view-decisions-btn').click();
  await new Promise((r) => setTimeout(r, 0)); // refreshDecisionsData()'s fetch
  assert.deepEqual(paneVisibility(ctx), { waves: false, document: false, networkCanvas: false, sidebar: false, decisions: true });
  assert.ok(selected(ctx, 'view-decisions-btn'));
  assert.ok(!selected(ctx, 'view-waves-btn') && !selected(ctx, 'view-graph-btn') && !selected(ctx, 'view-doc-btn'));
  assert.equal(ctx.el('toggle-sections-btn').classList.contains('hidden'), true);
  assert.ok(ctx.fetchCalls.some((u) => u.startsWith('/api/decisions')), 'opening Decisions fetches its data');

  ctx.el('view-waves-btn').click();
  assert.deepEqual(paneVisibility(ctx), { waves: true, document: false, networkCanvas: false, sidebar: false, decisions: false });
  assert.ok(selected(ctx, 'view-waves-btn'));
  assert.ok(!selected(ctx, 'view-decisions-btn'), 'leaving Decisions clears its selected state too');
});

test('the Decisions badge is hidden at zero and updates live off window.tmStore, capping at 99+', () => {
  const ctx = freshContext();
  const badge = ctx.el('decisions-badge');
  ctx.updateDecisionsBadge(); // the page's own load-time state, decisionsOpen still at 0
  assert.equal(badge.classList.contains('hidden'), true, 'no open decisions yet');

  ctx.socket.message({ type: 'update', items: [{ op: 'decisions_open', count: 2 }] });
  assert.equal(badge.classList.contains('hidden'), false);
  assert.equal(badge.textContent, '2');

  ctx.socket.message({ type: 'update', items: [{ op: 'decisions_open', count: 150 }] });
  assert.equal(badge.textContent, '99+');

  ctx.socket.message({ type: 'update', items: [{ op: 'decisions_open', count: 0 }] });
  assert.equal(badge.classList.contains('hidden'), true, 'back to zero hides it again');
});

test('the switcher is one four-segment control: Waves, Graph, Document, Decisions in order, each named "<View> view"', () => {
  const switcherMatch = INDEX_HTML.match(/<div class="h-8 bg-zinc-900[^"]*">([\s\S]*?)<\/div>\s*\n\s*<div class="relative flex-1/);
  assert.ok(switcherMatch, 'view switcher container not found ahead of the search box');
  const ids = [...switcherMatch[1].matchAll(/<button id="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(ids, ['view-waves-btn', 'view-graph-btn', 'view-doc-btn', 'view-decisions-btn']);
  for (const [id, label] of [
    ['view-waves-btn', 'Waves view'], ['view-graph-btn', 'Graph view'],
    ['view-doc-btn', 'Document view'], ['view-decisions-btn', 'Decisions view'],
  ]) {
    assert.match(switcherMatch[1], new RegExp(`id="${id}" title="${label}" aria-label="${label}"`));
  }
});

test('#view-decisions-btn is the switcher\'s own static segment, not injected, and view-extra-buttons is gone', () => {
  assert.equal((INDEX_HTML.match(/id="view-decisions-btn"/g) || []).length, 1);
  assert.ok(!INDEX_HTML.includes('view-extra-buttons'));
  assert.ok(!DECISIONS_SRC.includes('view-extra-buttons'));
  assert.ok(!DECISIONS_SRC.includes('renderDecisionsToolbarButton'), 'the button is no longer injected at runtime');
});
