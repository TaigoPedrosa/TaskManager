// A small DOM for running the page's own scripts under node:vm: an HTML parser, the element
// API those scripts call, a selector engine for the selectors they use, and events that bubble.
// Layout is not modelled: getBoundingClientRect reports zeros and nothing scrolls on its own.
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr']);
const RAW_TEXT = new Set(['script', 'style', 'textarea', 'title']);
const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', hellip: '…', middot: '·', mdash: '—', ndash: '–', rsaquo: '›', times: '×' };

function decode(text) {
  return text.replace(/&(#x[0-9a-f]+|#\d+|\w+);/gi, (m, e) => {
    if (e[0] === '#') return String.fromCodePoint(e[1].toLowerCase() === 'x' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10));
    return ENTITIES[e] ?? m;
  });
}

function escapeText(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escapeAttr(text) {
  return text.replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}

class Event {
  constructor(type, init = {}) {
    Object.assign(this, init);
    this.type = type;
    this.bubbles = init.bubbles ?? true;
    this.defaultPrevented = false;
    this.propagationStopped = false;
  }

  preventDefault() { this.defaultPrevented = true; }

  stopPropagation() { this.propagationStopped = true; }
}

class Node {
  constructor(doc) {
    this.ownerDocument = doc;
    this.parentNode = null;
    this.childNodes = [];
    this.listeners = {};
  }

  get firstChild() { return this.childNodes[0] || null; }

  get lastChild() { return this.childNodes.at(-1) || null; }

  get parentElement() { return this.parentNode instanceof Element ? this.parentNode : null; }

  get nextSibling() {
    const sibs = this.parentNode ? this.parentNode.childNodes : [];
    return sibs[sibs.indexOf(this) + 1] || null;
  }

  appendChild(child) { return this.insertBefore(child, null); }

  append(...nodes) { nodes.forEach((n) => this.appendChild(typeof n === 'string' ? new Text(this.ownerDocument, n) : n)); }

  insertBefore(child, ref) {
    if (child instanceof Fragment) {
      [...child.childNodes].forEach((c) => this.insertBefore(c, ref));
      return child;
    }
    if (child.parentNode) child.parentNode.removeChild(child);
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i < 0) this.childNodes.push(child);
    else this.childNodes.splice(i, 0, child);
    child.parentNode = this;
    return child;
  }

  removeChild(child) {
    const i = this.childNodes.indexOf(child);
    if (i >= 0) this.childNodes.splice(i, 1);
    child.parentNode = null;
    return child;
  }

  remove() { if (this.parentNode) this.parentNode.removeChild(this); }

  replaceWith(...nodes) {
    const parent = this.parentNode;
    if (!parent) return;
    nodes.forEach((n) => parent.insertBefore(typeof n === 'string' ? new Text(this.ownerDocument, n) : n, this));
    parent.removeChild(this);
  }

  contains(other) {
    for (let n = other; n; n = n.parentNode) if (n === this) return true;
    return false;
  }

  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }

  set textContent(value) {
    this.childNodes.forEach((c) => { c.parentNode = null; });
    this.childNodes = [];
    if (value !== '' && value != null) this.appendChild(new Text(this.ownerDocument, String(value)));
  }

  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }

  removeEventListener(type, fn) {
    if (this.listeners[type]) this.listeners[type] = this.listeners[type].filter((f) => f !== fn);
  }

  dispatchEvent(event) {
    if (!event.target) event.target = this;
    for (let n = this; n; n = n.parentNode) {
      event.currentTarget = n;
      [...(n.listeners[event.type] || [])].forEach((fn) => fn.call(n, event));
      if (typeof n[`on${event.type}`] === 'function') n[`on${event.type}`].call(n, event);
      if (event.propagationStopped || !event.bubbles) break;
    }
    return !event.defaultPrevented;
  }
}

class Text extends Node {
  constructor(doc, data) {
    super(doc);
    this.data = data;
  }

  get textContent() { return this.data; }

  set textContent(v) { this.data = String(v); }

  serialize() { return escapeText(this.data); }
}

class Fragment extends Node {}

function camelToData(prop) {
  return `data-${prop.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`;
}

