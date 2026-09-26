// The client's one source of truth for the protocol described in the plan's parent context:
// createStore(options) is the whole surface this file exposes. It defines nothing else at
// load time (no connection, no DOM) so core.js decides when and with what options to build
// window.tmStore.
//
// options:
//   wsUrl      - full ws(s):// URL; defaults to the current page's origin + /ws.
//   staticData - {statuses, hash, rows, edges, bodies, decisions}, every row/edge/body the
//                project has. Presence of this key is what puts the store in static mode:
//                everything after this point is computed locally, from web/visibility.py's
//                rules, instead of asked of a server.
//   filters, open, watch - the session's initial state (URL hash, remembered expansions).

const NO_REPO = '(none)';
const NO_SPEC = '(none)';
const NO_PHASE = '(none)';

// json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False): JSON.stringify
// on a plain value already omits whitespace and leaves non-ASCII untouched, but it orders
// object keys by insertion (and reorders integer-like keys first), so keys are sorted by hand
// here instead of trusting an intermediate object's own iteration order.
function canonical(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
}

async function sha256Hex(str) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(str));
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

function statusesHash(statuses) {
  return sha256Hex(canonical(statuses));
}

function toArray(ids) {
  return Array.isArray(ids) ? ids : [ids];
}

// --- visibility.py, ported for the static export's local read (see store.js's header) -------

function splitCsv(raw) {
  return (raw || '').split(',').filter(Boolean);
}

function modeMapOf(F, incKey, excKey) {
  const m = new Map();
  splitCsv(F[incKey]).forEach(v => m.set(v, 'include'));
  splitCsv(F[excKey]).forEach(v => m.set(v, 'exclude'));
  return m;
}

// mirrors visibility.py's parse_filters
function parseFilters(F) {
  F = F || {};
  const smin = F.smin;
  const smax = F.smax;
  return {
    statusMode: modeMapOf(F, 'status', 'xstatus'),
    phaseMode: modeMapOf(F, 'phase', 'xphase'),
    repoMode: modeMapOf(F, 'repo', 'xrepo'),
    modelMode: modeMapOf(F, 'model', 'xmodel'),
    specMode: modeMapOf(F, 'spec', 'xspec'),
    scoreMin: smin !== undefined && smin !== null && smin !== '' ? Number(smin) : null,
    scoreMax: smax !== undefined && smax !== null && smax !== '' ? Number(smax) : null,
    q: (F.q || '').toLowerCase(),
  };
}

// mirrors visibility.py's _dimension_passes
function dimensionPasses(mode, values) {
  if (values.some(v => mode.get(v) === 'exclude')) return false;
  const anyIncludes = [...mode.values()].some(m => m === 'include');
  return !anyIncludes || values.some(v => mode.get(v) === 'include');
}

// mirrors visibility.py's _structural_filter_active
function structuralFilterActive(filters) {
  return filters.statusMode.size > 0 || filters.phaseMode.size > 0 || filters.repoMode.size > 0 ||
    filters.modelMode.size > 0 || filters.specMode.size > 0 ||
    filters.scoreMin !== null || filters.scoreMax !== null;
}

// mirrors visibility.py's _spec_of
function specOf(rows, id) {
  let current = rows[id] ? rows[id].parent : null;
  while (current !== null && current !== undefined) {
    const row = rows[current];
    if (!row) return NO_SPEC;
    if (row.kind === 'spec') return current;
    current = row.parent;
  }
  return NO_SPEC;
}

// mirrors visibility.py's _task_passes
function taskPasses(filters, rows, row) {
  if (!dimensionPasses(filters.statusMode, [row.display])) return false;
  if (!dimensionPasses(filters.phaseMode, [row.phase || NO_PHASE])) return false;
  if (!dimensionPasses(filters.repoMode, [row.target_repo || NO_REPO])) return false;
  if (!dimensionPasses(filters.modelMode, row.acceptable_models || [])) return false;
  if (!dimensionPasses(filters.specMode, [specOf(rows, row.id)])) return false;
  const score = row.score;
  if (filters.scoreMin !== null && typeof score === 'number' && score < filters.scoreMin) return false;
  return !(filters.scoreMax !== null && typeof score === 'number' && score > filters.scoreMax);
}

// mirrors visibility.py's _text_matches
function textMatches(filters, row) {
  return row.title.toLowerCase().includes(filters.q) || row.id.toLowerCase().includes(filters.q);
}

