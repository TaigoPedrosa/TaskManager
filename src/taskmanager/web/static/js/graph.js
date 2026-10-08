// Vis Network DAG Graph, drawn from the store's visible set and its projected edges. The
// network is built once (renderGraph) and every later store change patches the same
// vis.DataSets in place (syncGraph*) -- never destroyed or rebuilt, so pan/zoom/selection
// survive a data change, and a filtered-out node is simply never in the DataSet rather than
// dimmed after the fact.
// Shape per node kind so spec/plan/task read as distinct at a glance, independent of the
// status colouring the fill and border already carry.
const GRAPH_SHAPE_BY_KIND = {
  spec: { shape: 'hexagon' },
  plan: { shape: 'box', shapeProperties: { borderRadius: 14 } },
  task: { shape: 'box', shapeProperties: { borderRadius: 3 } },
};

let visEdgesDS = null;
// zinc-100: the selected node's stroke, in place of its status stroke.
const SELECTED_STROKE = '#f4f4f5';

function graphEdgeId(source, target, type) {
  return `${source}\u0000${target}\u0000${type}`;
}

// A box shows the node's id on its status fill; its status, title and roll-up are its tooltip.
function graphVisNode(row) {
  const theme = getTheme(displayOf(row));
  return {
    id: row.id,
    label: row.id,
    ...(GRAPH_SHAPE_BY_KIND[row.kind] || GRAPH_SHAPE_BY_KIND.task),
    margin: 10,
    widthConstraint: { minimum: 170, maximum: 260 },
    color: {
      background: theme.graph_bg,
      border: row.id === selectedNodeId ? SELECTED_STROKE : theme.graph_border,
      highlight: { background: theme.graph_bg, border: SELECTED_STROKE }
    },
    font: { color: '#ffffff', face: 'Inter', size: 12, mod: '600' },
    borderWidth: 2,
    shadow: { enabled: true, color: 'rgba(0,0,0,0.5)', size: 4 }
  };
}

// The status label is spelled out here, so no status icon sits beside it; a container adds
// its roll-up from the counts tree, and a node held by open decisions links each one.
function graphTipHtml(id) {
  const row = window.tmStore.rows.get(id);
  if (!row) return esc(id);
  const theme = getTheme(displayOf(row));
  const rollup = row.kind === 'task' ? '' : `<div class="text-zinc-400">${esc(progressText(countsForRow(row)))}</div>`;
  const decisions = (row.waits_on || []).filter(w => w.startsWith('decision-'));
  const chips = decisions.length ? `<div class="flex flex-wrap gap-1.5">${decisions.map(d => idLink(d, 'decision', 'chip')).join('')}</div>` : '';
  return `<div class="graph-tip flex flex-col gap-1.5 px-1 py-1.5 font-normal"><div class="flex items-center gap-2">${idLink(row.id, row.kind)}<span class="graph-tip-status text-zinc-400">${esc(theme.label)}</span></div><div class="text-xs leading-4 text-zinc-300">${esc(row.title)}</div>${rollup}${chips}</div>`;
}

// Leaving a node waits a moment before the tooltip goes, so the pointer can cross to its links.
const GRAPH_TIP_LINGER_MS = 200;
let graphTipLinger = null;

function showGraphTip(anchor, id) {
  clearTimeout(graphTipLinger);
  showTip(anchor, graphTipHtml(id), true);
}

function lingerGraphTip() {
  clearTimeout(graphTipLinger);
  graphTipLinger = setTimeout(hideTip, GRAPH_TIP_LINGER_MS);
}

tipEl.addEventListener('mouseenter', () => clearTimeout(graphTipLinger));

// The tooltip sits at the end of the page, so a focused node hands Tab to its links by hand:
// Tab from the node enters them, Shift+Tab before the first returns to the node, Tab past the
// last goes on to whatever follows the node, and Escape closes it with focus on the node.
function focusAfter(el) {
  const order = [...document.querySelectorAll(FOCUSABLE)]
    .filter(f => !tipEl.contains(f) && !f.disabled && !f.closest('.hidden, [hidden]'));
  const next = order[order.indexOf(el) + 1];
  if (next) next.focus();
  return Boolean(next);
}

tipEl.addEventListener('keydown', (e) => {
  const proxy = tipAnchor;
  if (!proxy) return;
  const links = [...tipEl.querySelectorAll('a')];
  const at = links.indexOf(document.activeElement);
  if (e.key === 'Escape') {
    proxy.focus();
  } else if (e.key === 'Tab' && e.shiftKey && at === 0) {
    e.preventDefault();
    proxy.focus();
  } else if (e.key === 'Tab' && !e.shiftKey && at === links.length - 1 && focusAfter(proxy)) {
    e.preventDefault();
  }
});

tipEl.addEventListener('focusout', (e) => {
  if (!tipEl.contains(e.relatedTarget)) hideTip();
});

function graphVisEdge([source, target, type]) {
  return {
    id: graphEdgeId(source, target, type),
    from: source,
    to: target,
    arrows: 'to',
    dashes: type === 'contains',
    color: { color: type === 'contains' ? '#4b5563' : '#10b981', highlight: '#ffffff' },
    width: type === 'contains' ? 1 : 2
  };
}

