"""HTML/CSS/JS template for the TaskManager interactive web visualizer."""

import json
from typing import Any

from taskmanager.web.enums import AppIcon, StatusVisual, WebViewMode


def get_web_html(initial_data: dict[str, Any] | None = None) -> str:
    embedded_script = ""
    if initial_data is not None:
        raw_json = json.dumps(initial_data)
        embedded_script = f"<script>window.STATIC_DATA = {raw_json};</script>"

    themes_json = json.dumps(StatusVisual.all_themes_dict())
    sprite_svg = AppIcon.generate_svg_sprite()

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
  <script src="https://unpkg.com/lucide@latest"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap');
    body {{
      font-family: 'Inter', sans-serif;
    }}
    code, pre, .font-mono {{
      font-family: 'JetBrains Mono', monospace;
    }}
    ::-webkit-scrollbar {{
      width: 6px;
      height: 6px;
    }}
    ::-webkit-scrollbar-track {{
      background: rgba(0, 0, 0, 0.2);
    }}
    ::-webkit-scrollbar-thumb {{
      background: rgba(156, 163, 175, 0.25);
      border-radius: 3px;
    }}
    ::-webkit-scrollbar-thumb:hover {{
      background: rgba(156, 163, 175, 0.45);
    }}
    @keyframes pulse-highlight {{
      0% {{ box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }}
      70% {{ box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }}
      100% {{ box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }}
    }}
    .node-highlighted {{
      animation: pulse-highlight 1.5s ease-out;
      border-color: #10b981 !important;
    }}
  </style>
  <script>
    window.STATUS_THEMES = {themes_json};
    window.VIEW_MODES = {{
      DOCUMENT: '{WebViewMode.DOCUMENT.value}',
      GRAPH: '{WebViewMode.GRAPH.value}'
    }};
  </script>
  {embedded_script}