// mirrors visibility.py's _text_accepts
function textAccepts(filters, row, parentTextOk) {
  return filters.q === '' || parentTextOk || textMatches(filters, row);
}

// mirrors visibility.py's _children_by_parent
function childrenByParent(rows) {
  const byParent = new Map();
  Object.values(rows).forEach(row => {
    if (!byParent.has(row.parent)) byParent.set(row.parent, []);
    byParent.get(row.parent).push(row.id);
  });
  byParent.forEach(children => children.sort((a, b) => {
    const oa = rows[a].ordinal, ob = rows[b].ordinal;
    return oa !== ob ? oa - ob : (a < b ? -1 : a > b ? 1 : 0);
  }));
  return byParent;
}

// mirrors visibility.py's _text_ok_by_id
function textOkById(rows, byParent, filters) {
  const textOk = {};
  function visit(id, parentTextOk) {
    const ok = textAccepts(filters, rows[id], parentTextOk);
    textOk[id] = ok;
    (byParent.get(id) || []).forEach(child => visit(child, ok));
  }
  (byParent.get(null) || []).forEach(root => visit(root, false));
  return textOk;
}

// mirrors visibility.py's _node_visible_by_id
function nodeVisibleById(rows, byParent, filters, textOk) {
  const visible = {};
  const structuralActive = structuralFilterActive(filters);
  function visit(id) {
    if (id in visible) return visible[id];
    const row = rows[id];
    let result;
    if (row.kind === 'task') {
      result = textOk[id] && taskPasses(filters, rows, row);
    } else {
      const children = byParent.get(id) || [];
      result = children.some(visit) || (!structuralActive && textOk[id]);
    }
    visible[id] = result;
    return result;
  }
  Object.keys(rows).forEach(visit);
  return visible;
}

// mirrors visibility.py's visible_ids
function visibleIds(rows, filters, open) {
  const byParent = childrenByParent(rows);
  const textOk = textOkById(rows, byParent, filters);
  const nodeVisible = nodeVisibleById(rows, byParent, filters, textOk);
  const openSet = new Set(open);
  const result = [];
  function walk(id) {
    if (!nodeVisible[id]) return;
    result.push(id);
    if (openSet.has(id)) (byParent.get(id) || []).forEach(walk);
  }
  (byParent.get(null) || []).forEach(walk);
  return result;
}

// mirrors visibility.py's _passes_other_dimensions
function passesOtherDimensions(filters, rows, row, exclude) {
  if (exclude !== 'status' && !dimensionPasses(filters.statusMode, [row.display])) return false;
  if (exclude !== 'phase' && !dimensionPasses(filters.phaseMode, [row.phase || NO_PHASE])) return false;
  if (exclude !== 'repo' && !dimensionPasses(filters.repoMode, [row.target_repo || NO_REPO])) return false;
  if (exclude !== 'model' && !dimensionPasses(filters.modelMode, row.acceptable_models || [])) return false;
  if (exclude !== 'spec' && !dimensionPasses(filters.specMode, [specOf(rows, row.id)])) return false;
  const score = row.score;
  if (filters.scoreMin !== null && typeof score === 'number' && score < filters.scoreMin) return false;
  if (filters.scoreMax !== null && typeof score === 'number' && score > filters.scoreMax) return false;
  return !(filters.q !== '' && !textMatches(filters, row));
}

// mirrors visibility.py's _dimension_counts
function dimensionCounts(filters, rows, dimension, valuesOf) {
  const counts = {};
  Object.values(rows).forEach(row => {
    if (row.kind !== 'task' || !passesOtherDimensions(filters, rows, row, dimension)) return;
    valuesOf(row).forEach(v => { counts[v] = (counts[v] || 0) + 1; });
  });
  return counts;
}

// mirrors visibility.py's facets
function facetsOf(rows, filters) {
  const scores = Object.values(rows)
    .filter(r => r.kind === 'task' && typeof r.score === 'number')
    .map(r => r.score);
  return {
    status: dimensionCounts(filters, rows, 'status', r => [r.display]),
    phase: dimensionCounts(filters, rows, 'phase', r => [r.phase || NO_PHASE]),
    repo: dimensionCounts(filters, rows, 'repo', r => [r.target_repo || NO_REPO]),
    model: dimensionCounts(filters, rows, 'model', r => r.acceptable_models || []),
    spec: dimensionCounts(filters, rows, 'spec', r => [specOf(rows, r.id)]),
    score: scores.length ? { min: Math.min(...scores), max: Math.max(...scores) } : { min: 0, max: 100 },
  };
}

