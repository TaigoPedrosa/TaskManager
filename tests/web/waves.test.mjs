import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const WAVES_SRC = fs.readFileSync(
  path.join(here, '../../src/taskmanager/web/static/js/waves.js'),
  'utf8',
);

// Every status code waves.js's own chip builders might be asked for resolves to the same
// stand-in theme: these tests never assert on a chip's rendered label or colour, only on
// depth/reset/refetch, so a real theme table (window.STATUS_THEMES, core.js) is pure ballast.
const FAKE_THEME = { code: 'X', label: 'X', icon: 'circle', description: '' };

class FakeElement {
  constructor() {
    this.innerHTML = '';
  }

  querySelector() {
    return null;
  }

  querySelectorAll() {
    return [];
  }
}

class FakeStore {
  constructor() {
    this.listeners = [];
  }

  onChange(fn) {
    this.listeners.push(fn);
    return () => {
      this.listeners = this.listeners.filter((l) => l !== fn);
    };
  }

  emit(patch) {
    this.listeners.forEach((fn) => fn(patch));
  }
}

function metaResponse() {
  return { ok: true, status: 200, json: async () => ({ dispatch: { wave_size: 5, tick_budget: 20 } }) };
}

function oneEntryWave() {
  return {
    waves: [
      {
        entries: [
          {
            id: 'S-P-T', title: 'T', kind: 'task', action: 'implement', model: 'sonnet',
            repos: ['api'], status_before: 'READY', status_after: 'IMPLEMENTED', in_flight: false,
          },
        ],
        held: [],
      },
    ],
  };
}

function makeFakeFetch(wavesPayload) {
  const calls = [];
  const fn = async (url) => {
    calls.push(url);
    if (url.startsWith('/api/meta')) return metaResponse();
    return { ok: true, status: 200, json: async () => wavesPayload(url) };
  };
  fn.calls = calls;
  return fn;
}

// waves.js is a classic script (not a module): run it in its own vm context per test, with a
// fake store and fetch already in place before it runs its own top-level initWaves(). A `let`
// declared inside the script (waveDepth, waveData, ...) never becomes a context property --
// only its top-level `function`s do -- so every assertion below reads observable behaviour
// (a fetch call's URL, #waves-content's rendered HTML) rather than that internal state.
function freshContext(wavesPayload, { isStaticMode = false } = {}) {
  const contentEl = new FakeElement();
  let rafQueue = [];
  const loadPendingCalls = [];
  const sandbox = {
    console,
    setTimeout: globalThis.setTimeout,
    URLSearchParams,
    requestAnimationFrame: (fn) => rafQueue.push(fn),
    document: { getElementById: (id) => (id === 'waves-content' ? contentEl : new FakeElement()) },
    fetch: makeFakeFetch(wavesPayload || oneEntryWave),
    filters: { specMode: new Map() },
    esc: (s) => String(s ?? ''),
    renderIcon: (name) => `<svg data-icon="${name}"></svg>`,
    getTheme: () => FAKE_THEME,
    statusChip: () => '',
    showGraphInspector: () => {},
    setWavesLoadPending: (v) => loadPendingCalls.push(v),
    isStaticMode,
  };
  sandbox.window = sandbox;
  sandbox.window.tmStore = new FakeStore();
  const context = vm.createContext(sandbox);
  vm.runInContext(WAVES_SRC, context, { filename: 'waves.js' });
  // Set from outside, after the script ran: a plain context property, not a `let` the script
  // itself declared, so it is visible here exactly like `fetch`/`document` above.
  context.flushRaf = () => {
    const queue = rafQueue;
    rafQueue = [];
    queue.forEach((fn) => fn());
  };
  context.contentHtml = () => contentEl.innerHTML;
  context.loadPendingCalls = loadPendingCalls;
  return context;
}