class Element extends Node {
  constructor(doc, tagName, attrs = {}) {
    super(doc);
    this.tagName = tagName.toUpperCase();
    this.localName = tagName.toLowerCase();
    this.attrs = new Map(Object.entries(attrs));
    this.style = {};
    this.scrollTop = 0;
    this.scrollLeft = 0;
    this._value = null;
    this._checked = null;
    const el = this;
    this.classList = {
      add: (...c) => el._setClasses([...new Set([...el._classes(), ...c])]),
      remove: (...c) => el._setClasses(el._classes().filter((x) => !c.includes(x))),
      toggle: (c, force) => {
        const on = force === undefined ? !el._classes().includes(c) : Boolean(force);
        if (on) el.classList.add(c); else el.classList.remove(c);
        return on;
      },
      contains: (c) => el._classes().includes(c),
    };
    this.dataset = new Proxy({}, {
      get: (_, prop) => (typeof prop === 'string' ? el.getAttribute(camelToData(prop)) ?? undefined : undefined),
      set: (_, prop, value) => { el.setAttribute(camelToData(prop), value); return true; },
    });
  }

  _classes() { return (this.getAttribute('class') || '').split(/\s+/).filter(Boolean); }

  _setClasses(list) { this.setAttribute('class', list.join(' ')); }

  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }

  setAttribute(name, value) { this.attrs.set(name, String(value)); }

  removeAttribute(name) { this.attrs.delete(name); }

  hasAttribute(name) { return this.attrs.has(name); }

  toggleAttribute(name, force) {
    const on = force === undefined ? !this.hasAttribute(name) : force;
    if (on) this.setAttribute(name, ''); else this.removeAttribute(name);
    return on;
  }

  get id() { return this.getAttribute('id') || ''; }

  set id(v) { this.setAttribute('id', v); }

  get className() { return this.getAttribute('class') || ''; }

  set className(v) { this.setAttribute('class', v); }

  get children() { return this.childNodes.filter((c) => c instanceof Element); }

  get type() { return this.getAttribute('type') || (this.localName === 'button' ? 'submit' : 'text'); }

  set type(v) { this.setAttribute('type', v); }

  get disabled() { return this.hasAttribute('disabled'); }

  set disabled(v) { this.toggleAttribute('disabled', Boolean(v)); }

  get hidden() { return this.hasAttribute('hidden'); }

  set hidden(v) { this.toggleAttribute('hidden', Boolean(v)); }

  get open() { return this.hasAttribute('open'); }

  set open(v) { this.toggleAttribute('open', Boolean(v)); }

  get checked() { return this._checked ?? this.hasAttribute('checked'); }

  set checked(v) { this._checked = Boolean(v); }

  get value() {
    if (this._value !== null) return this._value;
    if (this.localName === 'textarea') return this.textContent;
    return this.getAttribute('value') ?? '';
  }

  set value(v) { this._value = String(v); }

  get tabIndex() { return Number(this.getAttribute('tabindex') ?? (/^(button|input|select|textarea|a)$/.test(this.localName) ? 0 : -1)); }

  get innerHTML() { return this.childNodes.map(serialize).join(''); }

  set innerHTML(html) {
    this.textContent = '';
    // Replacing an element's content drops its scroll offset, as a browser does when the new
    // content is shorter than the old offset.
    this.scrollTop = 0;
    parseInto(this, String(html));
    this.ownerDocument.onMutate(this);
  }

  get outerHTML() { return serialize(this); }

  insertAdjacentHTML(position, html) {
    const frag = new Fragment(this.ownerDocument);
    parseInto(frag, html);
    if (position === 'beforeend') this.appendChild(frag);
    else if (position === 'afterbegin') this.insertBefore(frag, this.firstChild);
    else if (position === 'beforebegin') this.parentNode.insertBefore(frag, this);
    else if (position === 'afterend') this.parentNode.insertBefore(frag, this.nextSibling);
    this.ownerDocument.onMutate(this);
  }

  querySelectorAll(selector) {
    const groups = parseSelector(selector);
    const out = [];
    walk(this, (el) => { if (groups.some((g) => matchesComplex(el, g, this))) out.push(el); });
    return out;
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  matches(selector) { return parseSelector(selector).some((g) => matchesComplex(this, g, null)); }

  closest(selector) {
    for (let n = this; n instanceof Element; n = n.parentNode) if (n.matches(selector)) return n;
    return null;
  }

  getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0 }; }

  getClientRects() { return []; }

  scrollIntoView() {}

  focus() { this.ownerDocument.activeElement = this; }

  blur() { if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body; }

  click() {
    if (this.disabled) return;
    const event = new Event('click');
    this.dispatchEvent(event);
    if (event.defaultPrevented) return;
    if (this.localName === 'button' && this.type === 'submit') {
      const form = this.closest('form');
      if (form) form.dispatchEvent(new Event('submit'));
    }
    if (this.localName === 'summary' && this.parentNode && this.parentNode.localName === 'details') {
      this.parentNode.open = !this.parentNode.open;
      this.parentNode.dispatchEvent(new Event('toggle', { bubbles: false }));
    }
  }
}

