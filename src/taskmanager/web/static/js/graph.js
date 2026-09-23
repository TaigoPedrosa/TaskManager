// Vis Network DAG Graph
// Shape per node kind so spec/plan/task/review_gate read as distinct at a glance,
// independent of the status colouring the fill and border already carry.
const GRAPH_SHAPE_BY_KIND = {
  spec: { shape: 'hexagon' },
  plan: { shape: 'box', shapeProperties: { borderRadius: 14 } },
  task: { shape: 'box', shapeProperties: { borderRadius: 3 } },
  review_gate: { shape: 'diamond' }
};

function renderGraph(graph) {
  const container = document.getElementById('network-canvas');

  const visNodes = graph.nodes.map(n => {
    const theme = getTheme(n.status);
    return {
      id: n.id,
      label: `${n.id}\n${n.title}\n*${theme.label}*`,
      ...(GRAPH_SHAPE_BY_KIND[n.kind] || GRAPH_SHAPE_BY_KIND.task),
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
  });

  const visEdges = graph.edges.map(e => ({
    from: e.source,
    to: e.target,
    arrows: 'to',
    dashes: e.type === 'contains',
    color: { color: e.type === 'contains' ? '#4b5563' : '#10b981', highlight: '#ffffff' },
    width: e.type === 'contains' ? 1 : 2
  }));

  visNodesDS = new vis.DataSet(visNodes);
  const data = { nodes: visNodesDS, edges: new vis.DataSet(visEdges) };
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
    interaction: { hover: true, selectConnectedEdges: true }
  };

  if (networkInstance) {
    networkInstance.destroy();
  }
  networkInstance = new vis.Network(container, data, options);
  applyGraphFilter();

  networkInstance.on('click', (params) => {
    if (params.nodes.length > 0) {
      showGraphInspector(params.nodes[0]);
    }
  });
}