// mirrors visibility.py's _nearest_visible_ancestor / _map_end
function mapEnd(rows, visibleSet, id) {
  if (visibleSet.has(id)) return id;
  let current = rows[id] ? rows[id].parent : null;
  while (current !== null && current !== undefined) {
    if (visibleSet.has(current)) return current;
    current = rows[current].parent;
  }
  return null;
}

// mirrors visibility.py's project_edges
function projectEdges(edges, rows, visible) {
  const visibleSet = new Set(visible);
  const seen = new Set();
  const result = [];
  function add(source, target, kind) {
    const key = `${source}\u0000${target}\u0000${kind}`;
    if (seen.has(key)) return;
    seen.add(key);
    result.push([source, target, kind]);
  }
  edges.forEach(([source, target, kind]) => {
    if (kind !== 'depends_on') return;
    const mappedSource = mapEnd(rows, visibleSet, source);
    const mappedTarget = mapEnd(rows, visibleSet, target);
    if (mappedSource === null || mappedTarget === null || mappedSource === mappedTarget) return;
    add(mappedSource, mappedTarget, 'depends_on');
  });
  visible.forEach(id => {
    const parent = rows[id].parent;
    if (visibleSet.has(parent)) add(parent, id, 'contains');
  });
  return result;
}

// --- statuses op ordering (null-spec first, else lexical) -----------------------------------

function specSortKey(id) {
  return id === null ? [0, ''] : [1, id];
}

function compareSpecEntries(a, b) {
  const ka = specSortKey(a.spec), kb = specSortKey(b.spec);
  if (ka[0] !== kb[0]) return ka[0] - kb[0];
  return ka[1] < kb[1] ? -1 : ka[1] > kb[1] ? 1 : 0;
}

// Same "null first, else lexical" rule, for a spec entry's own plans.
function comparePlanEntries(a, b) {
  const ka = specSortKey(a.plan), kb = specSortKey(b.plan);
  if (ka[0] !== kb[0]) return ka[0] - kb[0];
  return ka[1] < kb[1] ? -1 : ka[1] > kb[1] ? 1 : 0;
}

// A body's sections arrive as a list (bodies.py's shape); kept as a {key: {header,content,
// ordinal}} map internally so a "section" update item is an O(1) set/delete, not a list scan.
function normalizeBody(body) {
  if (!body) return body;
  const sections = {};
  (body.sections || []).forEach(s => {
    sections[s.key] = { header: s.header, content: s.content, ordinal: s.ordinal };
  });
  return { ...body, sections };
}

function diffMapIds(oldMap, newMap) {
  const ids = new Set([...oldMap.keys(), ...newMap.keys()]);
  const changed = [];
  ids.forEach(id => {
    const a = oldMap.get(id);
    const b = newMap.get(id);
    if (a !== b && JSON.stringify(a) !== JSON.stringify(b)) changed.push(id);
  });
  return changed;
}

