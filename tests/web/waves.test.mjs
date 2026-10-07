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
function freshContext(wavesPayload, { isStaticMode = false, shown = true } = {}) {
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

  // Action chip: lowercase action word, no icon.
  assert.match(entryHead, /<span class="st-chip st-X inline-flex items-center px-1\.5 py-0\.5 rounded-full font-medium text-\[10px\] leading-tight"[^>]*>implement<\/span>/, 'the action chip is the lowercase action word with no icon');

  // From/to chips: the raw status word, not the display status, and no icon.
  assert.match(entryBody, /<span class="st-chip st-X inline-flex items-center px-1\.5 py-0\.5 rounded-full font-medium text-\[10px\] leading-tight">Ready<\/span>/, 'the "from" chip reads the raw status, not a display status');
  assert.match(entryBody, /<span class="st-chip st-X inline-flex items-center px-1\.5 py-0\.5 rounded-full font-medium text-\[10px\] leading-tight">Implemented<\/span>/, 'the "to" chip reads the raw status, not a display status');

  // Repo pill colour matches the frame's cyan, not zinc.
  assert.match(entryHead, /class="[^"]*text-cyan-400[^"]*">api</, 'the repo pill reads cyan');

  // Model+repo pills sit in the header at sm and up, and in the body (under the title) below it.
  assert.match(entryHead, /<div class="hidden sm:flex items-center gap-1\.5 flex-wrap">/, 'desktop keeps the model+repo row in the header band');
  assert.match(entryBody, /<div class="flex sm:hidden items-center gap-1\.5 flex-wrap">/, '375 moves the model+repo row into the body, under the title');

  assert.match(html, /<span>Held \(1\)<\/span><svg data-icon="chevron-right">/, 'the held chevron sits after the label');
  assert.ok(classOf(html, /class="(wave-held-toggle [^"]*)"/).includes('uppercase'));
  const rows = classOf(html, /class="(wave-held-rows [^"]*)"/);
  for (const c of ['border', 'rounded-lg', 'font-mono']) assert.ok(rows.includes(c), `held rows lack ${c}`);

  // Held row at 375: id stacks over the reason, and the reason is not truncated there.
  const heldRow = classOf(html, /class="(flex flex-col[^"]*p-2 bg-zinc-950\/60)"/);
  for (const c of ['flex-col', 'sm:flex-row', 'sm:items-center', 'sm:gap-3']) {
    assert.ok(heldRow.includes(c), `held row lacks ${c}`);
  }
  const reasonCls = classOf(html, /class="([^"]*)">waits on S-P-T/);
  assert.ok(reasonCls.includes('sm:truncate'), 'the reason truncates from sm up');
  assert.ok(!reasonCls.includes('truncate'), 'the reason is not truncated below sm, so the full text survives at 375');

  assert.ok(classOf(html, /class="(wave-controls [^"]*)"/).includes('justify-between'));
  assert.match(html, /id="wave-size-caption"[^>]*>[^<]*<\/span>\s*<\/div>\s*<button id="wave-reset-btn"/, 'Reset sits apart from the caption, at the row end');
  assert.ok(classOf(html, /id="wave-size-caption" class="([^"]*)"/).includes('text-zinc-400'), 'caption meets AA contrast on zinc-950');
});

