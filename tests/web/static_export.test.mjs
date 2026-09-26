import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const JS_DIR = path.join(here, '../../src/taskmanager/web/static/js');

// Same fixed order taskmanager/web/ui.py's _JS_FILES loads them in (plain sequential <script>
// tags, so a file may reference at top level what an earlier one declared). A grep-based pin
// only checks that a call string is present in the source; it still passes on a page whose
// scripts throw before ever reaching that call. This test instead runs the concatenation the
// way a browser (or the static export served over http) would, and would have caught the
// static-export page throwing "Cannot access 'VIEW_BTN_ACTIVE' before initialization" and
// rendering nothing.
const JS_FILES = [
  'store.js', 'core.js', 'filters.js', 'tree.js', 'graph.js',
  'edit.js', 'detail.js', 'waves.js', 'decisions.js', 'main.js',
];
const PAGE_SRC = JS_FILES.map((f) => fs.readFileSync(path.join(JS_DIR, f), 'utf8')).join('\n;\n');

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
    appendChild() {}, removeChild() {}, remove() {},
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
    requestAnimationFrame: () => {},
    setTimeout, clearTimeout, setInterval, clearInterval,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    location: { hash: '' },
    URLSearchParams,
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
    WebSocket: class { constructor() {} send() {} close() {} },
    history: { replaceState() {} },
    addEventListener() {},
    STATIC_DATA: { rows: {}, edges: [], statuses: [], bodies: {} },
    VIEW_MODES: { GRAPH: 'graph', WAVES: 'waves' },
    STATUS_THEMES: {}, STATUS_GROUPS: [], PHASE_THEMES: {},
    innerWidth: 1440, innerHeight: 900,
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  vm.runInContext(PAGE_SRC, context, { filename: 'static-export.js' });
  return elementsById;
}

test('a static export runs every inline script without throwing, and opens on Graph', () => {
  const elementsById = runStaticPage();
  assert.equal(elementsById.get('waves-pane').classList.contains('hidden'), true);
  assert.equal(elementsById.get('network-canvas').classList.contains('hidden'), false);
});
