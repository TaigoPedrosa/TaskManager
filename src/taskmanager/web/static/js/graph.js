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

function graphEdgeId(source, target, type) {
  return `${source}\u0000${target}\u0000${type}`;
}

// A task's summary line is its status; a container's is the counts tree's roll-up
// (countsForRow/progressText, both core.js's), which already folds in the container's own
// unit once its own review/fix/merge cycle has started (rows.py's `statuses`).
function graphVisNode(row) {
  const theme = getTheme(displayOf(row));
  const summary = row.kind === 'task' ? theme.label : progressText(countsForRow(row));
  return {
    id: row.id,
    label: `${row.id}\n${row.title}\n*${summary}*`,
    ...(GRAPH_SHAPE_BY_KIND[row.kind] || GRAPH_SHAPE_BY_KIND.task),
    margin: 16,
    widthConstraint: { minimum: 170, maximum: 260 },
    color: {
      background: theme.graph_bg,
      border: theme.graph_border,
      highlight: { background: theme.graph_bg, border: '#ffffff' }
    },
    font: { color: '#f3f4f6', face: 'Inter', size: 16, multi: 'md' },
    borderWidth: 2,
    shadow: { enabled: true, color: 'rgba(0,0,0,0.5)', size: 4 }
  };
}

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
    // A canvas node has no DOM presence to Tab to (createNodeRow's tree row is the reachable
    // path to the same inspector), but vis's own keyboard interaction at least lets someone
    // already focused on the canvas pan/zoom/select without a mouse.
    interaction: { hover: true, selectConnectedEdges: true, keyboard: true }
  };

  networkInstance = new vis.Network(container, data, options);

  networkInstance.on('click', (params) => {
    if (params.nodes.length > 0) {
      showGraphInspector(params.nodes[0]);
    }
  });
  // A container stands for its subtree; opening or closing one is the same store-driven
  // toggle the tree view's own rows use, so the two views can never disagree about what's open.
  networkInstance.on('doubleClick', (params) => {
    if (params.nodes.length === 0) return;
    const row = window.tmStore.rows.get(params.nodes[0]);
    if (row && row.kind !== 'task') toggleExpand(row);
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
