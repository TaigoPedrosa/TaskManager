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

// The shared renderers (core.js) are stand-ins here that echo their inputs: these tests read
// what waves.js asks them for, and tests/web/kit.test.mjs reads the renderers themselves.

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

// #waves-content itself: every render replaces its markup, and with it the controls waves.js
// wires, so a control is handed out only while its id is in the current markup, and its
// listeners belong to that render alone -- as a real re-render's fresh nodes would.
class FakeContent {
  constructor() {
    this.html = '';
    this.controls = new Map();
  }

  set innerHTML(html) {
    this.html = html;
    this.controls = new Map();
  }

  get innerHTML() {
    return this.html;
  }

  querySelector(selector) {
    const id = selector.startsWith('#') ? selector.slice(1) : null;
    if (!id || !this.html.includes(`id="${id}"`)) return null;
    if (!this.controls.has(id)) {
      const listeners = {};
      this.controls.set(id, {
        value: '',
        addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
        dispatch: (type) => (listeners[type] || []).forEach((fn) => fn()),
      });
    }
    return this.controls.get(id);
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
// fake store and fetch already in place, then show the view (initWaves) unless `shown` is
// false. A `let`
// declared inside the script (waveDepth, waveData, ...) never becomes a context property --
// only its top-level `function`s do -- so every assertion below reads observable behaviour
// (a fetch call's URL, #waves-content's rendered HTML) rather than that internal state.
function freshContext(wavesPayload, { isStaticMode = false, shown = true, archived = false } = {}) {
  const contentEl = new FakeContent();
  let rafQueue = [];
  const loadPendingCalls = [];
  const sandbox = {
    console,
    setTimeout: globalThis.setTimeout,
    URLSearchParams,
    requestAnimationFrame: (fn) => rafQueue.push(fn),
    document: { getElementById: (id) => (id === 'waves-content' ? contentEl : new FakeElement()) },
    fetch: makeFakeFetch(wavesPayload || oneEntryWave),
    filters: { specMode: new Map(), archived },
    esc: (s) => String(s ?? ''),
    renderIcon: (name) => `<svg data-icon="${name}"></svg>`,
    statusIcon: (code) => `<span role="img" aria-label="${code}"></span>`,
    kindBadge: (kind) => (kind === 'task' ? '' : `<span class="kind-badge">${kind.toUpperCase()}</span>`),
    idLink: (id) => `<a class="id-link">${id}</a>`,
    leasePulse: (lease) => (lease ? `<span class="lease-pulse">${lease.agent_id}</span>` : ''),
    modelPill: (model) => `<span class="model-pill">${model}</span>`,
    repoPill: (repo) => `<span class="repo-pill">${repo}</span>`,
    disclosureHeader: (label, count, expanded, id) => `<button class="disclosure" aria-expanded="${expanded}" data-group-id="${id}">${label} ${count}</button>`,
    paneState: (kind, message = 'Loading…') => `<div data-pane-state="${kind}">${message}</div>`,
    openNode: () => {},
    setWavesLoadPending: (v) => loadPendingCalls.push(v),
    isStaticMode,
  };
  sandbox.window = sandbox;
  sandbox.window.tmStore = new FakeStore();
  const context = vm.createContext(sandbox);
  vm.runInContext(WAVES_SRC, context, { filename: 'waves.js' });
  if (shown) context.initWaves();
  // Set from outside, after the script ran: a plain context property, not a `let` the script
  // itself declared, so it is visible here exactly like `fetch`/`document` above.
  context.flushRaf = () => {
    const queue = rafQueue;
    rafQueue = [];
    queue.forEach((fn) => fn());
  };
  context.contentHtml = () => contentEl.innerHTML;
  context.control = (id) => contentEl.querySelector(`#${id}`);
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

test('nothing is fetched, refetched or rendered until the view is first shown, and only once after', async () => {
  const ctx = freshContext(undefined, { shown: false });
  ctx.window.tmStore.emit({ statusesChanged: true });
  ctx.flushRaf();
  ctx.scheduleWavesRefetch();
  ctx.flushRaf();
  await flushAsync();
  assert.deepEqual(ctx.fetch.calls, [], 'no /api/meta or /api/waves before the view is shown');
  assert.deepEqual(ctx.loadPendingCalls, [], 'the load bar never pends for a view nobody opened');
  assert.equal(ctx.contentHtml(), '');

  ctx.initWaves();
  ctx.initWaves();
  await flushAsync();
  assert.equal(ctx.fetch.calls.filter((u) => u.startsWith('/api/meta')).length, 1);
  assert.equal(wavesCalls(ctx).length, 1, 'showing the view again does not reload it');
});

test('Waves opens on wave 1, sized from /api/meta rather than a constant', async () => {
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
  assert.match(html, /data-pane-state="error"/);
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

test('an out-of-range wave size refuses locally, without ever calling /api/waves', async () => {
  const ctx = freshContext();
  await flushAsync();
  const before = wavesCalls(ctx).length;

  ctx.onWaveSizeChange(999); // waveMaxSize is 20 (metaResponse's tick_budget)
  await flushAsync();

  assert.equal(wavesCalls(ctx).length, before, 'no /api/waves request was ever made for the bad size');
  const html = ctx.contentHtml();
  assert.match(html, /Could not compute waves: wave size must be 1–20 \(this project's dispatch\.tick_budget\)\./);
  assert.match(html, /data-pane-state="error"/);
});

test('the first load shows the loading pane state, with a disabled compute button', () => {
  const ctx = freshContext();
  const html = ctx.contentHtml();
  assert.match(html, /data-pane-state="loading"/);
  assert.match(html, /id="wave-compute-btn"[^>]* disabled[^>]*>/, 'compute is disabled while the first request is in flight');
});

test("#waves-pane uses the frame's 16px gutter at 375, and 24px from sm up", () => {
  const indexHtml = fs.readFileSync(
    path.join(here, '../../src/taskmanager/web/static/index.html'),
    'utf8',
  );
  const m = indexHtml.match(/id="waves-pane" class="([^"]*)"/);
  assert.ok(m, '#waves-pane not found');
  const cls = m[1].split(/\s+/);
  assert.ok(cls.includes('p-4'), 'default (375) gutter is 16px');
  assert.ok(cls.includes('sm:p-6'), '768 and 1440 keep the 24px gutter');
  assert.ok(!cls.includes('p-6'), 'no unconditional 24px override');
});

async function typeWaveSize(ctx, value) {
  const input = ctx.control('wave-size-input');
  assert.ok(input, 'the size input is on screen');
  input.value = value;
  input.dispatch('change');
  await flushAsync();
}

test('the size input accepts only whole numbers 1..tick_budget, refusing the rest with no request', async () => {
  const ctx = freshContext();
  await flushAsync();
  const refusal = /Could not compute waves: wave size must be 1–20 \(this project's dispatch\.tick_budget\)\./;

  for (const bad of ['0', '-3', '2.5', '', 'abc', '1e1', '21']) {
    const before = wavesCalls(ctx).length;
    await typeWaveSize(ctx, bad);
    assert.equal(wavesCalls(ctx).length, before, `size ${JSON.stringify(bad)} reached /api/waves`);
    const html = ctx.contentHtml();
    assert.match(html, refusal, `size ${JSON.stringify(bad)} shows the refusal`);
    assert.doesNotMatch(html, /Wave 1</, `size ${JSON.stringify(bad)} leaves no stale wave on screen`);
  }

  for (const good of ['1', '20', ' 7 ']) {
    const before = wavesCalls(ctx).length;
    await typeWaveSize(ctx, good);
    assert.equal(wavesCalls(ctx).length, before + 1, `size ${JSON.stringify(good)} is requested`);
    assert.equal(new URLSearchParams(wavesCalls(ctx).at(-1).split('?')[1]).get('size'), good.trim());
    assert.doesNotMatch(ctx.contentHtml(), /data-pane-state="error"/);
  }
});

test("a FastAPI validation error renders its messages as text, never as [object Object]", async () => {
  const ctx = freshContext();
  await flushAsync();
  ctx.fetch = async (url) => {
    if (url.startsWith('/api/meta')) return metaResponse();
    return {
      ok: false,
      status: 422,
      json: async () => ({ detail: [{ loc: ['query', 'size'], msg: 'Input should be a valid integer', type: 'int_parsing' }] }),
    };
  };
  await ctx.fetchWaves();
  const html = ctx.contentHtml();
  assert.match(html, /Could not compute waves: Input should be a valid integer/);
  assert.doesNotMatch(html, /\[object Object\]/);
});

test('compute stops at the API depth cap, so no request ever asks past it', async () => {
  const ctx = freshContext((url) => ({
    waves: Array.from({ length: Number(depthOf(url)) }, () => oneEntryWave().waves[0]),
    max_depth: 2,
  }));
  await flushAsync();
  ctx.computeNextWave();
  await flushAsync();
  assert.equal(depthOf(wavesCalls(ctx).at(-1)), '2');
  assert.match(ctx.contentHtml(), /id="wave-compute-btn"[^>]* disabled[^>]*>/, 'compute is disabled at the cap');

  const before = wavesCalls(ctx).length;
  ctx.computeNextWave();
  await flushAsync();
  assert.equal(wavesCalls(ctx).length, before, 'no depth past the cap is requested');
});

test('the size label and input use the frame faces and colours', async () => {
  const ctx = freshContext();
  await flushAsync();
  const html = ctx.contentHtml();
  const label = classOf(html, /<label class="([^"]*)">\s*<span>Wave size<\/span>/);
  for (const c of ['text-zinc-400', 'font-medium']) assert.ok(label.includes(c), `label lacks ${c}`);
  assert.ok(!label.includes('text-zinc-300'));
  const input = classOf(html, /id="wave-size-input"[^>]* class="([^"]*)"/);
  for (const c of ['font-mono', 'text-zinc-200', 'w-16', 'h-8', 'px-2.5', 'rounded-lg']) assert.ok(input.includes(c), `input lacks ${c}`);
});

test('the compute button is the frame size and the from/to arrow reads at AA contrast', async () => {
  const ctx = freshContext();
  await flushAsync();
  const html = ctx.contentHtml();
  const btn = classOf(html, /id="wave-compute-btn" type="button" [^>]*?class="([^"]*)"/);
  for (const c of ['h-8', 'px-3', 'rounded-lg', 'font-semibold', 'bg-emerald-600', 'text-black']) assert.ok(btn.includes(c), `compute lacks ${c}`);
  for (const c of ['h-9', 'px-4']) assert.ok(!btn.includes(c), `compute keeps ${c}`);
  const arrow = classOf(html, /<span class="([^"]*)" aria-hidden="true">→<\/span>/);
  assert.ok(arrow.includes('text-zinc-400') && !arrow.includes('text-zinc-500'), 'text-zinc-500 measured 4.0:1');
});

test('only an empty first wave opens its held list by default; an empty later wave stays collapsed', async () => {
  const ctx = freshContext(() => ({
    waves: [{ entries: [], held: ['A: waits on B'] }],
  }));
  await flushAsync();
  assert.match(ctx.contentHtml(), /aria-expanded="true"/);

  const later = freshContext(() => ({
    waves: [oneEntryWave().waves[0], { entries: [], held: ['A: waits on B'] }],
  }));
  await flushAsync();
  const html = later.contentHtml();
  assert.match(html, /Nothing claimable\./);
  assert.doesNotMatch(html, /aria-expanded="true"/);
});

test("the page's Tailwind config makes font-mono JetBrains Mono, the frames' mono face", () => {
  const configSrc = fs.readFileSync(path.join(here, '../../tailwind.config.js'), 'utf8');
  const context = vm.createContext({ module: { exports: {} } });
  vm.runInContext(configSrc, context);
  const mono = context.module.exports.theme.extend.fontFamily?.mono;
  assert.ok(Array.isArray(mono) && /JetBrains Mono/.test(mono[0]), `font-mono resolves to ${JSON.stringify(mono)}`);
});

test('the Archive toggle reaches /api/waves as archived=only, and its absence asks for the default', async () => {
  const off = freshContext();
  await flushAsync();
  assert.equal(new URLSearchParams(wavesCalls(off)[0].split('?')[1]).get('archived'), null);

  const on = freshContext(undefined, { archived: true });
  await flushAsync();
  assert.equal(new URLSearchParams(wavesCalls(on)[0].split('?')[1]).get('archived'), 'only');

  on.filters.archived = false;
  on.resetWaves();
  await flushAsync();
  assert.equal(new URLSearchParams(wavesCalls(on).at(-1).split('?')[1]).get('archived'), null);
});