</head>
<body class="bg-zinc-950 text-zinc-100 flex flex-col h-screen overflow-hidden selection:bg-emerald-500 selection:text-black">
  {sprite_svg}

  <!-- Header -->
  <header class="h-14 border-b border-zinc-800 px-4 flex items-center justify-between bg-zinc-900/60 backdrop-blur z-20 flex-shrink-0">
    <div class="flex items-center space-x-3">
      <div class="flex items-center gap-2">
        <svg class="w-5 h-5 text-emerald-400" fill="none" stroke="currentColor"><use href="#icon-layers"/></svg>
        <span class="text-sm font-semibold tracking-wide text-zinc-100">TaskManager</span>
      </div>
      <span id="connection-pill" class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium bg-zinc-900 text-zinc-400 border border-zinc-800">
        <span id="connection-dot" class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
        <span id="connection-status">Synced</span>
      </span>
    </div>

    <!-- Complete Status Digest Chips -->
    <div id="stats-digest" class="hidden xl:flex items-center gap-1.5 text-xs overflow-x-auto py-1">
      <!-- Populated dynamically via STATUS_THEMES -->
    </div>

    <!-- View Switcher & Controls -->
    <div class="flex items-center space-x-2">
      <div class="bg-zinc-900 p-0.5 rounded-lg border border-zinc-800 flex text-xs">
        <button id="view-doc-btn" class="flex items-center gap-1.5 px-3 py-1.5 rounded-md font-medium bg-zinc-800 text-white shadow-sm transition">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor"><use href="#icon-file-text"/></svg>
          <span>Document</span>
        </button>
        <button id="view-graph-btn" class="flex items-center gap-1.5 px-3 py-1.5 rounded-md font-medium text-zinc-400 hover:text-white transition">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor"><use href="#icon-network"/></svg>
          <span>Graph</span>
        </button>
      </div>
      <button id="refresh-btn" title="Refresh state" class="p-2 rounded-lg bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-zinc-400 hover:text-white transition">
        <svg class="w-4 h-4" fill="none" stroke="currentColor"><use href="#icon-rotate-cw"/></svg>
      </button>
    </div>
  </header>

  <!-- Main View Area -->
  <div class="flex-1 flex overflow-hidden">
    
    <!-- Sidebar / Hierarchical Navigation (Always Visible) -->
    <aside id="sidebar-pane" class="w-80 border-r border-zinc-800 flex flex-col bg-zinc-900/40 flex-shrink-0">
      <div class="p-3 border-b border-zinc-800 flex items-center gap-2">
        <div class="relative flex-1">
          <div class="absolute inset-y-0 left-0 pl-2.5 flex items-center pointer-events-none text-zinc-500">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor"><use href="#icon-search"/></svg>
          </div>
          <input id="search-box" type="text" placeholder="Filter specs, plans, tasks..." class="w-full bg-zinc-950 border border-zinc-800 rounded-lg pl-8 pr-3 py-1.5 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500">
        </div>
        <button id="expand-all-btn" title="Expand all" class="p-1.5 rounded-lg bg-zinc-950 hover:bg-zinc-800 border border-zinc-800 text-zinc-400 hover:text-zinc-200 text-xs">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor"><use href="#icon-chevrons-up-down"/></svg>
        </button>
      </div>
      <div id="tree-list" class="flex-1 overflow-y-auto p-2 space-y-1">
        <!-- Dynamically Populated Tree -->
      </div>
    </aside>

    <!-- Document View Pane -->
    <main id="document-pane" class="flex-1 overflow-y-auto p-6 space-y-8 bg-zinc-950">
      <div id="unified-document" class="max-w-4xl mx-auto space-y-8 pb-16">
        <!-- Dynamically rendered unified spec document -->
      </div>
    </main>

    <!-- Graph View Pane -->
    <section id="graph-pane" class="hidden flex-1 flex flex-col bg-zinc-950/80 overflow-hidden relative">
      <div class="absolute top-3 right-3 z-10 flex gap-2">
        <button id="graph-fit-btn" class="flex items-center gap-1.5 px-2.5 py-1 text-xs rounded bg-zinc-900/90 hover:bg-zinc-800 text-zinc-300 border border-zinc-800 backdrop-blur">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor"><use href="#icon-maximize-2"/></svg>
          <span>Fit View</span>
        </button>
      </div>
      <div id="network-canvas" class="w-full h-full"></div>

      <!-- Graph Slide-over Node Inspector Drawer -->
      <div id="graph-inspector" class="hidden absolute top-0 right-0 w-96 h-full bg-zinc-900/95 border-l border-zinc-800 backdrop-blur z-20 flex flex-col shadow-2xl transition-transform duration-200">
        <div class="p-4 border-b border-zinc-800 flex items-start justify-between">
          <div class="space-y-1">
            <div class="flex items-center gap-2">
              <span id="inspector-kind" class="px-1.5 py-0.5 rounded text-[10px] font-mono uppercase bg-zinc-800 text-zinc-400">TASK</span>
              <span id="inspector-id" class="font-mono text-xs font-semibold text-emerald-400">---</span>
            </div>
            <h3 id="inspector-title" class="text-sm font-semibold text-zinc-100">---</h3>
          </div>
          <button id="inspector-close-btn" class="p-1 rounded text-zinc-400 hover:text-white hover:bg-zinc-800">
            <svg class="w-4 h-4" fill="none" stroke="currentColor"><use href="#icon-x"/></svg>
          </button>
        </div>
        <div id="inspector-body" class="flex-1 overflow-y-auto p-4 space-y-4 text-xs">
          <!-- Populated dynamically on node select -->
        </div>
      </div>
    </section>

  </div>

  <script>
    // State Management
    let treeData = [];
    let graphData = {{ nodes: [], edges: [] }};
    let statsData = {{}};
    let selectedNodeId = null;
    let activeStatusFilter = null;
    let currentMode = window.VIEW_MODES.DOCUMENT;
    let networkInstance = null;
    let isStaticMode = typeof window.STATIC_DATA !== 'undefined';
    const collapsedNodes = new Set();

    // DOM Elements
    const documentPane = document.getElementById('document-pane');
    const graphPane = document.getElementById('graph-pane');
    const unifiedDocument = document.getElementById('unified-document');
    const treeList = document.getElementById('tree-list');
    const searchBox = document.getElementById('search-box');
    const statsDigest = document.getElementById('stats-digest');
    const viewDocBtn = document.getElementById('view-doc-btn');
    const viewGraphBtn = document.getElementById('view-graph-btn');
    const graphFitBtn = document.getElementById('graph-fit-btn');
    const refreshBtn = document.getElementById('refresh-btn');
    const expandAllBtn = document.getElementById('expand-all-btn');
    const graphInspector = document.getElementById('graph-inspector');
    const inspectorCloseBtn = document.getElementById('inspector-close-btn');
    const connectionStatus = document.getElementById('connection-status');
    const connectionDot = document.getElementById('connection-dot');

    function renderIcon(iconName, classes = 'w-4 h-4') {{
      return `<svg class="${{classes}}" fill="none" stroke="currentColor"><use href="#icon-${{iconName}}"></use></svg>`;
    }}

    function getTheme(status) {{
      return window.STATUS_THEMES[status] || window.STATUS_THEMES.NOT_STARTED;
    }}

    // Mode Switching
    function setViewMode(mode) {{
      currentMode = mode;
      if (mode === window.VIEW_MODES.DOCUMENT) {{
        viewDocBtn.className = 'flex items-center gap-1.5 px-3 py-1.5 rounded-md font-medium bg-zinc-800 text-white shadow-sm transition';
        viewGraphBtn.className = 'flex items-center gap-1.5 px-3 py-1.5 rounded-md font-medium text-zinc-400 hover:text-white transition';
        documentPane.classList.remove('hidden');
        graphPane.classList.add('hidden');
      }} else {{
        viewGraphBtn.className = 'flex items-center gap-1.5 px-3 py-1.5 rounded-md font-medium bg-zinc-800 text-white shadow-sm transition';
        viewDocBtn.className = 'flex items-center gap-1.5 px-3 py-1.5 rounded-md font-medium text-zinc-400 hover:text-white transition';
        documentPane.classList.add('hidden');
        graphPane.classList.remove('hidden');
        if (networkInstance) {{
          setTimeout(() => networkInstance.fit(), 50);
        }}
      }}
    }}

    viewDocBtn.addEventListener('click', () => setViewMode(window.VIEW_MODES.DOCUMENT));
    viewGraphBtn.addEventListener('click', () => setViewMode(window.VIEW_MODES.GRAPH));
    graphFitBtn.addEventListener('click', () => networkInstance && networkInstance.fit());
    inspectorCloseBtn.addEventListener('click', () => graphInspector.classList.add('hidden'));
    refreshBtn.addEventListener('click', loadAllData);

    // Expand / Collapse All
    expandAllBtn.addEventListener('click', () => {{
      if (collapsedNodes.size > 0) {{
        collapsedNodes.clear();
      }} else {{
        function collect(node) {{
          if (node.children && node.children.length > 0) {{
            collapsedNodes.add(node.id);
            node.children.forEach(collect);
          }}
        }}
        treeData.forEach(collect);
      }}
      renderTree(treeData, searchBox.value);
      renderUnifiedDocument();
    }});

    // Status Digest Bar
    function updateStatsDigest(stats) {{
      statsData = stats;
      statsDigest.innerHTML = '';

      // Total Chip
      const totalChip = document.createElement('button');
      totalChip.className = `flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-xs transition ${{activeStatusFilter === null ? 'bg-zinc-800 text-white border-zinc-700' : 'bg-zinc-900/60 text-zinc-400 border-zinc-800 hover:bg-zinc-850'}}`;
      totalChip.innerHTML = `<span>Total:</span> <strong class="text-white">${{stats.total || 0}}</strong>`;
      totalChip.onclick = () => {{
        activeStatusFilter = null;
        updateStatsDigest(statsData);
        renderTree(treeData, searchBox.value);
        renderUnifiedDocument();
      }};
      statsDigest.appendChild(totalChip);

      // Render chip for all non-zero or key statuses
      const displayStatuses = ['READY', 'IN_FLIGHT', 'WAITING_REVIEW', 'WAITING_FIXES', 'WAITING_MERGE', 'COMPLETED', 'BLOCKED', 'NOT_STARTED'];
      displayStatuses.forEach(code => {{
        const theme = window.STATUS_THEMES[code];
        if (!theme) return;
        const count = stats[code] || 0;
        if (count === 0 && code !== 'READY' && code !== 'COMPLETED' && code !== 'BLOCKED') return;

        const chip = document.createElement('button');
        const isActive = activeStatusFilter === code;
        chip.className = `flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-xs transition ${{isActive ? theme.badge_class + ' ring-1 ring-white/20' : 'bg-zinc-900/60 text-zinc-400 border-zinc-800 hover:bg-zinc-850'}}`;
        chip.innerHTML = `
          ${{renderIcon(theme.icon, 'w-3.5 h-3.5')}}
          <span>${{theme.label}}</span>
          <strong class="${{isActive ? 'text-white' : theme.text_class}}">${{count}}</strong>
        `;
        chip.onclick = () => {{
          activeStatusFilter = isActive ? null : code;
          updateStatsDigest(statsData);
          renderTree(treeData, searchBox.value);
          renderUnifiedDocument();
        }};
        statsDigest.appendChild(chip);
      }});
    }}

    // Sidebar Tree Rendering
    function renderTree(nodes, filterText = '') {{
      treeList.innerHTML = '';
      const filter = filterText.toLowerCase();

      function isNodeVisible(node) {{
        const matchesFilter = !filter || node.title.toLowerCase().includes(filter) || node.id.toLowerCase().includes(filter);
        const effectiveStatus = node.virtual_status || node.status;
        const matchesStatus = !activeStatusFilter || effectiveStatus === activeStatusFilter;
        if (matchesFilter && matchesStatus) return true;
        if (node.children) {{
          return node.children.some(c => isNodeVisible(c));
        }}
        return false;
      }}

      function createNodeRow(node, depth = 0) {{
        if (!isNodeVisible(node)) return;

        const effectiveStatus = node.virtual_status || node.status;
        const theme = getTheme(effectiveStatus);
        const hasChildren = node.children && node.children.length > 0;
        const isCollapsed = collapsedNodes.has(node.id);

        const row = document.createElement('div');
        row.className = `flex items-center justify-between px-2.5 py-1.5 rounded-lg cursor-pointer text-xs group transition ${{selectedNodeId === node.id ? 'bg-zinc-800 text-white font-medium border border-zinc-700' : 'text-zinc-400 hover:bg-zinc-850 hover:text-zinc-200'}}`;
        row.style.paddingLeft = `${{depth * 14 + 8}}px`;

        let chevron = `<span class="w-3.5 h-3.5 inline-block"></span>`;
        if (hasChildren) {{
          chevron = `<button class="p-0.5 hover:text-white toggle-btn">${{renderIcon(isCollapsed ? 'chevron-right' : 'chevron-down', 'w-3.5 h-3.5')}}</button>`;
        }}

        let progressHtml = '';
        if (node.kind === 'plan' && hasChildren) {{
          const doneCount = node.children.filter(c => c.status === 'COMPLETED' || c.status === 'SUPERSEDED').length;
          progressHtml = `<span class="text-[10px] font-mono text-zinc-500 mr-1">${{doneCount}}/${{node.children.length}}</span>`;
        }}

        row.innerHTML = `
          <div class="flex items-center gap-1.5 truncate">
            ${{chevron}}
            <span class="${{theme.text_class}} flex-shrink-0">${{renderIcon(theme.icon, 'w-3.5 h-3.5')}}</span>
            <span class="font-mono text-[10px] text-zinc-500 uppercase">${{node.id}}</span>
            <span class="truncate">${{node.title}}</span>
          </div>
          <div class="flex items-center gap-1 flex-shrink-0">
            ${{progressHtml}}
            <span class="px-1.5 py-0.5 rounded text-[10px] font-mono border ${{theme.badge_class}}">${{theme.label}}</span>
          </div>
        `;

        const toggleBtn = row.querySelector('.toggle-btn');
        if (toggleBtn) {{
          toggleBtn.onclick = (e) => {{
            e.stopPropagation();
            if (isCollapsed) collapsedNodes.delete(node.id);
            else collapsedNodes.add(node.id);
            renderTree(treeData, searchBox.value);
            renderUnifiedDocument();
          }};
        }}

        row.onclick = () => selectNode(node.id);
        treeList.appendChild(row);

        if (hasChildren && !isCollapsed) {{
          node.children.forEach(c => createNodeRow(c, depth + 1));
        }}
      }}

      nodes.forEach(n => createNodeRow(n));
    }}

    searchBox.addEventListener('input', (e) => {{
      renderTree(treeData, e.target.value);
      renderUnifiedDocument();
    }});

    // Select Node Action (Coordinates Tree, Document, and Graph)
    function selectNode(nodeId) {{
      selectedNodeId = nodeId;
      renderTree(treeData, searchBox.value);

      if (currentMode === window.VIEW_MODES.DOCUMENT) {{
        const targetEl = document.getElementById(`doc-node-${{nodeId}}`);
        if (targetEl) {{
          // If inside a collapsed plan, expand it
          collapsedNodes.delete(nodeId);
          renderUnifiedDocument();
          const refreshedEl = document.getElementById(`doc-node-${{nodeId}}`);
          if (refreshedEl) {{
            refreshedEl.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
            refreshedEl.classList.add('node-highlighted');
            setTimeout(() => refreshedEl.classList.remove('node-highlighted'), 1600);
          }}
        }}
      }} else if (currentMode === window.VIEW_MODES.GRAPH) {{
        if (networkInstance) {{
          networkInstance.selectNodes([nodeId]);
          networkInstance.focus(nodeId, {{ scale: 1.1, animation: true }});
        }}
        showGraphInspector(nodeId);
      }}
    }}

    // Render Unified Document View
    function renderUnifiedDocument() {{
      unifiedDocument.innerHTML = '';

      if (treeData.length === 0) {{
        unifiedDocument.innerHTML = '<div class="text-zinc-500 text-sm italic py-12 text-center">No specifications or tasks registered. Use CLI to add items.</div>';
        return;
      }}

      function renderSectionMarkdown(content) {{
        if (!content) return '';
        return marked.parse(content);
      }}

      treeData.forEach(spec => {{
        const specStatus = spec.virtual_status || spec.status;
        const specTheme = getTheme(specStatus);

        const specCard = document.createElement('article');
        specCard.id = `doc-node-${{spec.id}}`;
        specCard.className = 'space-y-6 transition duration-200';

        // Spec Header Banner
        let specPills = '';
        if (spec.priority) {{
          specPills += `<span class="px-2 py-0.5 rounded bg-zinc-900 border border-zinc-800 text-zinc-300 font-mono text-xs">Prio: ${{spec.priority}}</span>`;
        }}
        if (spec.target_repo) {{
          specPills += `<span class="px-2 py-0.5 rounded bg-zinc-900 border border-zinc-800 text-cyan-400 font-mono text-xs">${{spec.target_repo}}</span>`;
        }}

        let specSectionsHtml = '';
        if (spec.sections && spec.sections.length > 0) {{
          specSectionsHtml = `
            <div class="space-y-4 pt-2">
              ${{spec.sections.map(sec => `
                <section class="bg-zinc-900/30 border border-zinc-800/80 rounded-xl p-5 space-y-2">
                  <h3 class="text-sm font-semibold text-zinc-200 flex items-center gap-2">
                    ${{sec.header || sec.key}}
                  </h3>
                  <div class="prose prose-invert max-w-none text-xs leading-relaxed text-zinc-300 prose-headings:font-semibold prose-a:text-emerald-400 prose-pre:bg-zinc-950 prose-pre:border prose-pre:border-zinc-800">
                    ${{renderSectionMarkdown(sec.content)}}
                  </div>
                </section>
              `).join('')}}
            </div>
          `;
        }}

        // Render Plans
        let plansHtml = '';
        if (spec.children && spec.children.length > 0) {{
          plansHtml = `
            <div class="space-y-6 pt-4">
              <h2 class="text-xs font-semibold text-zinc-400 uppercase tracking-wider flex items-center gap-2">
                ${{renderIcon('layers', 'w-4 h-4 text-emerald-400')}}
                <span>Implementation Plans (${{spec.children.length}})</span>
              </h2>
              <div class="space-y-4">
                ${{spec.children.map(plan => renderPlanCard(plan)).join('')}}
              </div>
            </div>
          `;
        }}

        specCard.innerHTML = `
          <div class="border-b border-zinc-800 pb-6 space-y-3">
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="px-2 py-0.5 rounded text-[11px] font-mono uppercase bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">${{spec.kind}}</span>
                <span class="font-mono text-xs font-semibold text-zinc-400">${{spec.id}}</span>
                <span class="px-2 py-0.5 rounded-full text-xs font-medium border flex items-center gap-1.5 ${{specTheme.badge_class}}">
                  ${{renderIcon(specTheme.icon, 'w-3 h-3')}}
                  <span>${{specTheme.label}}</span>
                </span>
              </div>
              <div class="flex items-center gap-2">
                ${{specPills}}
              </div>
            </div>
            <h1 class="text-2xl font-bold tracking-tight text-white">${{spec.title}}</h1>
          </div>
          ${{specSectionsHtml}}
          ${{plansHtml}}
        `;

        unifiedDocument.appendChild(specCard);
      }});

      attachCollapsibleHandlers();
    }}

    function renderPlanCard(plan) {{
      const planStatus = plan.virtual_status || plan.status;
      const theme = getTheme(planStatus);
      const isCollapsed = collapsedNodes.has(plan.id);

      const tasks = plan.children || [];
      const completedCount = tasks.filter(t => t.status === 'COMPLETED' || t.status === 'SUPERSEDED').length;
      const percent = tasks.length > 0 ? Math.round((completedCount / tasks.length) * 100) : 0;

      // Filter tasks by active status filter if set
      const visibleTasks = activeStatusFilter 
        ? tasks.filter(t => (t.virtual_status || t.status) === activeStatusFilter)
        : tasks;

      let planSectionsHtml = '';
      if (plan.sections && plan.sections.length > 0) {{
        planSectionsHtml = `
          <div class="space-y-3 pt-2">
            ${{plan.sections.map(s => `
              <div class="p-3 bg-zinc-950/60 rounded-lg border border-zinc-800 text-xs">
                <div class="font-semibold text-zinc-300 mb-1">${{s.header || s.key}}</div>
                <div class="prose prose-invert max-w-none text-xs text-zinc-400">
                  ${{marked.parse(s.content)}}
                </div>
              </div>
            `).join('')}}
          </div>
        `;
      }}

      return `
        <div id="doc-node-${{plan.id}}" class="border border-zinc-800 rounded-xl bg-zinc-900/30 overflow-hidden transition">
          <!-- Plan Header -->
          <div class="p-4 bg-zinc-900/70 border-b border-zinc-800 flex items-center justify-between cursor-pointer plan-header" data-node-id="${{plan.id}}">
            <div class="flex items-center gap-2.5 truncate">
              <button class="text-zinc-400 hover:text-white">${{renderIcon(isCollapsed ? 'chevron-right' : 'chevron-down', 'w-4 h-4')}}</button>
              <span class="font-mono text-xs font-semibold text-emerald-400">${{plan.id}}</span>
              <span class="text-sm font-semibold text-zinc-200 truncate">${{plan.title}}</span>
              <span class="px-2 py-0.5 rounded-full text-[11px] font-medium border flex items-center gap-1 ${{theme.badge_class}}">
                ${{renderIcon(theme.icon, 'w-3 h-3')}}
                <span>${{theme.label}}</span>
              </span>
            </div>
            <div class="flex items-center gap-3 flex-shrink-0">
              <div class="flex items-center gap-2">
                <div class="w-20 bg-zinc-800 rounded-full h-1.5 overflow-hidden">
                  <div class="bg-emerald-500 h-full rounded-full" style="width: ${{percent}}%"></div>
                </div>
                <span class="font-mono text-xs text-zinc-400">${{completedCount}}/${{tasks.length}}</span>
              </div>
            </div>
          </div>

          <!-- Plan Body -->
          <div class="plan-body ${{isCollapsed ? 'hidden' : ''}} p-4 space-y-4">
            ${{planSectionsHtml}}
            <div class="space-y-3">
              <div class="text-[11px] font-semibold uppercase tracking-wider text-zinc-500 flex items-center gap-2">
                ${{renderIcon('check', 'w-3.5 h-3.5')}}
                <span>Tasks (${{visibleTasks.length}})</span>
              </div>
              <div class="space-y-2.5">
                ${{visibleTasks.map(task => renderTaskCard(task)).join('')}}
              </div>
            </div>
          </div>
        </div>
      `;
    }}

    function renderTaskCard(task) {{
      const taskStatus = task.virtual_status || task.status;
      const theme = getTheme(taskStatus);
      const isCollapsed = collapsedNodes.has(task.id);

      // Model pills
      let modelPills = '';
      if (task.acceptable_models && task.acceptable_models.length > 0) {{
        modelPills = task.acceptable_models.map(m => 
          `<span class="px-1.5 py-0.5 rounded bg-purple-950/60 text-purple-300 border border-purple-800/80 font-mono text-[10px]">${{m}}</span>`
        ).join('');
      }}

      // Lease banner
      let leaseBanner = '';
      if (task.lease) {{
        leaseBanner = `
          <div class="p-2.5 bg-blue-950/40 border border-blue-800/80 rounded-lg flex items-center justify-between text-xs mb-3">
            <div class="flex items-center gap-2">
              <span class="text-blue-400">${{renderIcon('flame', 'w-3.5 h-3.5')}}</span>
              <span class="text-blue-300 font-semibold">Active In-Flight Lease:</span>
              <span class="text-zinc-300 font-mono">${{task.lease.agent_id}}</span>
            </div>
            <span class="text-zinc-400 font-mono text-[11px]">${{task.lease.branch_name}}</span>
          </div>
        `;
      }}

      // Verifications Table
      let verificationsHtml = '';
      if (task.verifications && task.verifications.length > 0) {{
        verificationsHtml = `
          <div class="space-y-1.5 pt-2 border-t border-zinc-800/60 mb-3">
            <div class="text-[10px] font-semibold text-zinc-400 uppercase tracking-wider flex items-center gap-1.5">
              ${{renderIcon('shield-check', 'w-3.5 h-3.5 text-emerald-400')}}
              <span>Verifications</span>
            </div>
            <div class="rounded-lg border border-zinc-800 overflow-hidden divide-y divide-zinc-800 text-[11px] font-mono">
              ${{task.verifications.map(v => `
                <div class="p-2 bg-zinc-950/60 flex items-center justify-between">
                  <span class="text-emerald-400">${{v.type}}</span>
                  <span class="text-zinc-300">${{v.target}}</span>
                </div>
              `).join('')}}
            </div>
          </div>
        `;
      }}

      // Task Sections
      let sectionsHtml = '';
      if (task.sections && task.sections.length > 0) {{
        sectionsHtml = task.sections.map(s => `
          <div class="space-y-1 pt-2">
            <div class="font-semibold text-zinc-300 text-xs">${{s.header || s.key}}</div>
            <div class="prose prose-invert max-w-none text-xs text-zinc-400">
              ${{marked.parse(s.content)}}
            </div>
          </div>
        `).join('');
      }}

      return `
        <div id="doc-node-${{task.id}}" class="border border-zinc-800/80 rounded-lg bg-zinc-950/40 overflow-hidden hover:border-zinc-700 transition">
          <!-- Task Header -->
          <div class="p-3 flex items-center justify-between cursor-pointer task-header bg-zinc-900/30 hover:bg-zinc-900/60" data-node-id="${{task.id}}">
            <div class="flex items-center gap-2 truncate">
              <button class="text-zinc-500 hover:text-white">${{renderIcon(isCollapsed ? 'chevron-right' : 'chevron-down', 'w-3.5 h-3.5')}}</button>
              <span class="${{theme.text_class}} flex-shrink-0">${{renderIcon(theme.icon, 'w-3.5 h-3.5')}}</span>
              <span class="font-mono text-xs font-semibold text-emerald-400 flex-shrink-0">${{task.id}}</span>
              <span class="text-xs font-medium text-zinc-200 truncate">${{task.title}}</span>
            </div>
            <div class="flex items-center gap-2 flex-shrink-0">
              ${{modelPills}}
              <span class="px-1.5 py-0.5 rounded bg-zinc-900 border border-zinc-800 text-zinc-400 font-mono text-[10px]">P${{task.priority || 50}}</span>
              <span class="px-2 py-0.5 rounded-full text-[10px] font-mono border ${{theme.badge_class}}">${{theme.label}}</span>
            </div>
          </div>

          <!-- Task Body -->
          <div class="task-body ${{isCollapsed ? 'hidden' : ''}} p-3.5 bg-zinc-950/80 border-t border-zinc-800/60 space-y-2">
            ${{leaseBanner}}
            ${{verificationsHtml}}
            ${{sectionsHtml}}
          </div>
        </div>
      `;
    }}

    function attachCollapsibleHandlers() {{
      document.querySelectorAll('.plan-header').forEach(header => {{
        header.onclick = () => {{
          const id = header.getAttribute('data-node-id');
          if (collapsedNodes.has(id)) collapsedNodes.delete(id);
          else collapsedNodes.add(id);
          renderUnifiedDocument();
          renderTree(treeData, searchBox.value);
        }};
      }});

      document.querySelectorAll('.task-header').forEach(header => {{
        header.onclick = () => {{
          const id = header.getAttribute('data-node-id');
          if (collapsedNodes.has(id)) collapsedNodes.delete(id);
          else collapsedNodes.add(id);
          renderUnifiedDocument();
        }};
      }});
    }}

    // Vis Network DAG Graph
    function renderGraph(graph) {{
      const container = document.getElementById('network-canvas');

      const visNodes = graph.nodes.map(n => {{
        const theme = getTheme(n.status);
        return {{
          id: n.id,
          label: `${{n.id}}\\n${{n.title}}`,
          shape: 'box',
          margin: 10,
          color: {{
            background: theme.graph_bg,
            border: theme.graph_border,
            highlight: {{ background: theme.graph_bg, border: '#ffffff' }}
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
          showGraphInspector(params.nodes[0]);
        }}
      }});
    }}

    // Slide-over Node Inspector for Graph
    async function showGraphInspector(nodeId) {{
      selectedNodeId = nodeId;
      graphInspector.classList.remove('hidden');

      let detail = null;
      if (isStaticMode) {{
        detail = (window.STATIC_DATA.details || {{}})[nodeId];
      }} else {{
        try {{
          const res = await fetch(`/api/nodes/${{nodeId}}`);
          if (res.ok) detail = await res.json();
        }} catch (e) {{
          console.error('Failed to load node detail:', e);
        }}
      }}

      if (!detail) return;

      const n = detail.node;
      const theme = getTheme(detail.virtual_status || n.status);

      document.getElementById('inspector-kind').textContent = n.kind;
      document.getElementById('inspector-id').textContent = n.id;
      document.getElementById('inspector-title').textContent = n.title;

      const body = document.getElementById('inspector-body');
      let leaseBanner = '';
      if (detail.lease) {{
        leaseBanner = `
          <div class="p-2.5 bg-blue-950/40 border border-blue-800/80 rounded-lg text-xs space-y-1">
            <div class="text-blue-300 font-semibold flex items-center gap-1.5">
              ${{renderIcon('flame', 'w-3.5 h-3.5')}}
              <span>In-Flight Active Lease</span>
            </div>
            <div class="text-zinc-400 font-mono">Agent: ${{detail.lease.agent_id}}</div>
            <div class="text-zinc-500 font-mono text-[11px]">${{detail.lease.branch_name}}</div>
          </div>
        `;
      }}

      let verificationsHtml = '';
      if (detail.verifications && detail.verifications.length > 0) {{
        verificationsHtml = `
          <div class="space-y-1.5 pt-2">
            <div class="font-semibold text-zinc-400 uppercase tracking-wider text-[10px]">Verifications</div>
            <div class="divide-y divide-zinc-800 rounded border border-zinc-800 font-mono text-[11px]">
              ${{detail.verifications.map(v => `
                <div class="p-2 bg-zinc-950/60 flex items-center justify-between">
                  <span class="text-emerald-400">${{v.verification_type}}</span>
                  <span class="text-zinc-300">${{v.target_path}}</span>
                </div>
              `).join('')}}
            </div>
          </div>
        `;
      }}

      let sectionsHtml = '';
      if (detail.sections && detail.sections.length > 0) {{
        sectionsHtml = detail.sections.map(s => `
          <div class="space-y-1 pt-2">
            <div class="font-semibold text-zinc-300 text-xs">${{s.header || s.key}}</div>
            <div class="prose prose-invert max-w-none text-xs text-zinc-400">
              ${{marked.parse(s.content)}}
            </div>
          </div>
        `).join('');
      }}

      body.innerHTML = `
        <div class="flex items-center gap-2">
          <span class="px-2 py-0.5 rounded-full text-xs font-medium border flex items-center gap-1 ${{theme.badge_class}}">
            ${{renderIcon(theme.icon, 'w-3 h-3')}}
            <span>${{theme.label}}</span>
          </span>
          <span class="px-2 py-0.5 rounded bg-zinc-950 border border-zinc-800 font-mono text-zinc-400 text-xs">Prio: ${{n.priority || 50}}</span>
        </div>
        ${{leaseBanner}}
        ${{verificationsHtml}}
        ${{sectionsHtml}}
      `;
    }}

    // Fetch and Load Data
    async function loadAllData() {{
      if (isStaticMode) {{
        treeData = window.STATIC_DATA.tree || [];
        graphData = window.STATIC_DATA.graph || {{ nodes: [], edges: [] }};
        statsData = window.STATIC_DATA.stats || {{}};
        updateStatsDigest(statsData);
        renderTree(treeData);
        renderUnifiedDocument();
        renderGraph(graphData);
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
        statsData = await statsRes.json();

        updateStatsDigest(statsData);
        renderTree(treeData);
        renderUnifiedDocument();
        renderGraph(graphData);
      }} catch (err) {{
        console.error('Failed to load data:', err);
      }}
    }}

    // WebSocket Live Updates
    if (!isStaticMode) {{
      function connectWS() {{
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${{protocol}}//${{location.host}}/ws`;
        const ws = new WebSocket(wsUrl);

        ws.onopen = () => {{
          connectionStatus.textContent = 'Synced';
          connectionDot.className = 'w-1.5 h-1.5 rounded-full bg-emerald-400';
        }};

        ws.onmessage = (event) => {{
          try {{
            const data = JSON.parse(event.data);
            if (data.type === 'reload' || data.type === 'update') {{
              loadAllData();
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
      connectionDot.className = 'w-1.5 h-1.5 rounded-full bg-zinc-500';
    }}

    // Initialize
    loadAllData();
  </script>
</body>
</html>
"""
