"""HTML/CSS/JS template for the TaskManager interactive web visualizer."""

import json
from typing import Any


def get_web_html(initial_data: dict[str, Any] | None = None) -> str:
    embedded_script = ""
    if initial_data is not None:
        raw_json = json.dumps(initial_data)
        embedded_script = f"<script>window.STATIC_DATA = {raw_json};</script>"

    return f"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>TaskManager — Interactive Visualizer</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {{
      darkMode: 'class',
      theme: {{
        extend: {{
          colors: {{
            brand: {{
              50: '#f0fdf4',
              500: '#22c55e',
              600: '#16a34a',
              900: '#14532d',
            }}
          }}
        }}
      }}
    }}
  </script>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap');
    body {{
      font-family: 'Inter', sans-serif;
    }}
    code, pre, .font-mono {{
      font-family: 'JetBrains Mono', monospace;
    }}
    /* Custom Scrollbars */
    ::-webkit-scrollbar {{
      width: 6px;
      height: 6px;
    }}
    ::-webkit-scrollbar-track {{
      background: rgba(0, 0, 0, 0.1);
    }}
    ::-webkit-scrollbar-thumb {{
      background: rgba(156, 163, 175, 0.3);
      border-radius: 3px;
    }}
    ::-webkit-scrollbar-thumb:hover {{
      background: rgba(156, 163, 175, 0.5);
    }}
    .status-READY {{ background-color: #065f46; color: #34d399; border-color: #059669; }}
    .status-IMPLEMENTING, .status-IN_FLIGHT {{ background-color: #1e3a8a; color: #60a5fa; border-color: #2563eb; }}
    .status-WAITING_REVIEW {{ background-color: #78350f; color: #fbbf24; border-color: #d97706; }}
    .status-WAITING_FIXES, .status-FIXING {{ background-color: #831843; color: #f472b6; border-color: #db2777; }}
    .status-WAITING_MERGE {{ background-color: #164e63; color: #22d3ee; border-color: #0891b2; }}
    .status-COMPLETED {{ background-color: #14532d; color: #4ade80; border-color: #16a34a; }}
    .status-BLOCKED {{ background-color: #374151; color: #9ca3af; border-color: #4b5563; }}
    .status-NOT_STARTED {{ background-color: #1f2937; color: #d1d5db; border-color: #374151; }}
  </style>
  {embedded_script}
</head>
<body class="bg-zinc-950 text-zinc-100 flex flex-col h-screen overflow-hidden selection:bg-emerald-500 selection:text-black">

  <!-- Header -->
  <header class="h-14 border-b border-zinc-800 px-4 flex items-center justify-between bg-zinc-900/50 backdrop-blur z-20 flex-shrink-0">
    <div class="flex items-center space-x-3">
      <div class="w-8 h-8 rounded-lg bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center font-bold text-emerald-400 font-mono">
        tm
      </div>
      <div>
        <h1 class="text-sm font-semibold tracking-wide flex items-center gap-2">
          TaskManager
          <span id="connection-pill" class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-950 text-emerald-400 border border-emerald-800">
            <span id="connection-dot" class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
            <span id="connection-status">Live</span>
          </span>
        </h1>
      </div>
    </div>

    <!-- Quick Stats -->
    <div id="stats-container" class="hidden md:flex items-center space-x-2 text-xs">
      <span class="px-2.5 py-1 rounded bg-zinc-800 border border-zinc-700 text-zinc-300">Total: <strong id="stat-total" class="text-white">0</strong></span>
      <span class="px-2.5 py-1 rounded bg-emerald-950 border border-emerald-800 text-emerald-300">Ready: <strong id="stat-ready" class="text-emerald-100">0</strong></span>
      <span class="px-2.5 py-1 rounded bg-blue-950 border border-blue-800 text-blue-300">In Flight: <strong id="stat-inflight" class="text-blue-100">0</strong></span>
      <span class="px-2.5 py-1 rounded bg-amber-950 border border-amber-800 text-amber-300">Review: <strong id="stat-review" class="text-amber-100">0</strong></span>
      <span class="px-2.5 py-1 rounded bg-zinc-800 border border-zinc-700 text-zinc-400">Done: <strong id="stat-done" class="text-emerald-400">0</strong></span>
    </div>

    <!-- Mode Switcher -->
    <div class="flex items-center space-x-2">
      <div class="bg-zinc-800 p-0.5 rounded-lg border border-zinc-700 flex text-xs">
        <button id="view-split" class="px-3 py-1.5 rounded-md font-medium bg-zinc-700 text-white shadow-sm transition">Split</button>
        <button id="view-tree" class="px-3 py-1.5 rounded-md font-medium text-zinc-400 hover:text-white transition">Tree</button>
        <button id="view-graph" class="px-3 py-1.5 rounded-md font-medium text-zinc-400 hover:text-white transition">Graph</button>
      </div>
      <button id="refresh-btn" title="Refresh state" class="p-2 rounded-lg bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-zinc-300 hover:text-white transition">
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
      </button>
    </div>
  </header>

  <!-- Main View Area -->
  <div class="flex-1 flex overflow-hidden">
    
    <!-- Sidebar / Tree Navigation -->
    <aside id="tree-pane" class="w-80 border-r border-zinc-800 flex flex-col bg-zinc-900/30 flex-shrink-0 transition-all duration-200">
      <div class="p-3 border-b border-zinc-800">
        <div class="relative">
          <input id="search-box" type="text" placeholder="Filter tasks, plans, specs..." class="w-full bg-zinc-950 border border-zinc-800 rounded-lg px-3 py-1.5 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500">
        </div>
      </div>
      <div id="tree-list" class="flex-1 overflow-y-auto p-2 space-y-1">
        <!-- Dynamically Populated Tree -->
      </div>
    </aside>

    <!-- Center Detail / Content Area -->
    <main id="detail-pane" class="flex-1 flex flex-col min-w-0 bg-zinc-950 overflow-hidden border-r border-zinc-800">
      <!-- Node Header Bar -->
      <div id="node-header" class="p-4 border-b border-zinc-800 bg-zinc-900/40 flex items-start justify-between">
        <div class="space-y-1">
          <div class="flex items-center gap-2">
            <span id="node-kind-badge" class="px-2 py-0.5 rounded text-[10px] font-mono uppercase bg-zinc-800 text-zinc-400">SELECT NODE</span>
            <span id="node-id" class="font-mono text-xs font-semibold text-emerald-400">---</span>
            <span id="node-status-badge" class="px-2 py-0.5 rounded-full text-xs font-medium border border-transparent"></span>
          </div>
          <h2 id="node-title" class="text-base font-semibold text-zinc-100">Select a task or plan to inspect</h2>
        </div>
        <div id="node-meta-pills" class="flex flex-wrap gap-1.5 text-xs justify-end max-w-xs">
          <!-- Metadata pills populated dynamically -->
        </div>
      </div>

      <!-- Markdown & Sections Body -->
      <div id="markdown-container" class="flex-1 overflow-y-auto p-6 space-y-6">
        <div class="text-zinc-500 text-sm italic">No node selected. Click on any item in the tree or graph to view details.</div>
      </div>
    </main>

    <!-- Graph Visualizer Pane -->
    <section id="graph-pane" class="flex-1 flex flex-col bg-zinc-950/60 overflow-hidden relative">
      <div class="absolute top-3 right-3 z-10 flex gap-2">
        <button id="graph-fit" class="px-2.5 py-1 text-xs rounded bg-zinc-800/80 hover:bg-zinc-700 text-zinc-300 border border-zinc-700 backdrop-blur">Fit View</button>
      </div>
      <div id="network-canvas" class="w-full h-full"></div>
    </section>

  </div>

  <script>
    // State
    let treeData = [];
    let graphData = {{ nodes: [], edges: [] }};
    let selectedNodeId = null;
    let networkInstance = null;
    let currentLayout = 'split'; // 'split', 'tree', 'graph'
    let isStaticMode = typeof window.STATIC_DATA !== 'undefined';

    // Elements
    const treePane = document.getElementById('tree-pane');
    const detailPane = document.getElementById('detail-pane');
    const graphPane = document.getElementById('graph-pane');
    const searchBox = document.getElementById('search-box');
    const treeList = document.getElementById('tree-list');
    const markdownContainer = document.getElementById('markdown-container');
    const nodeHeader = document.getElementById('node-header');
    const connectionStatus = document.getElementById('connection-status');
    const connectionDot = document.getElementById('connection-dot');

    // Layout Switching
    function setLayout(mode) {{
      currentLayout = mode;
      ['view-split', 'view-tree', 'view-graph'].forEach(id => {{
        document.getElementById(id).classList.remove('bg-zinc-700', 'text-white');
        document.getElementById(id).classList.add('text-zinc-400');
      }});

      if (mode === 'split') {{
        document.getElementById('view-split').classList.add('bg-zinc-700', 'text-white');
        treePane.classList.remove('hidden');
        detailPane.classList.remove('hidden');
        graphPane.classList.remove('hidden');
      }} else if (mode === 'tree') {{
        document.getElementById('view-tree').classList.add('bg-zinc-700', 'text-white');
        treePane.classList.remove('hidden');
        detailPane.classList.remove('hidden');
        graphPane.classList.add('hidden');
      }} else if (mode === 'graph') {{
        document.getElementById('view-graph').classList.add('bg-zinc-700', 'text-white');
        treePane.classList.add('hidden');
        detailPane.classList.add('hidden');
        graphPane.classList.remove('hidden');
      }}
      if (networkInstance) {{
        setTimeout(() => networkInstance.fit(), 100);
      }}
    }}

    document.getElementById('view-split').addEventListener('click', () => setLayout('split'));
    document.getElementById('view-tree').addEventListener('click', () => setLayout('tree'));
    document.getElementById('view-graph').addEventListener('click', () => setLayout('graph'));
    document.getElementById('graph-fit').addEventListener('click', () => networkInstance && networkInstance.fit());
    document.getElementById('refresh-btn').addEventListener('click', loadAllData);

    // Fetch and Load Data
    async function loadAllData() {{
      if (isStaticMode) {{
        treeData = window.STATIC_DATA.tree || [];
        graphData = window.STATIC_DATA.graph || {{ nodes: [], edges: [] }};
        updateStats(window.STATIC_DATA.stats || {{}});
        renderTree(treeData);
        renderGraph(graphData);
        if (treeData.length > 0 && !selectedNodeId) {{
          selectNode(treeData[0].id);
        }}
        return;
      }}

      try {{
        const [treeRes, graphRes, statsRes] = await Promise.all([
          fetch('/api/tree'),
          fetch('/api/graph'),
          fetch('/api/stats')
        ]);
        treeData = await treeRes.json();
        graphData = await graphRes.json();
        const stats = await statsRes.json();

        updateStats(stats);
        renderTree(treeData);
        renderGraph(graphData);

        if (!selectedNodeId && treeData.length > 0) {{
          selectNode(treeData[0].id);
        }}
      }} catch (err) {{
        console.error('Failed to load data:', err);
      }}
    }}

    function updateStats(stats) {{
      document.getElementById('stat-total').textContent = stats.total || 0;
      document.getElementById('stat-ready').textContent = stats.ready || 0;
      document.getElementById('stat-inflight').textContent = stats.in_flight || 0;
      document.getElementById('stat-review').textContent = stats.waiting_review || 0;
      document.getElementById('stat-done').textContent = stats.completed || 0;
    }}

    // Render Tree
    function renderTree(nodes, filterText = '') {{
      treeList.innerHTML = '';
      const filter = filterText.toLowerCase();

      function createNodeRow(node, depth = 0) {{
        const matches = !filter || node.title.toLowerCase().includes(filter) || node.id.toLowerCase().includes(filter);
        const item = document.createElement('div');
        item.className = `flex items-center justify-between px-2.5 py-1.5 rounded cursor-pointer text-xs group transition ${{selectedNodeId === node.id ? 'bg-zinc-800 text-white font-medium border border-zinc-700' : 'text-zinc-400 hover:bg-zinc-850 hover:text-zinc-200'}}`;
        item.style.paddingLeft = `${{depth * 14 + 10}}px`;
        item.onclick = () => selectNode(node.id);

        const statusClass = `status-${{node.virtual_status || node.status}}`;

        item.innerHTML = `
          <div class="flex items-center gap-2 truncate">
            <span class="font-mono text-[10px] text-zinc-500 uppercase">${{node.kind}}</span>
            <span class="truncate">${{node.title}}</span>
          </div>
          <span class="px-1.5 py-0.5 rounded text-[10px] font-mono border ${{statusClass}}">${{node.virtual_status || node.status}}</span>
        `;

        if (matches) treeList.appendChild(item);

        if (node.children) {{
          node.children.forEach(c => createNodeRow(c, depth + 1));
        }}
      }}

      nodes.forEach(n => createNodeRow(n));
    }}

    searchBox.addEventListener('input', (e) => {{
      renderTree(treeData, e.target.value);
    }});

    // Select and inspect a node
    async function selectNode(nodeId) {{
      selectedNodeId = nodeId;
      renderTree(treeData, searchBox.value);

      if (isStaticMode) {{
        const nodeDetail = (window.STATIC_DATA.details || {{}})[nodeId];
        if (nodeDetail) renderDetail(nodeDetail);
        return;
      }}

      try {{
        const res = await fetch(`/api/nodes/${{nodeId}}`);
        if (res.ok) {{
          const detail = await res.json();
          renderDetail(detail);
        }}
      }} catch (err) {{
        console.error('Failed to load node detail:', err);
      }}

      // Highlight in graph
      if (networkInstance) {{
        networkInstance.selectNodes([nodeId]);
      }}
    }}

    function renderDetail(detail) {{
      const n = detail.node;
      document.getElementById('node-id').textContent = n.id;
      document.getElementById('node-title').textContent = n.title;
      document.getElementById('node-kind-badge').textContent = n.kind;

      const stBadge = document.getElementById('node-status-badge');
      stBadge.className = `px-2 py-0.5 rounded-full text-xs font-medium border status-${{detail.virtual_status || n.status}}`;
      stBadge.textContent = detail.virtual_status || n.status;

      // Metadata Pills
      const metaContainer = document.getElementById('node-meta-pills');
      metaContainer.innerHTML = '';
      if (n.priority) {{
        metaContainer.innerHTML += `<span class="px-2 py-0.5 rounded bg-zinc-800 text-zinc-300 border border-zinc-700 font-mono">Prio: ${{n.priority}}</span>`;
      }}
      if (n.acceptable_models && n.acceptable_models.length > 0) {{
        metaContainer.innerHTML += `<span class="px-2 py-0.5 rounded bg-zinc-800 text-purple-300 border border-purple-900 font-mono">${{n.acceptable_models.join(', ')}}</span>`;
      }}
      if (n.target_repo) {{
        metaContainer.innerHTML += `<span class="px-2 py-0.5 rounded bg-zinc-800 text-cyan-300 border border-cyan-900 font-mono">${{n.target_repo}}</span>`;
      }}

      // Active Lease banner
      let leaseBanner = '';
      if (detail.lease) {{
        leaseBanner = `
          <div class="p-3 bg-blue-950/40 border border-blue-800/80 rounded-lg flex items-center justify-between text-xs">
            <div class="flex items-center gap-2">
              <span class="w-2 h-2 rounded-full bg-blue-400 animate-ping"></span>
              <span class="text-blue-300 font-semibold">Active In-Flight Lease</span>
              <span class="text-zinc-400 font-mono">Agent: ${{detail.lease.agent_id}}</span>
            </div>
            <span class="text-zinc-500 font-mono">${{detail.lease.branch_name}}</span>
          </div>
        `;
      }}

      // Verifications list
      let verificationsHtml = '';
      if (detail.verifications && detail.verifications.length > 0) {{
        verificationsHtml = `
          <div class="space-y-2 pt-4 border-t border-zinc-800">
            <h3 class="text-xs font-semibold text-zinc-400 uppercase tracking-wider">Static Verifications</h3>
            <div class="space-y-1.5">
              ${{detail.verifications.map(v => `
                <div class="flex items-center justify-between p-2 rounded bg-zinc-900 border border-zinc-800 text-xs font-mono">
                  <span class="text-emerald-400">${{v.verification_type}}</span>
                  <span class="text-zinc-300">${{v.target_path}}</span>
                </div>
              `).join('')}}
            </div>
          </div>
        `;
      }}

      // Rendered Markdown Content
      const mdHtml = marked.parse(detail.rendered_markdown || '# No content available');

      markdownContainer.innerHTML = `
        ${{leaseBanner}}
        <div class="prose prose-invert max-w-none text-sm leading-relaxed prose-headings:font-semibold prose-a:text-emerald-400 prose-pre:bg-zinc-900 prose-pre:border prose-pre:border-zinc-800">
          ${{mdHtml}}
        </div>
        ${{verificationsHtml}}
      `;
    }}

    // Render Vis Network DAG Graph
    function renderGraph(graph) {{
      const container = document.getElementById('network-canvas');
      
      const statusColors = {{
        READY: {{ background: '#065f46', border: '#10b981' }},
        IMPLEMENTING: {{ background: '#1e3a8a', border: '#3b82f6' }},
        IN_FLIGHT: {{ background: '#1e3a8a', border: '#3b82f6' }},
        WAITING_REVIEW: {{ background: '#78350f', border: '#f59e0b' }},
        WAITING_MERGE: {{ background: '#164e63', border: '#06b6d4' }},
        COMPLETED: {{ background: '#14532d', border: '#22c55e' }},
        BLOCKED: {{ background: '#374151', border: '#6b7280' }},
        NOT_STARTED: {{ background: '#1f2937', border: '#4b5563' }}
      }};

      const visNodes = graph.nodes.map(n => {{
        const colors = statusColors[n.status] || statusColors.NOT_STARTED;
        return {{
          id: n.id,
          label: `${{n.id}}\\n${{n.title}}`,
          shape: 'box',
          margin: 10,
          color: {{
            background: colors.background,
            border: colors.border,
            highlight: {{ background: colors.background, border: '#ffffff' }}
          }},
          font: {{ color: '#f3f4f6', face: 'Inter', size: 12, multi: 'md' }},
          borderWidth: 2,
          shadow: {{ enabled: true, color: 'rgba(0,0,0,0.5)', size: 4 }}
        }};
      }});

      const visEdges = graph.edges.map(e => ({{
        from: e.source,
        to: e.target,
        arrows: 'to',
        dashes: e.type === 'contains',
        color: {{ color: e.type === 'contains' ? '#4b5563' : '#10b981', highlight: '#ffffff' }},
        width: e.type === 'contains' ? 1 : 2
      }}));

      const data = {{ nodes: new vis.DataSet(visNodes), edges: new vis.DataSet(visEdges) }};
      const options = {{
        layout: {{
          hierarchical: {{
            direction: 'UD',
            sortMethod: 'directed',
            levelSeparation: 90,
            nodeSpacing: 180
          }}
        }},
        physics: false,
        interaction: {{ hover: true, selectConnectedEdges: true }}
      }};

      if (networkInstance) {{
        networkInstance.destroy();
      }}
      networkInstance = new vis.Network(container, data, options);

      networkInstance.on('click', (params) => {{
        if (params.nodes.length > 0) {{
          selectNode(params.nodes[0]);
        }}
      }});
    }}

    // WebSocket Live Updates
    if (!isStaticMode) {{
      function connectWS() {{
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${{protocol}}//${{location.host}}/ws`;
        const ws = new WebSocket(wsUrl);

        ws.onopen = () => {{
          connectionStatus.textContent = 'Live';
          connectionDot.className = 'w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse';
        }};

        ws.onmessage = (event) => {{
          try {{
            const data = JSON.parse(event.data);
            if (data.type === 'reload' || data.type === 'update') {{
              loadAllData();
              if (selectedNodeId) selectNode(selectedNodeId);
            }}
          }} catch (e) {{
            console.error('WS error:', e);
          }}
        }};

        ws.onclose = () => {{
          connectionStatus.textContent = 'Disconnected';
          connectionDot.className = 'w-1.5 h-1.5 rounded-full bg-red-400';
          setTimeout(connectWS, 3000);
        }};
      }}
      connectWS();
    }} else {{
      connectionStatus.textContent = 'Static Export';
      connectionDot.className = 'w-1.5 h-1.5 rounded-full bg-zinc-400';
    }}

    // Initial Load
    loadAllData();
  </script>
</body>
</html>
"""