async function flushAsync() {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

function wavesCalls(ctx) {
  return ctx.fetch.calls.filter((u) => u.startsWith('/api/waves'));
}

function depthOf(url) {
  return new URLSearchParams(url.split('?')[1]).get('depth');
}

test('the page opens on wave 1, sized from /api/meta rather than a constant', async () => {
  const ctx = freshContext();
  await flushAsync();
  const calls = wavesCalls(ctx);
  assert.equal(calls.length, 1);
  assert.equal(depthOf(calls[0]), '1');
  assert.equal(new URLSearchParams(calls[0].split('?')[1]).get('size'), '5');
});

test('computeNextWave requests depth+1 each time, and is a no-op once the last wave is empty', async () => {
  const ctx = freshContext();
  await flushAsync();

  ctx.computeNextWave();
  await flushAsync();
  assert.equal(depthOf(wavesCalls(ctx).at(-1)), '2');

  ctx.computeNextWave();
  await flushAsync();
  assert.equal(depthOf(wavesCalls(ctx).at(-1)), '3');

  const emptyCtx = freshContext(() => ({ waves: [{ entries: [], held: [] }] }));
  await flushAsync();
  const countBefore = wavesCalls(emptyCtx).length;
  emptyCtx.computeNextWave();
  await flushAsync();
  assert.equal(wavesCalls(emptyCtx).length, countBefore, 'an empty last wave disables compute');
});

test('resetWaves always requests depth 1, however deep the last computed wave was', async () => {
  const ctx = freshContext();
  await flushAsync();
  ctx.computeNextWave();
  await flushAsync();
  ctx.computeNextWave();
  await flushAsync();
  assert.equal(depthOf(wavesCalls(ctx).at(-1)), '3');

  ctx.resetWaves();
  await flushAsync();
  assert.equal(depthOf(wavesCalls(ctx).at(-1)), '1');
});

test('a statusesChanged patch refetches at the current depth, coalesced to one fetch per frame', async () => {
  const ctx = freshContext();
  await flushAsync();
  ctx.computeNextWave();
  await flushAsync();
  const depthNow = depthOf(wavesCalls(ctx).at(-1));
  assert.equal(depthNow, '2');

  const before = wavesCalls(ctx).length;
  ctx.window.tmStore.emit({ statusesChanged: true });
  ctx.window.tmStore.emit({ statusesChanged: true });
  ctx.window.tmStore.emit({ statusesChanged: false }); // no counts-tree change: no refetch
  assert.equal(wavesCalls(ctx).length, before, 'nothing fetched before the animation frame runs');

  ctx.flushRaf();
  await flushAsync();
  const after = wavesCalls(ctx);
  assert.equal(after.length, before + 1, 'two statusesChanged patches in one frame coalesce to one refetch');
  assert.equal(depthOf(after.at(-1)), depthNow, 'the refetch keeps the depth already on screen');
});

test('the size input requests with the new size and keeps the depth already on screen', async () => {
  const ctx = freshContext();
  await flushAsync();
  ctx.computeNextWave();
  await flushAsync();

  ctx.onWaveSizeChange(9);
  await flushAsync();
  const last = wavesCalls(ctx).at(-1);
  assert.equal(new URLSearchParams(last.split('?')[1]).get('size'), '9');
  assert.equal(depthOf(last), '2');
});

test('a failed request shows the error and drops the stale cards, not a silent empty wave', async () => {
  const ctx = freshContext();
  await flushAsync();
  ctx.fetch = async (url) => {
    if (url.startsWith('/api/meta')) return metaResponse();
    return { ok: false, status: 400, json: async () => ({ detail: "wave size must be 1–20 (this project's dispatch.tick_budget)." }) };
  };

  await ctx.fetchWaves();

  const html = ctx.contentHtml();
  assert.match(html, /Could not compute waves: wave size must be 1–20 \(this project's dispatch\.tick_budget\)\./);
  assert.match(html, /role="alert"/);
  assert.doesNotMatch(html, /Wave 1/);
  assert.match(html, /id="wave-size-input"[^>]*border-red-700/, 'a size-range refusal marks the size input itself invalid');
});

test('every fetch drives the shared load indicator, not only the first', async () => {
  const ctx = freshContext();
  await flushAsync();
  assert.deepEqual(ctx.loadPendingCalls, [true, false], 'the initial /api/waves fetch');

  ctx.computeNextWave();
  await flushAsync();
  assert.deepEqual(ctx.loadPendingCalls, [true, false, true, false], 'a later fetch pends the indicator again');
});

test('a static export never calls /api/meta or /api/waves, on load or on refetch', async () => {
  const ctx = freshContext(undefined, { isStaticMode: true });
  await flushAsync();
  assert.equal(ctx.fetch.calls.length, 0, 'initWaves fired no request');
  assert.equal(ctx.contentHtml(), '', 'no Waves message is rendered');

  ctx.window.tmStore.emit({ statusesChanged: true });
  ctx.flushRaf();
  await flushAsync();
  assert.equal(ctx.fetch.calls.length, 0, 'a later store change still fires nothing');
});

function classOf(html, pattern) {
  const m = html.match(pattern);
  assert.ok(m, `no element matches ${pattern}`);
  return m[1].split(/\s+/);
}

test('the wave card, task card, held list and controls follow the frame anatomy', async () => {
  const ctx = freshContext(() => ({
    waves: [
      {
        entries: [
          {
            id: 'S-P-T', title: 'The title row', kind: 'task', action: 'implement', model: 'sonnet',
            repos: ['api'], status_before: 'READY', status_after: 'IMPLEMENTED', in_flight: true,
          },
        ],
        held: ['S-P-U: waits on S-P-T'],
      },
    ],
  }));
  await flushAsync();
  const html = ctx.contentHtml();

  const title = classOf(html, /<h3 class="([^"]*)">Wave 1<\/h3>/);
  for (const c of ['uppercase', 'text-emerald-400']) assert.ok(title.includes(c), `wave title lacks ${c}`);
  const head = classOf(html, /<div class="(wave-head [^"]*)">/);
  for (const c of ['font-mono', 'bg-zinc-900/95', 'border-b']) assert.ok(head.includes(c), `wave header band lacks ${c}`);

  const entryHead = html.slice(html.indexOf('wave-entry-head'), html.indexOf('wave-entry-body'));
  const entryBody = html.slice(html.indexOf('wave-entry-body'), html.indexOf('</button>', html.indexOf('wave-entry-body')));
  for (const text of ['S-P-T', 'in flight', 'sonnet', '>api<']) assert.ok(entryHead.includes(text), `task header band lacks ${text}`);
  assert.ok(entryBody.includes('The title row'), 'the title sits in the body row');
  assert.ok(!entryHead.includes('The title row'), 'the title is not in the header band');
  assert.match(entryBody, /→/, 'from/to chips are separated by an arrow');
  assert.doesNotMatch(entryBody, /data-icon="chevron-right"/);

  assert.match(html, /<span>Held \(1\)<\/span><svg data-icon="chevron-right">/, 'the held chevron sits after the label');
  assert.ok(classOf(html, /class="(wave-held-toggle [^"]*)"/).includes('uppercase'));
  const rows = classOf(html, /class="(wave-held-rows [^"]*)"/);
  for (const c of ['border', 'rounded-lg', 'font-mono']) assert.ok(rows.includes(c), `held rows lack ${c}`);

  assert.ok(classOf(html, /class="(wave-controls [^"]*)"/).includes('justify-between'));
  assert.match(html, /id="wave-size-caption"[^>]*>[^<]*<\/span>\s*<\/div>\s*<button id="wave-reset-btn"/, 'Reset sits apart from the caption, at the row end');
  assert.ok(classOf(html, /id="wave-size-caption" class="([^"]*)"/).includes('text-zinc-400'), 'caption meets AA contrast on zinc-950');
});