test('an out-of-range wave size refuses locally, without ever calling /api/waves', async () => {
  const ctx = freshContext();
  await flushAsync();
  const before = wavesCalls(ctx).length;

  ctx.onWaveSizeChange(999); // waveMaxSize is 20 (metaResponse's tick_budget)
  await flushAsync();

  assert.equal(wavesCalls(ctx).length, before, 'no /api/waves request was ever made for the bad size');
  const html = ctx.contentHtml();
  assert.match(html, /Could not compute waves: wave size must be 1–20 \(this project's dispatch\.tick_budget\)\./);
  assert.match(html, /role="alert"/);
});

test('the loading state renders inside a Wave 1 card, with a disabled compute button', () => {
  const ctx = freshContext();
  const html = ctx.contentHtml();
  assert.match(html, /<h3 class="[^"]*">Wave 1<\/h3>/);
  assert.match(html, /Loading&hellip;/);
  assert.match(html, /id="wave-compute-btn"[^>]* disabled[^>]*>/, 'compute is disabled while the first request is in flight');
});

test('the empty wave body reads at AA contrast, not italic', async () => {
  const ctx = freshContext(() => ({ waves: [{ entries: [], held: [] }] }));
  await flushAsync();
  const html = ctx.contentHtml();
  const cls = classOf(html, /class="([^"]*)">Nothing claimable\./);
  assert.ok(cls.includes('text-zinc-400'), 'meets AA contrast (text-zinc-500 measured 4.0:1)');
  assert.ok(cls.includes('text-sm'), 'matches the frame\'s size');
  assert.ok(!cls.includes('italic'), 'the frame draws this non-italic');
});

test('the error state carries no retry button, matching the error frame', async () => {
  const ctx = freshContext();
  await flushAsync();
  ctx.fetch = async (url) => {
    if (url.startsWith('/api/meta')) return metaResponse();
    return { ok: false, status: 400, json: async () => ({ detail: 'boom' }) };
  };
  await ctx.fetchWaves();
  const html = ctx.contentHtml();
  assert.doesNotMatch(html, /wave-retry-btn/);
  assert.doesNotMatch(html, />Retry</);
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
    assert.doesNotMatch(ctx.contentHtml(), /role="alert"/);
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

test('the loading line reads at AA contrast on the card', () => {
  const ctx = freshContext();
  const cls = classOf(ctx.contentHtml(), /class="([^"]*)">Loading&hellip;/);
  assert.ok(cls.includes('text-zinc-400'), 'text-zinc-500 measured 4.0:1');
  assert.ok(!cls.includes('text-zinc-500'));
});

test('the error alert is the frame box: solid red-950 fill, red-700 border, 12px padding, medium text', async () => {
  const ctx = freshContext();
  await flushAsync();
  await typeWaveSize(ctx, '50');
  const cls = classOf(ctx.contentHtml(), /role="alert" class="([^"]*)"/);
  for (const c of ['bg-red-950', 'border-red-700', 'p-3', 'font-medium', 'text-red-200', 'rounded-lg']) {
    assert.ok(cls.includes(c), `alert lacks ${c}`);
  }
  for (const c of ['bg-red-950/40', 'border-red-800/60', 'p-4']) assert.ok(!cls.includes(c), `alert keeps ${c}`);
  const input = classOf(ctx.contentHtml(), /id="wave-size-input"[^>]* class="([^"]*)"/);
  assert.ok(input.includes('border-red-700'), 'the refused input wears the red border');
  assert.ok(input.includes('text-zinc-200') && !input.includes('text-red-200'), 'its value keeps the frame\'s zinc-200');
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
  const arrow = classOf(html, /<span class="([^"]*)">→<\/span>/);
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
  assert.match(html, /class="wave-held-rows [^"]* hidden"/);
});

test('at 375 the wave body has a 12px side gutter and the empty-wave note sits under the button', async () => {
  const ctx = freshContext(() => ({ waves: [{ entries: [], held: [] }] }));
  await flushAsync();
  const html = ctx.contentHtml();
  const body = classOf(html, /class="(wave-body [^"]*)"/);
  for (const c of ['px-3', 'py-4', 'sm:px-4']) assert.ok(body.includes(c), `wave body lacks ${c}`);
  assert.ok(!body.includes('p-4'), 'no unconditional 16px side padding');
  const footer = classOf(html, /class="(wave-footer [^"]*)"/);
  for (const c of ['flex-col', 'gap-2', 'sm:flex-row', 'sm:items-center', 'sm:gap-3']) assert.ok(footer.includes(c), `footer lacks ${c}`);
  assert.match(html, /The last wave is empty\./);
});

test("the page's Tailwind config makes font-mono JetBrains Mono, the frames' mono face", () => {
  const configSrc = fs.readFileSync(path.join(here, '../../tailwind.config.js'), 'utf8');
  const context = vm.createContext({ module: { exports: {} } });
  vm.runInContext(configSrc, context);
  const mono = context.module.exports.theme.extend.fontFamily?.mono;
  assert.ok(Array.isArray(mono) && /JetBrains Mono/.test(mono[0]), `font-mono resolves to ${JSON.stringify(mono)}`);
});

test("chips, pills, titles and held rows take the frames' line heights, not the inherited 1.5", async () => {
  const ctx = freshContext(() => ({
    waves: [{ ...oneEntryWave().waves[0], held: ['S-P-U: waits on S-P-T'] }, { entries: [], held: [] }],
  }));
  await flushAsync();
  const html = ctx.contentHtml();
  const has = (pattern, what, token) => assert.ok(classOf(html, pattern).includes(token), `${what} lacks ${token}`);
  has(/<span class="(st-chip[^"]*)"[^>]*>implement<\/span>/, 'action chip', 'leading-tight');
  has(/<span class="(st-chip[^"]*)">Ready<\/span>/, 'status chip', 'leading-tight');
  has(/<span class="([^"]*)">sonnet<\/span>/, 'model pill', 'leading-tight');
  has(/<div class="([^"]*)">T<\/div>/, 'task title', 'leading-tight');
  has(/class="(wave-held-toggle [^"]*)"/, 'held toggle', 'leading-tight');
  has(/class="(wave-held-rows [^"]*)"/, 'held rows', 'leading-snug');
  has(/class="([^"]*)">Nothing claimable\./, 'empty-wave note', 'leading-tight');
});
