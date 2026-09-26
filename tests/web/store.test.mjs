import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const STORE_SRC = fs.readFileSync(
  path.join(here, '../../src/taskmanager/web/static/js/store.js'),
  'utf8',
);
const STATUSES_FIXTURE = JSON.parse(
  fs.readFileSync(path.join(here, '../fixtures/statuses_hash.json'), 'utf8'),
);
const VISIBILITY_FIXTURE = JSON.parse(
  fs.readFileSync(path.join(here, '../fixtures/visibility_cases.json'), 'utf8'),
);

// A minimal stand-in for the browser's WebSocket: readyState plus onopen/onmessage/onclose,
// the same surface store.js's connect() uses. Each method returns whatever the handler
// returns, so a caller can `await socket.message(...)` through store.js's async onmessage.
class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = FakeSocket.CONNECTING;
    this.sent = [];
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    FakeSocket.instances.push(this);
  }

  send(data) {
    this.sent.push(JSON.parse(data));
  }

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

// store.js runs inside its own vm context, so anything it builds internally (as opposed to
// what FakeSocket itself parses, already in this file's realm) carries that context's own
// Array/Object -- deepStrictEqual treats that as unequal to an identical plain literal from
// here. Round-tripping through this realm's JSON normalizes it before comparing.
function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

// store.js is a classic script, not a module: run it in its own vm context so each test
// starts from a clean slate, with a fake socket and (per Node 22+) the real Web Crypto.
function freshContext() {
  FakeSocket.instances = [];
  const sandbox = {
    WebSocket: FakeSocket,
    crypto: globalThis.crypto,
    TextEncoder: globalThis.TextEncoder,
    console,
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
  };
  const context = vm.createContext(sandbox);
  vm.runInContext(STORE_SRC, context, { filename: 'store.js' });
  return context;
}

test('canonical and the hash of statuses_hash.json match its own golden vector', async () => {
  const ctx = freshContext();
  assert.equal(ctx.canonical(STATUSES_FIXTURE.statuses), STATUSES_FIXTURE.canonical);
  const hash = await ctx.sha256Hex(ctx.canonical(STATUSES_FIXTURE.statuses));
  assert.equal(hash, STATUSES_FIXTURE.hash);
});

test('every visibility_cases.json case passes through the static path', () => {
  const ctx = freshContext();
  for (const c of VISIBILITY_FIXTURE.cases) {
    const store = ctx.createStore({
      staticData: {
        statuses: [],
        hash: '',
        rows: VISIBILITY_FIXTURE.rows,
        edges: VISIBILITY_FIXTURE.edges,
        bodies: {},
        decisions: [],
      },
      filters: c.filters,
      open: c.open,
    });
    assert.deepEqual([...store.rows.keys()], c.visible, c.name);
    assert.deepEqual(plain(store.edges), c.edges, c.name);
    assert.deepEqual(plain(store.facets), c.facets, c.name);
  }
});

test('a reset subscribe followed by a snapshot leaves the store equal to the snapshot, and every update item type applies in order', async () => {
  const ctx = freshContext();
  const store = ctx.createStore({ wsUrl: 'ws://x/ws' });
  const socket = FakeSocket.instances[0];

  socket.open();
  assert.deepEqual(socket.sent, [{ type: 'subscribe', id: 1, filters: {}, open: [], watch: [], reset: true }]);

  const snapshotStatuses = [{ spec: null, plans: [{ plan: null, counts: { READY: 1 } }] }];
  await socket.message({
    type: 'snapshot',
    re: 1,
    hash: await ctx.sha256Hex(ctx.canonical(snapshotStatuses)),
    statuses: snapshotStatuses,
    facets: { status: { READY: 2 } },
    decisions_open: 1,
    rows: [{ id: 'T1', kind: 'task', title: 'One', parent: null, ordinal: 0, display: 'READY', score: 1 }],
    edges: [],
    bodies: { T1: { node: { id: 'T1' }, sections: [{ key: 'report', header: 'Report', content: 'x', ordinal: 0 }] } },
  });

  assert.equal(store.pending, false);
  assert.deepEqual([...store.rows.keys()], ['T1']);
  assert.equal(store.rows.get('T1').title, 'One');
  assert.deepEqual(plain(store.statuses), snapshotStatuses);
  assert.deepEqual(plain(store.facets), { status: { READY: 2 } });
  assert.equal(store.decisionsOpen, 1);
  assert.deepEqual(plain(store.bodies.get('T1').sections.report), { header: 'Report', content: 'x', ordinal: 0 });

  // A row re-added then dropped in the same update leaves it absent: items apply in the
  // order they arrive, not all-adds-then-all-drops.
  const updatedStatuses = [{ spec: null, plans: [{ plan: null, counts: { READY: 2 } }] }];
  await socket.message({
    type: 'update',
    re: null,
    hash: await ctx.sha256Hex(ctx.canonical(updatedStatuses)),
    items: [
      { op: 'row', row: { id: 'T2', kind: 'task', title: 'Two', parent: null, ordinal: 1, display: 'READY', score: 2 } },
      { op: 'row', row: { id: 'T1', kind: 'task', title: 'One (edited)', parent: null, ordinal: 0, display: 'READY', score: 1 } },
      { op: 'drop', id: 'T1' },
      { op: 'statuses', spec: null, entry: { spec: null, plans: [{ plan: null, counts: { READY: 2 } }] } },
      { op: 'edges', add: [['T2', 'T3', 'depends_on']], remove: [] },
      { op: 'facets', facets: { status: { READY: 1 } } },
      { op: 'decisions_open', count: 3 },
      { op: 'section', id: 'T2', key: 'notes', section: { header: 'Notes', content: 'hi', ordinal: 0 } },
      { op: 'body', id: 'T2', part: 'lease', value: { agent_id: 'a1' } },
    ],
  });

  assert.deepEqual([...store.rows.keys()], ['T2']);
  assert.equal(store.rows.has('T1'), false);
  assert.equal(store.bodies.has('T1'), false);
  assert.deepEqual(plain(store.edges), [['T2', 'T3', 'depends_on']]);
  assert.deepEqual(plain(store.facets), { status: { READY: 1 } });
  assert.equal(store.decisionsOpen, 3);
  assert.deepEqual(plain(store.statuses), updatedStatuses);
  assert.deepEqual(plain(store.bodies.get('T2').sections.notes), { header: 'Notes', content: 'hi', ordinal: 0 });
  assert.deepEqual(plain(store.bodies.get('T2').lease), { agent_id: 'a1' });
  // The declared hash matched the resulting statuses, so no drift resubscribe fired.
  assert.equal(socket.sent.length, 1);
});