function renderGraph() {
  const container = document.getElementById('network-canvas');

  visNodesDS = new vis.DataSet([...window.tmStore.rows.values()].map(graphVisNode));
  visEdgesDS = new vis.DataSet(window.tmStore.edges.map(graphVisEdge));
  const data = { nodes: visNodesDS, edges: visEdgesDS };
  const options = {
    layout: {
      hierarchical: {
        direction: 'UD',
        sortMethod: 'directed',
        levelSeparation: 240,
        nodeSpacing: 320
      }
    },
    physics: false,
    // A canvas node has no DOM presence of its own; each one gets a focusable proxy over it
    // (syncGraphFocusProxies), and vis's own keyboard interaction pans and zooms the canvas.
    // Bound to the canvas, not the window: vis renders on the first store change in every view,
    // and its window-bound keys swallow the arrows every other pane reads.
    interaction: { hover: true, selectConnectedEdges: true, keyboard: { enabled: true, bindToWindow: false } }
  };

  networkInstance = new vis.Network(container, data, options);

  networkInstance.on('click', (params) => {
    if (params.nodes.length > 0) {
      showGraphInspector(params.nodes[0]);
    }
  });
  networkInstance.on('hoverNode', (params) => {
    const proxy = graphFocusProxies.get(params.node);
    if (proxy) showGraphTip(proxy, params.node);
  });
  networkInstance.on('blurNode', lingerGraphTip);
  networkInstance.on('dragStart', hideTip);
  networkInstance.on('zoom', hideTip);
  networkInstance.on('afterDrawing', syncGraphFocusProxies);
  // A container stands for its subtree; opening or closing one is the same store-driven
  // toggle the tree view's own rows use, so the two views can never disagree about what's open.
  networkInstance.on('doubleClick', (params) => {
    if (params.nodes.length === 0) return;
    const row = window.tmStore.rows.get(params.nodes[0]);
    if (row && row.kind !== 'task') toggleExpand(row);
  });
}

// One transparent button over each drawn node: Tab reaches the node, focus opens its tooltip
// as hover does, and Enter opens its details. Pointer events pass through to the canvas.
const graphFocusLayer = document.createElement('div');
graphFocusLayer.className = 'absolute inset-0 overflow-hidden pointer-events-none';
const graphFocusProxies = new Map();

function syncGraphFocusProxies() {
  if (graphFocusLayer.parentNode !== networkCanvas) networkCanvas.appendChild(graphFocusLayer);
  const ids = visNodesDS.getIds();
  const keep = new Set(ids);
  graphFocusProxies.forEach((btn, id) => {
    if (!keep.has(id)) {
      btn.remove();
      graphFocusProxies.delete(id);
    }
  });
  ids.forEach((id) => {
    let btn = graphFocusProxies.get(id);
    if (!btn) {
      btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `graph-node-focus absolute rounded-md pointer-events-none ${FOCUS_RING}`;
      btn.setAttribute('tabindex', '0');
      btn.setAttribute('data-node-id', id);
      btn.addEventListener('focusin', () => showGraphTip(btn, id));
      btn.addEventListener('focusout', (e) => {
        if (!tipEl.contains(e.relatedTarget)) hideTip();
      });
      btn.addEventListener('keydown', (e) => {
        const first = tipAnchor === btn && tipEl.querySelector('a');
        if (e.key !== 'Tab' || e.shiftKey || !first) return;
        e.preventDefault();
        first.focus();
      });
      btn.addEventListener('click', () => showGraphInspector(id));
      graphFocusLayer.appendChild(btn);
      graphFocusProxies.set(id, btn);
    }
    const row = window.tmStore.rows.get(id);
    btn.setAttribute('aria-label', row ? `${row.id}: ${row.title}` : id);
    const box = networkInstance.getBoundingBox(id);
    if (!box) return;
    const topLeft = networkInstance.canvasToDOM({ x: box.left, y: box.top });
    const bottomRight = networkInstance.canvasToDOM({ x: box.right, y: box.bottom });
    btn.style.left = `${topLeft.x}px`;
    btn.style.top = `${topLeft.y}px`;
    btn.style.width = `${bottomRight.x - topLeft.x}px`;
    btn.style.height = `${bottomRight.y - topLeft.y}px`;
  });
}

// Adds, updates or drops exactly the rows a patch names. A node whose data changed in place
// (still present, e.g. a new status colour) goes through DataSet.update() on an id already in
// the set, which vis treats as a data change, not a structural one -- the hierarchical layout
// only re-lays nodes the DataSet actually gained or lost.
function syncGraphNodes(rowIds) {
  const present = [];
  const gone = [];
  rowIds.forEach(id => {
    const row = window.tmStore.rows.get(id);
    if (row) present.push(graphVisNode(row)); else gone.push(id);
  });
  if (gone.length) visNodesDS.remove(gone);
  if (present.length) visNodesDS.update(present);
}

// A container's summary line reads the counts tree, not its own row, so a `plan_counts`/
// `statuses` item changes it with no matching `row` item -- refresh every container node
// already drawn. Same DataSet.update()-on-an-existing-id path as above: no layout re-run.
function refreshGraphContainerLabels() {
  const present = visNodesDS.getIds()
    .map(id => window.tmStore.rows.get(id))
    .filter(row => row && row.kind !== 'task')
    .map(graphVisNode);
  if (present.length) visNodesDS.update(present);
}

// The store hands over the whole projected-edge list whenever it changes (no per-item add/
// remove), so the desired set is diffed against the DataSet's own ids here.
function syncGraphEdges() {
  const desired = window.tmStore.edges.map(graphVisEdge);
  const desiredIds = new Set(desired.map(e => e.id));
  const toRemove = visEdgesDS.getIds().filter(id => !desiredIds.has(id));
  if (toRemove.length) visEdgesDS.remove(toRemove);
  if (desired.length) visEdgesDS.update(desired);
}

function syncGraph(patch) {
  if (!networkInstance) {
    renderGraph();
    return;
  }
  if (patch.rowIds.length) syncGraphNodes(patch.rowIds);
  if (patch.statusesChanged) refreshGraphContainerLabels();
  if (patch.edgesChanged) syncGraphEdges();
}

window.tmStore.onChange(syncGraph);
