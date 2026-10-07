import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const REPO_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '../..');

// The page the server sends, so the theme table, its order and every script are what ships.
function pageScripts() {
  const html = execFileSync(
    'uv',
    ['run', '--project', REPO_ROOT, '--quiet', 'python', '-c', 'from taskmanager.web.ui import get_web_html; print(get_web_html())'],
    { encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] },
  );
  return [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
}

function makeElement(id) {
  const el = {
    id, className: '', style: {}, dataset: {}, innerHTML: '', textContent: '', value: '',
    classList: { add() {}, remove() {}, toggle: () => false, contains: () => false },
    addEventListener() {}, removeEventListener() {}, appendChild() {}, removeChild() {}, remove() {},
    querySelector: () => makeElement(), querySelectorAll: () => [],
    getBoundingClientRect: () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }),
    setAttribute() {}, getAttribute: () => null, focus() {}, click() {}, blur() {},
    closest: () => null, contains: () => false,
  };
  return el;
}

const SCRIPTS = pageScripts();

function loadPage({ readyState = 'complete' } = {}) {
  const elementsById = new Map();
  const docListeners = {};
  const frames = [];
  const sandbox = {
    console,
    window: null,
    document: {
      getElementById(id) {
        if (!elementsById.has(id)) elementsById.set(id, makeElement(id));
        return elementsById.get(id);
      },
      createElement: () => makeElement(),
      readyState,
      addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); },
      querySelector: () => null,
      querySelectorAll: () => [],
      body: makeElement('body'),
    },
    requestAnimationFrame: (fn) => { frames.push(fn); },
    setTimeout, clearTimeout, setInterval, clearInterval,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    location: { hash: '', protocol: 'http:', host: 'test' },
    URLSearchParams,
    // Never settles: these tests read renderers, not anything the page fetches.
    fetch: () => new Promise(() => {}),
    WebSocket: class { send() {} close() {} },
    history: { replaceState() {} },
    addEventListener() {},
    tailwind: {},
    innerWidth: 1440, innerHeight: 900,
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  SCRIPTS.forEach((src, i) => vm.runInContext(src, context, { filename: `inline-script-${i}.js` }));
  return { page: context, docListeners, frames };
}

const { page } = loadPage();

test('a plan whose one unit is LANDED is not done', () => {
  const p = page.progressParts({ LANDED: 1 });
  assert.equal(p.completed, 0);
  assert.equal(p.total, 1);
});

test('landed counts follow completed and stay out of the done count', () => {
  const counts = { IMPLEMENTING: 2, COMPLETED: 5, LANDED: 3, WAITING_REVIEW: 2 };
  const p = page.progressParts(counts);
  assert.equal(`${p.completed}/${p.total}`, '5/12');
  assert.equal(page.progressText(counts), '2 implementing · 5 completed · 3 landed · 2 waiting review');
  const segments = [...page.progressBar(counts).matchAll(/st-seg st-(\w+)" style="width:([\d.]+)%/g)]
    .map(([, code, width]) => [code, Number(width)]);
  assert.deepEqual(segments.map(([code]) => code), ['IMPLEMENTING', 'COMPLETED', 'LANDED', 'WAITING_REVIEW']);
  assert.equal(segments[2][1], 25);
});

test('a plan header keeps its id whole and lets its bar yield the width', () => {
  const html = page.renderPlanCard({ id: 'LAND-CORE', kind: 'plan', title: 'A plan', display: 'LANDED' }, new Map());
  assert.match(html, /<div class="flex items-center gap-2\.5 flex-shrink-0 pr-3">[^]*?>LAND-CORE<\/span>/);
  assert.match(html, /<div class="w-40 min-w-0"><div class="flex w-full /);
});

test('every view names a LANDED node through its theme', () => {
  const chip = page.statusChip('LANDED');
  assert.match(chip, /class="st-chip st-LANDED /);
  assert.match(chip, /<span>Landed<\/span>/);
  assert.match(chip, /title="Landed, review owed: /);
  assert.match(page.statusDot('LANDED'), /st-dot st-LANDED .*title="Landed"/);
  assert.match(page.waveStatusChip('LANDED'), /st-chip st-LANDED .*>Landed<\/span>/);
  const graphNode = page.graphVisNode({ id: 'T1', title: 'A task', kind: 'task', display: 'LANDED' });
  assert.match(graphNode.label, /\*Landed\*$/);
  assert.equal(graphNode.color.border, page.STATUS_THEMES.LANDED.graph_border);
});

test('a LANDED node can be reset to, deferred and abandoned from its action bar', () => {
  const bar = page.renderActionBar({ id: 'T1', kind: 'task', status: 'LANDED' }, false);
  assert.match(bar, /ab-defer/);
  assert.match(bar, /ab-abandon/);
  let dialog = null;
  page.openDialog = (opts) => { dialog = opts; };
  page.openResetDialog({ id: 'T1' });
  assert.match(dialog.bodyHtml, /<option value="LANDED">/);
});

test('a page still parsing renders nothing until every script has run', () => {
  const { page: parsing, docListeners, frames } = loadPage({ readyState: 'loading' });
  assert.equal(frames.length, 0);
  let rendered = 0;
  parsing.renderAll = () => { rendered += 1; };
  docListeners.DOMContentLoaded.forEach((fn) => fn({}));
  frames.splice(0).forEach((fn) => fn(0));
  assert.equal(rendered, 1);
});