test('a tampered statuses before an update produces exactly one subscribe with reset: true, and the state is consistent after the new snapshot', async () => {
  const ctx = freshContext();
  const store = ctx.createStore({ wsUrl: 'ws://x/ws' });
  const socket = FakeSocket.instances[0];
  socket.open();
  await socket.message({
    type: 'snapshot', re: 1, hash: await ctx.sha256Hex(ctx.canonical([])),
    statuses: [], facets: {}, decisions_open: 0, rows: [], edges: [], bodies: {},
  });
  assert.equal(socket.sent.length, 1);

  await socket.message({
    type: 'update',
    re: null,
    hash: 'not-the-real-hash-for-this-statuses-value',
    items: [{ op: 'statuses', spec: null, entry: { spec: null, plans: [{ plan: null, counts: { READY: 99 } }] } }],
  });

  assert.equal(socket.sent.length, 2);
  assert.equal(socket.sent[1].reset, true);
  assert.equal(socket.sent[1].id, 2);
  assert.equal(store.pending, true);

  const goodStatuses = [{ spec: null, plans: [{ plan: null, counts: { READY: 1 } }] }];
  await socket.message({
    type: 'snapshot', re: 2, hash: await ctx.sha256Hex(ctx.canonical(goodStatuses)),
    statuses: goodStatuses, facets: {}, decisions_open: 0, rows: [], edges: [], bodies: {},
  });

  assert.equal(socket.sent.length, 2, 'a matching hash fires no further resubscribe');
  assert.deepEqual(plain(store.statuses), goodStatuses);
  assert.equal(store.pending, false);
});

test('a socket close reconnects after 3 s and subscribes with reset: true', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const ctx = freshContext();
  ctx.createStore({ wsUrl: 'ws://x/ws' });
  const first = FakeSocket.instances[0];
  first.open();
  assert.equal(FakeSocket.instances.length, 1);

  first.close();
  assert.equal(FakeSocket.instances.length, 1, 'no reconnect before 3 s');
  t.mock.timers.tick(2999);
  assert.equal(FakeSocket.instances.length, 1);
  t.mock.timers.tick(1);
  assert.equal(FakeSocket.instances.length, 2, 'reconnected at 3 s');

  const second = FakeSocket.instances[1];
  second.open();
  // The id counter never resets: this store's second lifetime subscribe, not the second
  // connection's first.
  assert.deepEqual(second.sent, [{ type: 'subscribe', id: 2, filters: {}, open: [], watch: [], reset: true }]);
});

test("close(plan) also closes its open descendants and sends one subscribe; a drop removes the row's body and its watch", async () => {
  const ctx = freshContext();
  const store = ctx.createStore({ wsUrl: 'ws://x/ws', open: ['P1', 'T1'] });
  const socket = FakeSocket.instances[0];
  socket.open();
  await socket.message({
    type: 'snapshot', re: 1, hash: await ctx.sha256Hex(ctx.canonical([])),
    statuses: [], facets: {}, decisions_open: 0,
    rows: [
      { id: 'S1', kind: 'spec', title: 'S1', parent: null, ordinal: 0 },
      { id: 'P1', kind: 'plan', title: 'P1', parent: 'S1', ordinal: 0 },
      { id: 'T1', kind: 'task', title: 'T1', parent: 'P1', ordinal: 0, display: 'READY', score: 1 },
    ],
    edges: [],
    bodies: { T1: { node: { id: 'T1' }, sections: [] } },
  });
  store.watch(['T1']);

  store.close(['P1']);

  const closeSubscribe = socket.sent[socket.sent.length - 1];
  assert.deepEqual(closeSubscribe.open, [], 'closing the plan also closed the task open beneath it');

  await socket.message({ type: 'update', re: null, items: [{ op: 'drop', id: 'T1' }] });
  assert.equal(store.rows.has('T1'), false);
  assert.equal(store.bodies.has('T1'), false);

  store.resync();
  assert.deepEqual(socket.sent[socket.sent.length - 1].watch, [], 'the dropped id is no longer watched');
});

test('pending is true from a subscribe until the reply carrying its re arrives', async () => {
  const ctx = freshContext();
  const store = ctx.createStore({ wsUrl: 'ws://x/ws' });
  const socket = FakeSocket.instances[0];

  assert.equal(store.pending, false, 'nothing sent yet: the socket is not open');
  socket.open();
  assert.equal(store.pending, true);
  await socket.message({
    type: 'snapshot', re: 1, hash: await ctx.sha256Hex(ctx.canonical([])),
    statuses: [], facets: {}, decisions_open: 0, rows: [], edges: [], bodies: {},
  });
  assert.equal(store.pending, false);

  store.setFilters({ q: 'x' });
  assert.equal(store.pending, true);
  await socket.message({ type: 'update', re: 2, items: [] });
  assert.equal(store.pending, false);
});
