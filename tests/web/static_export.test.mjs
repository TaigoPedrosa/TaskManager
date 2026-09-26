import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import os from 'node:os';
import { execFileSync } from 'node:child_process';

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.join(here, '../..');

// The page under test is the one `tm web export` writes, over a freshly initialized project,
// so the inline scripts, their order and the injected globals are exactly what ships. A grep
// of the sources passes on a page whose scripts throw before ever reaching the checked call.
function exportedPageScripts() {
  const project = fs.mkdtempSync(path.join(os.tmpdir(), 'tm-static-export-'));
  try {
    const tm = (...args) => execFileSync('uv', ['run', '--project', REPO_ROOT, '--quiet', 'tm', ...args], {
      stdio: ['ignore', 'ignore', 'inherit'],
    });
    execFileSync('git', ['-C', project, 'init', '-q']);
    tm('init', '-C', project);
    const out = path.join(project, 'export.html');
    tm('web', 'export', '-C', project, '-o', out);
    const html = fs.readFileSync(out, 'utf8');
    return [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  } finally {
    fs.rmSync(project, { recursive: true, force: true });
  }
}

function makeElement(id) {
  const classes = new Set();
  const el = {
    id, className: '', style: {}, dataset: {}, innerHTML: '', textContent: '', value: '',
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
    addEventListener() {}, removeEventListener() {},
    appendChild() {}, removeChild() {}, remove() { el.removed = true; },
    querySelector: () => makeElement(), querySelectorAll: () => [],
    getBoundingClientRect: () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }),
    setAttribute() {}, getAttribute: () => null, focus() {}, click() {}, blur() {},
    closest: () => null, contains: () => false,
  };
  return el;
}

// A page's own DOM: every element the scripts' top-level code touches is looked up by id and
// memoized, so the script's `const foo = document.getElementById('foo')` and this test's own
// lookup of the same id share one object -- the same relationship a real DOM gives a page.
function runStaticPage() {
  const elementsById = new Map();
  const sandbox = {
    console,
    window: null,
    document: {
      getElementById(id) {
        if (!elementsById.has(id)) elementsById.set(id, makeElement(id));
        return elementsById.get(id);
      },
      createElement: () => makeElement(),
      addEventListener() {},
      querySelector: () => null,
      querySelectorAll: () => [],
      body: makeElement('body'),
    },
    // Fired synchronously rather than deferred to a real frame, so scheduleRender()'s
    // coalesced renderAll() actually runs in this test instead of silently never firing.
    requestAnimationFrame: (fn) => fn(),
    setTimeout, clearTimeout, setInterval, clearInterval,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    location: { hash: '' },
    URLSearchParams,
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
    WebSocket: class { constructor() {} send() {} close() {} },
    history: { replaceState() {} },
    addEventListener() {},
    tailwind: {},
    innerWidth: 1440, innerHeight: 900,
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  exportedPageScripts().forEach((src, i) => vm.runInContext(src, context, { filename: `inline-script-${i}.js` }));
  return elementsById;
}

test('a static export runs every inline script without throwing, opens on Document, and offers no Waves view', () => {
  const elementsById = runStaticPage();
  assert.equal(elementsById.get('document-pane').classList.contains('hidden'), false);
  assert.equal(elementsById.get('waves-pane').classList.contains('hidden'), true);
  assert.equal(elementsById.get('network-canvas').classList.contains('hidden'), true);
  assert.equal(elementsById.get('view-waves-btn').removed, true, 'the Waves toggle is gone');
  assert.equal(elementsById.get('waves-content')?.innerHTML ?? '', '', 'no Waves message is rendered');
  assert.match(
    elementsById.get('unified-document')?.innerHTML ?? '',
    /No specs, plans or tasks match/,
    'the Document view rendered from the embedded data'
  );
});