function defaultWsUrl() {
  if (typeof location === 'undefined') {
    throw new Error('createStore: options.wsUrl is required outside a browser');
  }
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${location.host}/ws`;
}

function createStore(options) {
  const opts = options || {};
  const isStaticMode = Object.prototype.hasOwnProperty.call(opts, 'staticData');

  let rows = new Map();
  let edges = [];
  let facets = {};
  let statuses = [];
  let decisionsOpen = 0;
  let bodies = new Map();
  let connected = isStaticMode;
  let pending = false;

  let filters = { ...(opts.filters || {}) };
  const openSet = new Set(opts.open || []);
  const watchSet = new Set(opts.watch || []);

  const listeners = new Set();
  function notify(patch) {
    listeners.forEach(fn => {
      try { fn(patch); } catch { /* a listener's own bug is not the store's to swallow silently, but it must not break the next one */ }
    });
  }
  function onChange(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  }

  // --- static path: everything computed locally from the seeded full dataset ---------------

  let allRows = null;
  let allEdges = null;

  function recomputeStatic() {
    const parsed = parseFilters(filters);
    const visible = visibleIds(allRows, parsed, [...openSet]);
    const newRows = new Map(visible.map(id => [id, allRows[id]]));
    const rowIds = diffMapIds(rows, newRows);
    rows = newRows;
    edges = projectEdges(allEdges, allRows, visible);
    facets = facetsOf(allRows, parsed);
    notify({ rowIds, bodyIds: [], statusesChanged: false, facetsChanged: true, edgesChanged: true, connectionChanged: false });
  }

  function initStatic(data) {
    allRows = data.rows || {};
    allEdges = data.edges || [];
    statuses = data.statuses || [];
    decisionsOpen = (data.decisions || []).filter(d => d.status === 'OPEN').length;
    bodies = new Map(Object.entries(data.bodies || {}).map(([id, b]) => [id, normalizeBody(b)]));
    recomputeStatic();
  }

  // --- live path: a websocket speaking the /ws protocol ------------------------------------

  let socket = null;
  let reconnectTimer = null;
  let nextMsgId = 1;
  let pendingId = null;

  function sendSubscribe(reset) {
    if (!socket || socket.readyState !== 1 /* OPEN */) return;
    const id = nextMsgId++;
    pendingId = id;
    pending = true;
    socket.send(JSON.stringify({
      type: 'subscribe',
      id,
      filters,
      open: [...openSet],
      watch: [...watchSet].slice(0, 200),
      reset: !!reset,
    }));
  }

  function applySnapshot(msg) {
    const oldRows = rows;
    const oldBodies = bodies;
    rows = new Map((msg.rows || []).map(r => [r.id, r]));
    edges = (msg.edges || []).slice();
    facets = msg.facets || {};
    statuses = msg.statuses || [];
    decisionsOpen = msg.decisions_open || 0;
    bodies = new Map(Object.entries(msg.bodies || {}).map(([id, b]) => [id, normalizeBody(b)]));
    notify({
      rowIds: diffMapIds(oldRows, rows),
      bodyIds: diffMapIds(oldBodies, bodies),
      statusesChanged: true,
      facetsChanged: true,
      edgesChanged: true,
      connectionChanged: false,
    });
  }

  function applyStatusesItem(item) {
    const idx = statuses.findIndex(s => s.spec === item.spec);
    if (item.entry === null) {
      if (idx !== -1) statuses.splice(idx, 1);
    } else if (idx !== -1) {
      statuses[idx] = item.entry;
    } else {
      statuses.push(item.entry);
      statuses.sort(compareSpecEntries);
    }
  }

  function applyPlanCountsItem(item) {
    const specEntry = statuses.find(s => s.spec === item.spec);
    if (!specEntry) return;
    const idx = specEntry.plans.findIndex(p => p.plan === item.plan);
    if (item.counts === null) {
      if (idx !== -1) specEntry.plans.splice(idx, 1);
    } else if (idx !== -1) {
      specEntry.plans[idx] = { plan: item.plan, counts: item.counts };
    } else {
      specEntry.plans.push({ plan: item.plan, counts: item.counts });
      specEntry.plans.sort(comparePlanEntries);
    }
  }

  function applyEdgesItem(item) {
    const key = e => e.join('\u0000');
    const removeKeys = new Set((item.remove || []).map(key));
    edges = edges.filter(e => !removeKeys.has(key(e)));
    const existing = new Set(edges.map(key));
    (item.add || []).forEach(e => {
      const k = key(e);
      if (!existing.has(k)) { edges.push(e); existing.add(k); }
    });
  }

  function bodyOf(bodyId) {
    let body = bodies.get(bodyId);
    if (!body) { body = { sections: {} }; bodies.set(bodyId, body); }
    return body;
  }

  function applyUpdate(msg) {
    const rowIds = new Set();
    const bodyIds = new Set();
    let statusesChanged = false;
    let facetsChanged = false;
    let edgesChanged = false;
    for (const item of msg.items || []) {
      switch (item.op) {
        case 'row':
          rows.set(item.row.id, item.row);
          rowIds.add(item.row.id);
          break;
        case 'drop':
          rows.delete(item.id);
          bodies.delete(item.id);
          watchSet.delete(item.id);
          rowIds.add(item.id);
          bodyIds.add(item.id);
          break;
        case 'statuses':
          applyStatusesItem(item);
          statusesChanged = true;
          break;
        case 'plan_counts':
          applyPlanCountsItem(item);
          statusesChanged = true;
          break;
        case 'edges':
          applyEdgesItem(item);
          edgesChanged = true;
          break;
        case 'facets':
          facets = item.facets;
          facetsChanged = true;
          break;
        case 'decisions_open':
          decisionsOpen = item.count;
          break;
        case 'section': {
          const body = bodyOf(item.id);
          if (item.section === null) delete body.sections[item.key];
          else body.sections[item.key] = item.section;
          bodyIds.add(item.id);
          break;
        }
        case 'body':
          bodyOf(item.id)[item.part] = item.value;
          bodyIds.add(item.id);
          break;
        default:
          break;
      }
    }
    notify({
      rowIds: [...rowIds],
      bodyIds: [...bodyIds],
      statusesChanged,
      facetsChanged,
      edgesChanged,
      connectionChanged: false,
    });
  }

  async function handleMessage(raw) {
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }
    if (msg.re !== undefined && msg.re !== null && msg.re === pendingId) pending = false;
    if (msg.type === 'snapshot') applySnapshot(msg);
    else if (msg.type === 'update') applyUpdate(msg);
    else if (msg.type !== 'error') return;
    if (typeof msg.hash === 'string') {
      const ownHash = await statusesHash(statuses);
      if (ownHash !== msg.hash) sendSubscribe(true);
    }
  }

  function connect() {
    socket = new WebSocket(opts.wsUrl || defaultWsUrl());
    socket.onopen = () => {
      connected = true;
      notify({ rowIds: [], bodyIds: [], statusesChanged: false, facetsChanged: false, edgesChanged: false, connectionChanged: true });
      sendSubscribe(true);
    };
    socket.onmessage = (ev) => handleMessage(ev.data);
    socket.onclose = () => {
      connected = false;
      notify({ rowIds: [], bodyIds: [], statusesChanged: false, facetsChanged: false, edgesChanged: false, connectionChanged: true });
      reconnectTimer = setTimeout(connect, 3000);
    };
  }

  // --- shared: closing a container also closes whatever open descendant it still has --------

  function closeWithDescendants(ids, getParent) {
    const toClose = new Set(toArray(ids));
    let changed = true;
    while (changed) {
      changed = false;
      for (const id of openSet) {
        if (toClose.has(id)) continue;
        let parent = getParent(id);
        while (parent !== null && parent !== undefined) {
          if (toClose.has(parent)) { toClose.add(id); changed = true; break; }
          parent = getParent(parent);
        }
      }
    }
    toClose.forEach(id => openSet.delete(id));
  }

  const liveGetParent = (id) => { const r = rows.get(id); return r ? r.parent : null; };
  const staticGetParent = (id) => { const r = allRows[id]; return r ? r.parent : null; };

  let setFilters;
  let openFn;
  let closeFn;
  let watchFn;
  let unwatchFn;
  let resyncFn;

  if (isStaticMode) {
    setFilters = (F) => { filters = { ...F }; recomputeStatic(); };
    openFn = (ids) => { toArray(ids).forEach(id => openSet.add(id)); recomputeStatic(); };
    closeFn = (ids) => { closeWithDescendants(ids, staticGetParent); recomputeStatic(); };
    watchFn = (ids) => { toArray(ids).forEach(id => watchSet.add(id)); };
    unwatchFn = (ids) => { toArray(ids).forEach(id => watchSet.delete(id)); };
    resyncFn = () => recomputeStatic();
    initStatic(opts.staticData);
  } else {
    setFilters = (F) => { filters = { ...F }; sendSubscribe(false); };
    openFn = (ids) => { toArray(ids).forEach(id => openSet.add(id)); sendSubscribe(false); };
    closeFn = (ids) => { closeWithDescendants(ids, liveGetParent); sendSubscribe(false); };
    watchFn = (ids) => { toArray(ids).forEach(id => watchSet.add(id)); sendSubscribe(false); };
    unwatchFn = (ids) => { toArray(ids).forEach(id => watchSet.delete(id)); sendSubscribe(false); };
    resyncFn = () => sendSubscribe(true);
    connect();
  }

  return {
    get rows() { return rows; },
    get edges() { return edges; },
    get facets() { return facets; },
    get statuses() { return statuses; },
    get decisionsOpen() { return decisionsOpen; },
    get bodies() { return bodies; },
    get connected() { return connected; },
    get pending() { return pending; },
    setFilters,
    open: openFn,
    close: closeFn,
    watch: watchFn,
    unwatch: unwatchFn,
    resync: resyncFn,
    onChange,
  };
}