function walk(root, fn) {
  root.childNodes.forEach((c) => {
    if (c instanceof Element) {
      fn(c);
      walk(c, fn);
    }
  });
}

function serialize(node) {
  if (!(node instanceof Element)) return node instanceof Text ? node.serialize() : '';
  const attrs = [...node.attrs].map(([k, v]) => ` ${k}="${escapeAttr(v)}"`).join('');
  if (VOID.has(node.localName)) return `<${node.localName}${attrs}>`;
  return `<${node.localName}${attrs}>${node.childNodes.map(serialize).join('')}</${node.localName}>`;
}

const TOKEN = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s=>/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>/gi;
const ATTR = /([^\s=>/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;

function parseInto(root, html) {
  const doc = root.ownerDocument;
  const stack = [root];
  let last = 0;
  TOKEN.lastIndex = 0;
  let m;
  const text = (s) => { if (s) stack.at(-1).appendChild(new Text(doc, decode(s))); };
  while ((m = TOKEN.exec(html))) {
    text(html.slice(last, m.index));
    last = TOKEN.lastIndex;
    if (m[1]) {
      const name = m[1].toLowerCase();
      const i = stack.findLastIndex((n) => n.localName === name);
      if (i > 0) stack.length = i;
    } else if (m[2]) {
      const name = m[2].toLowerCase();
      const attrs = {};
      for (const a of (m[3] || '').matchAll(ATTR)) attrs[a[1].toLowerCase()] = decode(a[2] ?? a[3] ?? a[4] ?? '');
      const el = new Element(doc, name, attrs);
      stack.at(-1).appendChild(el);
      if (RAW_TEXT.has(name)) {
        const close = html.toLowerCase().indexOf(`</${name}`, last);
        const end = close < 0 ? html.length : close;
        const raw = html.slice(last, end);
        if (raw) el.appendChild(new Text(doc, name === 'textarea' || name === 'title' ? decode(raw) : raw));
        const gt = html.indexOf('>', end);
        last = gt < 0 ? html.length : gt + 1;
        TOKEN.lastIndex = last;
      } else if (!VOID.has(name) && !m[4]) {
        stack.push(el);
      }
    }
  }
  text(html.slice(last));
}

// Selector grammar: comma-separated groups of compound selectors joined by descendant or
// child combinators; a compound is tag, #id, .class, [attr], [attr="v"] and :not(compound).
function parseSelector(selector) {
  return splitTop(selector, ',').map((group) => {
    const parts = [];
    let combinator = ' ';
    for (const tok of group.trim().replace(/\s*>\s*/g, ' > ').split(/\s+(?![^[]*\])(?![^(]*\))/)) {
      if (tok === '>') { combinator = '>'; continue; }
      parts.push({ combinator, simple: parseCompound(tok) });
      combinator = ' ';
    }
    return parts;
  });
}

function splitTop(s, sep) {
  const out = [];
  let depth = 0;
  let cur = '';
  for (const ch of s) {
    if (ch === '(' || ch === '[') depth++;
    if (ch === ')' || ch === ']') depth--;
    if (ch === sep && depth === 0) { out.push(cur); cur = ''; } else cur += ch;
  }
  out.push(cur);
  return out;
}

function parseCompound(tok) {
  const tests = [];
  const re = /^(\*|[a-zA-Z][\w-]*)|#([\w-]+)|\.([\w\-\\/:[\]]+?)(?=[.#[:]|$)|\[([\w-]+)(?:([\^$*]?=)"?([^"\]]*)"?)?\]|:not\(([^)]*)\)/g;
  let m;
  while ((m = re.exec(tok)) && m[0]) {
    if (m[1]) { const t = m[1].toLowerCase(); if (t !== '*') tests.push((el) => el.localName === t); }
    else if (m[2]) { const id = m[2]; tests.push((el) => el.id === id); }
    else if (m[3]) { const c = m[3].replace(/\\/g, ''); tests.push((el) => el._classes().includes(c)); }
    else if (m[4]) {
      const [name, op, val] = [m[4], m[5], m[6]];
      tests.push((el) => {
        const v = el.getAttribute(name);
        if (v === null) return false;
        if (!op) return true;
        if (op === '=') return v === val;
        if (op === '^=') return v.startsWith(val);
        if (op === '$=') return v.endsWith(val);
        return v.includes(val);
      });
    } else if (m[7] !== undefined) {
      const inner = parseCompound(m[7]);
      tests.push((el) => !inner(el));
    }
  }
  return (el) => tests.every((t) => t(el));
}

function matchesComplex(el, parts, scope) {
  const match = (node, i) => {
    if (!parts[i].simple(node)) return false;
    if (i === 0) return true;
    const combinator = parts[i].combinator;
    for (let p = node.parentNode; p instanceof Element && p !== scope; p = p.parentNode) {
      if (match(p, i - 1)) return true;
      if (combinator === '>') return false;
    }
    return false;
  };
  return match(el, parts.length - 1);
}

class Document extends Node {
  constructor() {
    super(null);
    this.ownerDocument = this;
    this.mutations = [];
    this.documentElement = new Element(this, 'html');
    this.appendChild(this.documentElement);
    this.head = new Element(this, 'head');
    this.body = new Element(this, 'body');
    this.documentElement.append(this.head, this.body);
    this.activeElement = this.body;
  }

  onMutate(el) { this.mutations.push(el); }

  createElement(tag) { return new Element(this, tag); }

  createTextNode(text) { return new Text(this, text); }

  createDocumentFragment() { return new Fragment(this); }

  getElementById(id) {
    let found = null;
    walk(this, (el) => { if (!found && el.id === id) found = el; });
    return found;
  }

  querySelectorAll(selector) { return this.documentElement.querySelectorAll(selector); }

  querySelector(selector) { return this.documentElement.querySelector(selector); }
}

class DOMParser {
  parseFromString(html) {
    const doc = new Document();
    parseInto(doc.body, html);
    return doc;
  }
}

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.join(here, '../..');
let pageHtml = null;

// The page exactly as `tm web run` serves it: index.html with every slot filled, so the scripts
// run in their shipped order against the markup they ship with.
export function servedPage() {
  pageHtml ??= execFileSync('uv', ['run', '--project', REPO_ROOT, '--quiet', 'python', '-c',
    'from taskmanager.web.ui import get_web_html; print(get_web_html(), end="")'], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  return pageHtml;
}

export class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    FakeSocket.last = this;
  }

  send(data) { this.sent.push(JSON.parse(data)); }

  open() { this.readyState = 1; return this.onopen && this.onopen({}); }

  message(obj) { return this.onmessage && this.onmessage({ data: JSON.stringify(obj) }); }

  close() { this.readyState = 3; return this.onclose && this.onclose({}); }
}

export function jsonResponse(status, body) {
  const text = body === undefined ? '' : JSON.stringify(body);
  return { ok: status >= 200 && status < 300, status, text: async () => text, json: async () => JSON.parse(text) };
}

function defaultResponse(url) {
  if (url.startsWith('/api/meta')) return jsonResponse(200, { dispatch: { wave_size: 5, tick_budget: 20 } });
  if (url.startsWith('/api/waves')) return jsonResponse(200, { waves: [{ entries: [], held: [] }] });
  if (url.startsWith('/api/decisions')) return jsonResponse(200, { items: [], next: null, counts: { open: 0, answered: 0, withdrawn: 0 } });
  return jsonResponse(404, { detail: 'Not Found' });
}

// The address bar and session history: pushState and replaceState move `location`, and back()
// and forward() move it and fire popstate on the window, as a browser does.
function sessionHistory(location, fire) {
  const entries = [location.href];
  let index = 0;
  const show = (href) => {
    const url = new URL(href, location.href);
    Object.assign(location, { href: url.href, pathname: url.pathname, search: url.search, hash: url.hash });
  };
  show(location.href);
  const go = (delta) => {
    if (!entries[index + delta]) return;
    index += delta;
    show(entries[index]);
    fire('popstate');
  };
  return {
    entries,
    get length() { return entries.length; },
    pushState(state, title, href) {
      show(href);
      entries.splice(index + 1, entries.length, location.href);
      index += 1;
    },
    replaceState(state, title, href) {
      show(href);
      entries[index] = location.href;
    },
    back: () => go(-1),
    forward: () => go(1),
  };
}

// Loads the served page (or `html`, an exported one) into a fresh Document at `url` and runs
// its inline scripts in one vm context. `fetch` answers every request the page makes;
// `beforeScripts` can seed globals (a static export's window.STATIC_DATA) before the first
// script runs.
export function loadPage({ fetch, beforeScripts, url = '/', html = servedPage() } = {}) {
  const doc = new Document();
  const bodyHtml = html.slice(html.indexOf('>', html.indexOf('<body')) + 1, html.lastIndexOf('</body>'));
  parseInto(doc.body, bodyHtml.replace(/<script>[\s\S]*?<\/script>/g, ''));
  const headScripts = [...html.slice(0, html.indexOf('<body')).matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  const bodyScripts = [...bodyHtml.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  const rafQueue = [];
  const fetchCalls = [];
  const windowListeners = {};
  const location = { protocol: 'http:', host: 'test', href: new URL(url, 'http://test/').href };
  const sandbox = {
    console,
    document: doc,
    Event,
    DOMParser,
    WebSocket: FakeSocket,
    location,
    history: sessionHistory(location, (type) => sandbox.dispatchEvent(new Event(type))),
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    navigator: { clipboard: { writeText: async () => {} } },
    requestAnimationFrame: (fn) => rafQueue.push(fn),
    setTimeout, clearTimeout, setInterval, clearInterval,
    URLSearchParams, TextEncoder, crypto: globalThis.crypto,
    CSS: { escape: (s) => String(s) },
    innerWidth: 1440, innerHeight: 900,
    addEventListener(type, fn) { (windowListeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) {
      if (windowListeners[type]) windowListeners[type] = windowListeners[type].filter((f) => f !== fn);
    },
    dispatchEvent(event) {
      [...(windowListeners[event.type] || [])].forEach((fn) => fn(event));
      return !event.defaultPrevented;
    },
    tailwind: {},
    fetch: async (url, opts) => {
      fetchCalls.push({ url, method: (opts && opts.method) || 'GET', body: opts && opts.body });
      const answered = fetch && (await fetch(url, opts));
      return answered || defaultResponse(url);
    },
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  if (beforeScripts) beforeScripts(sandbox);
  headScripts.forEach((src, i) => vm.runInContext(src, context, { filename: `head-${i}.js` }));
  bodyScripts.forEach((src, i) => vm.runInContext(src, context, { filename: `body-${i}.js` }));
  return {
    window: sandbox,
    document: doc,
    fetchCalls,
    socket: FakeSocket.last,
    $: (sel) => doc.querySelector(sel),
    $$: (sel) => doc.querySelectorAll(sel),
    run: (code) => vm.runInContext(code, context),
    flushFrames() {
      while (rafQueue.length) rafQueue.shift()();
    },
    async settle() {
      for (let i = 0; i < 10; i++) {
        await new Promise((r) => setTimeout(r, 0));
        while (rafQueue.length) rafQueue.shift()();
      }
    },
  };
}

export function key(el, keyName) {
  const event = new Event('keydown', { key: keyName });
  el.dispatchEvent(event);
  return event;
}

export function type(el, text) {
  el.value = text;
  el.dispatchEvent(new Event('input'));
}
