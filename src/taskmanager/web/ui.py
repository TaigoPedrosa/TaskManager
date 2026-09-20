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
    groups_json = json.dumps(StatusVisual.groups_list())
    status_css = StatusVisual.css()
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
    {status_css}
    .st-chip {{ color: var(--st-fg); background: var(--st-bg); border: 1px solid color-mix(in srgb, var(--st-fg) 40%, transparent); }}
    .st-text {{ color: var(--st-fg); }}
    .st-seg {{ background: var(--st-fg); height: 100%; }}
    .st-dot {{ background: var(--st-fg); }}
    details > summary {{ list-style: none; }}
    details > summary::-webkit-details-marker {{ display: none; }}
    details > summary .details-caret {{ transition: transform 0.15s ease; }}
    details[open] > summary .details-caret {{ transform: rotate(90deg); }}
  </style>
  <script>
    window.STATUS_THEMES = {themes_json};
    window.STATUS_GROUPS = {groups_json};
    window.VIEW_MODES = {{
      DOCUMENT: '{WebViewMode.DOCUMENT.value}',
      GRAPH: '{WebViewMode.GRAPH.value}'
    }};
  </script>
  {embedded_script}
</head>
<body class="bg-zinc-950 text-zinc-100 flex flex-col h-screen overflow-hidden selection:bg-emerald-500 selection:text-black">
  {sprite_svg}

  <!-- Toolbar: brand, view switcher, search and status counts in one dense bar -->
  <header id="toolbar" class="border-b border-zinc-800 px-4 py-2 bg-zinc-900/60 backdrop-blur z-20 flex-shrink-0 space-y-2">
    <div class="flex items-center gap-3 flex-wrap lg:flex-nowrap">
      <div class="flex items-center gap-2 flex-shrink-0">
        <svg class="w-5 h-5 text-emerald-400" fill="none" stroke="currentColor"><use href="#icon-layers"/></svg>
        <span class="text-sm font-semibold tracking-wide text-zinc-100 hidden sm:inline">TaskManager</span>
        <span id="connection-pill" title="Synced" class="inline-flex items-center px-1.5 py-1.5 rounded-full bg-zinc-900 border border-zinc-800">
          <span id="connection-dot" class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
          <span id="connection-status" class="sr-only">Synced</span>
        </span>
      </div>

      <div class="bg-zinc-900 p-0.5 rounded-lg border border-zinc-800 flex text-xs flex-shrink-0">
        <button id="view-doc-btn" class="flex items-center gap-1.5 px-3 py-1.5 rounded-md font-medium bg-zinc-800 text-white shadow-sm transition">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor"><use href="#icon-file-text"/></svg>
          <span>Document</span>
        </button>
        <button id="view-graph-btn" class="flex items-center gap-1.5 px-3 py-1.5 rounded-md font-medium text-zinc-400 hover:text-white transition">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor"><use href="#icon-network"/></svg>
          <span>Graph</span>
        </button>
      </div>

      <div class="relative flex-1 min-w-[160px] max-w-sm">
        <div class="absolute inset-y-0 left-0 pl-2.5 flex items-center pointer-events-none text-zinc-500">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor"><use href="#icon-search"/></svg>
        </div>
        <input id="search-box" type="text" placeholder="Filter specs, plans, tasks..." class="w-full bg-zinc-950 border border-zinc-800 rounded-lg pl-8 pr-3 py-1.5 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500">
      </div>

      <div id="stats-digest" class="flex items-center gap-1 flex-wrap lg:flex-nowrap text-xs lg:ml-auto"></div>

      <div class="flex items-center gap-1.5 flex-shrink-0">
        <button id="toggle-sections-btn" title="Expand all sections" aria-label="Expand all sections" class="flex items-center gap-1.5 p-2 rounded-lg bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-zinc-400 hover:text-white transition">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor"><use href="#icon-chevrons-up-down"/></svg>
        </button>
        <button id="legend-btn" aria-expanded="false" aria-controls="legend-panel" title="Legend" aria-label="Legend" class="flex items-center gap-1.5 p-2 rounded-lg bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-xs text-zinc-300 hover:text-white transition">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor"><use href="#icon-info"/></svg>
        </button>
        <button id="refresh-btn" title="Refresh state" aria-label="Refresh state" class="p-2 rounded-lg bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-zinc-400 hover:text-white transition">
          <svg class="w-4 h-4" fill="none" stroke="currentColor"><use href="#icon-rotate-cw"/></svg>
        </button>
      </div>
    </div>

    <div class="flex flex-wrap items-center gap-2 text-xs">
      <select id="repo-filter" aria-label="Filter by target repo" class="bg-zinc-950 border border-zinc-800 rounded-lg px-2 py-1 text-xs text-zinc-200 focus:outline-none focus:border-emerald-500"></select>
      <select id="model-filter" aria-label="Filter by model" class="bg-zinc-950 border border-zinc-800 rounded-lg px-2 py-1 text-xs text-zinc-200 focus:outline-none focus:border-emerald-500"></select>
      <div id="active-filters" class="flex flex-wrap items-center gap-1.5"></div>
    </div>
  </header>

  <!-- Status legend -->
  <div id="legend-panel" role="dialog" aria-label="Status legend" class="hidden fixed top-16 right-4 w-[26rem] max-w-[calc(100vw-2rem)] max-h-[75vh] overflow-y-auto z-40 rounded-xl border border-zinc-700 bg-zinc-900 shadow-2xl p-4 space-y-2">
    <div class="flex items-start justify-between gap-3">
      <p class="text-xs text-zinc-300 leading-relaxed"><strong class="text-white">Done means Completed and nothing else.</strong> Superseded, Abandoned and Deferred work is set aside: progress bars show it as its own segment and never count it as completed. A Superseded task still unblocks its dependents.</p>
      <button id="legend-close-btn" aria-label="Close legend" class="p-1 rounded text-zinc-400 hover:text-white hover:bg-zinc-800 flex-shrink-0">
        <svg class="w-4 h-4" fill="none" stroke="currentColor"><use href="#icon-x"/></svg>
      </button>
    </div>
    <div id="legend-body"></div>
  </div>

  <!-- Main View Area -->
  <div class="flex-1 flex overflow-hidden">
    
    <!-- Sidebar / Hierarchical Navigation (Graph view only) -->
    <aside id="sidebar-pane" class="hidden relative border-r border-zinc-800 flex flex-col bg-zinc-900/40 flex-shrink-0" style="width:320px">
      <div class="p-3 border-b border-zinc-800 flex items-center justify-between gap-2">
        <span class="text-[11px] font-semibold uppercase tracking-wider text-zinc-400">Nodes</span>
        <button id="expand-all-btn" title="Expand all" aria-label="Expand all tree nodes" class="p-1.5 rounded-lg bg-zinc-950 hover:bg-zinc-800 border border-zinc-800 text-zinc-400 hover:text-zinc-200 text-xs">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor"><use href="#icon-chevrons-up-down"/></svg>
        </button>
      </div>
      <div id="tree-list" class="flex-1 overflow-y-auto p-2 space-y-1">
        <!-- Dynamically Populated Tree -->
      </div>
      <div id="sidebar-resize-handle" title="Drag to resize" class="absolute top-0 right-0 w-1.5 h-full cursor-col-resize hover:bg-emerald-500/40"></div>
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
    let visNodesDS = null;
    let currentMode = window.VIEW_MODES.DOCUMENT;
    let networkInstance = null;
    let isStaticMode = typeof window.STATIC_DATA !== 'undefined';
    const collapsedNodes = new Set();
    const expandedSections = new Set();
    let allSectionIds = [];

    // DOM Elements
    const documentPane = document.getElementById('document-pane');
    const graphPane = document.getElementById('graph-pane');
    const sidebarPane = document.getElementById('sidebar-pane');
    const unifiedDocument = document.getElementById('unified-document');
    const treeList = document.getElementById('tree-list');
    const searchBox = document.getElementById('search-box');
    const statsDigest = document.getElementById('stats-digest');
    const viewDocBtn = document.getElementById('view-doc-btn');
    const viewGraphBtn = document.getElementById('view-graph-btn');
    const graphFitBtn = document.getElementById('graph-fit-btn');
    const refreshBtn = document.getElementById('refresh-btn');
    const expandAllBtn = document.getElementById('expand-all-btn');
    const toggleSectionsBtn = document.getElementById('toggle-sections-btn');
    const sidebarResizeHandle = document.getElementById('sidebar-resize-handle');
    const graphInspector = document.getElementById('graph-inspector');
    const inspectorCloseBtn = document.getElementById('inspector-close-btn');
    const connectionPill = document.getElementById('connection-pill');
    const connectionStatus = document.getElementById('connection-status');
    const connectionDot = document.getElementById('connection-dot');
    const repoFilter = document.getElementById('repo-filter');
    const modelFilter = document.getElementById('model-filter');
    const activeFilters = document.getElementById('active-filters');
    const legendBtn = document.getElementById('legend-btn');
    const legendPanel = document.getElementById('legend-panel');
    const legendBody = document.getElementById('legend-body');
    const legendCloseBtn = document.getElementById('legend-close-btn');

    // Sidebar width: Graph view only, drag-resizable, remembered per browser.
    const SIDEBAR_MIN_WIDTH = 200;
    const SIDEBAR_MAX_WIDTH = 560;
    const SIDEBAR_DEFAULT_WIDTH = 320;

    function loadSidebarWidth() {{
      try {{
        const saved = Number(localStorage.getItem('tm-sidebar-width'));
        if (saved >= SIDEBAR_MIN_WIDTH && saved <= SIDEBAR_MAX_WIDTH) return saved;
      }} catch (e) {{
        console.error('Could not read the saved sidebar width:', e);
      }}
      return SIDEBAR_DEFAULT_WIDTH;
    }}

    function setSidebarWidth(px) {{
      sidebarPane.style.width = `${{px}}px`;
    }}

    setSidebarWidth(loadSidebarWidth());

    sidebarResizeHandle.addEventListener('mousedown', (e) => {{
      e.preventDefault();
      const startX = e.clientX;
      const startWidth = sidebarPane.getBoundingClientRect().width;

      function onMove(moveEvent) {{
        const next = Math.min(
          SIDEBAR_MAX_WIDTH,
          Math.max(SIDEBAR_MIN_WIDTH, startWidth + (moveEvent.clientX - startX))
        );
        setSidebarWidth(next);
      }}

      function onUp() {{
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        try {{
          localStorage.setItem('tm-sidebar-width', String(sidebarPane.getBoundingClientRect().width));
        }} catch (e) {{
          console.error('Could not persist the sidebar width:', e);
        }}
      }}

      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    }});

    function renderIcon(iconName, classes = 'w-4 h-4') {{
      return `<svg class="${{classes}}" fill="none" stroke="currentColor"><use href="#icon-${{iconName}}"></use></svg>`;
    }}

    function getTheme(status) {{
      return window.STATUS_THEMES[status] || {{ ...window.STATUS_THEMES.NOT_STARTED, label: String(status) }};
    }}

    // Mode Switching. The sidebar is a graph-view tool for jumping to a node; it takes
    // no space in Document view so the document pane reads at its own full width.
    function setViewMode(mode) {{
      currentMode = mode;
      if (mode === window.VIEW_MODES.DOCUMENT) {{
        viewDocBtn.className = 'flex items-center gap-1.5 px-3 py-1.5 rounded-md font-medium bg-zinc-800 text-white shadow-sm transition';
        viewGraphBtn.className = 'flex items-center gap-1.5 px-3 py-1.5 rounded-md font-medium text-zinc-400 hover:text-white transition';
        documentPane.classList.remove('hidden');
        graphPane.classList.add('hidden');
        sidebarPane.classList.add('hidden');
        toggleSectionsBtn.classList.remove('hidden');
      }} else {{
        viewGraphBtn.className = 'flex items-center gap-1.5 px-3 py-1.5 rounded-md font-medium bg-zinc-800 text-white shadow-sm transition';
        viewDocBtn.className = 'flex items-center gap-1.5 px-3 py-1.5 rounded-md font-medium text-zinc-400 hover:text-white transition';
        documentPane.classList.add('hidden');
        graphPane.classList.remove('hidden');
        sidebarPane.classList.remove('hidden');
        toggleSectionsBtn.classList.add('hidden');
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

    // Document sections: default collapsed, remembered for this session only (never persisted),
    // so a re-render after a filter change never surprise-collapses one the user just opened.
    function updateToggleSectionsButton() {{
      const label = expandedSections.size > 0 ? 'Collapse all sections' : 'Expand all sections';
      toggleSectionsBtn.title = label;
      toggleSectionsBtn.setAttribute('aria-label', label);
    }}

    toggleSectionsBtn.addEventListener('click', () => {{
      if (expandedSections.size > 0) {{
        expandedSections.clear();
      }} else {{
        allSectionIds.forEach(id => expandedSections.add(id));
      }}
      renderUnifiedDocument();
    }});

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
      renderTree(treeData);
      renderUnifiedDocument();
    }});

    // Filters (mirrored into the URL hash so a view is shareable)
    const NO_REPO = '(none)';
    const filters = {{ statuses: new Set(), repo: '', model: '', q: '' }};

    function structuralFilterActive() {{
      return filters.statuses.size > 0 || filters.repo !== '' || filters.model !== '';
    }}

    function taskPasses(t) {{
      const status = t.virtual_status || t.status;
      if (filters.statuses.size > 0 && !filters.statuses.has(status)) return false;
      if (filters.repo !== '' && (t.target_repo || NO_REPO) !== filters.repo) return false;
      if (filters.model !== '' && !(t.acceptable_models || []).includes(filters.model)) return false;
      return true;
    }}

    function textMatches(n) {{
      return n.title.toLowerCase().includes(filters.q) || n.id.toLowerCase().includes(filters.q);
    }}

    function textAccepts(n, parentTextOk) {{
      return filters.q === '' || parentTextOk || textMatches(n);
    }}

    function nodeVisible(n, parentTextOk = false) {{
      const textOk = textAccepts(n, parentTextOk);
      if (n.kind === 'task') return textOk && taskPasses(n);
      return (n.children || []).some(c => nodeVisible(c, textOk)) || (!structuralFilterActive() && textOk);
    }}

    function readHash() {{
      const p = new URLSearchParams(location.hash.slice(1));
      filters.statuses = new Set((p.get('status') || '').split(',').filter(c => window.STATUS_THEMES[c]));
      filters.repo = p.get('repo') || '';
      filters.model = p.get('model') || '';
      filters.q = (p.get('q') || '').toLowerCase();
      searchBox.value = filters.q;
    }}

    function writeHash() {{
      const p = new URLSearchParams();
      if (filters.statuses.size) p.set('status', [...filters.statuses].join(','));
      if (filters.repo) p.set('repo', filters.repo);
      if (filters.model) p.set('model', filters.model);
      if (filters.q) p.set('q', filters.q);
      try {{
        history.replaceState(null, '', p.toString() ? '#' + p : location.pathname + location.search);
      }} catch (e) {{
        console.error('Could not update the URL hash:', e);
      }}
    }}

    function renderAll() {{
      writeHash();
      updateStatsDigest();
      renderFilterBar();
      renderTree(treeData);
      renderUnifiedDocument();
      applyGraphFilter();
    }}

    function esc(text) {{
      return String(text ?? '').replace(/[&<>"']/g, ch => ({{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }}[ch]));
    }}

    function statusChip(code, size = 'text-[10px]') {{
      const t = getTheme(code);
      return `<span class="st-chip st-${{t.code}} inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full font-medium ${{size}}" title="${{esc(t.description)}}">${{renderIcon(t.icon, 'w-3 h-3')}}<span>${{esc(t.label)}}</span></span>`;
    }}

    function statusIcon(code, size = 'w-3.5 h-3.5') {{
      const t = getTheme(code);
      return `<span class="st-text st-${{t.code}} flex-shrink-0">${{renderIcon(t.icon, size)}}</span>`;
    }}

    // A tree row carries exactly one status marker: a coloured dot, name in the tooltip only.
    function statusDot(code) {{
      const t = getTheme(code);
      return `<span class="st-dot st-${{t.code}} inline-block w-2.5 h-2.5 rounded-full flex-shrink-0" title="${{esc(t.label)}}"></span>`;
    }}

    // Progress: per-status counts over a spec's or plan's tasks. Only COMPLETED counts as done.
    function progressParts(node) {{
      const p = node.progress || {{ total: 0, counts: {{}} }};
      const codes = Object.keys(window.STATUS_THEMES).filter(c => p.counts[c]);
      return {{ total: p.total, completed: p.counts.COMPLETED || 0, codes, counts: p.counts }};
    }}

    function progressText(node) {{
      const p = progressParts(node);
      if (p.total === 0) return 'No tasks';
      return p.codes.map(c => `${{p.counts[c]}} ${{getTheme(c).label.toLowerCase()}}`).join(' · ');
    }}

    function progressBar(node, height = 'h-1.5') {{
      const p = progressParts(node);
      const text = esc(progressText(node));
      const segs = p.codes.map(c => `<span class="st-seg st-${{c}}" style="width:${{(p.counts[c] / p.total) * 100}}%"></span>`).join('');
      return `<div class="flex w-full ${{height}} rounded-full overflow-hidden bg-zinc-800" role="img" aria-label="${{text}}" title="${{text}}">${{segs}}</div>`;
    }}

    function renderSectionBody(content) {{
      const text = content || '';
      if (typeof marked === 'undefined') {{
        return `<pre class="whitespace-pre-wrap font-sans">${{esc(text)}}</pre>`;
      }}
      return marked.parse(text, {{ breaks: true }});
    }}

    // Sections default collapsed; expandedSections remembers, for this session only, which
    // ones the user opened, so re-rendering after a filter change never re-collapses them.
    function renderSections(sections, ownerId) {{
      return (sections || []).map(s => {{
        const id = `${{ownerId}}::${{s.key}}`;
        allSectionIds.push(id);
        const isOpen = expandedSections.has(id);
        return `
          <details ${{isOpen ? 'open' : ''}} data-section-id="${{id}}" class="rounded-lg border border-zinc-800 bg-zinc-950/60">
            <summary class="cursor-pointer select-none px-3 py-1.5 text-xs font-semibold text-zinc-300 flex items-center gap-2">
              ${{renderIcon('chevron-right', 'w-3 h-3 text-zinc-500 details-caret')}}
              <span>${{esc((s.header || s.key).replace(/^#+\\s*/, ''))}}</span>
              <span class="font-mono text-[10px] text-zinc-400 font-normal">${{esc(s.key)}}</span>
            </summary>
            <div class="prose prose-invert max-w-none px-3 pb-3 text-xs leading-relaxed text-zinc-400">${{renderSectionBody(s.content)}}</div>
          </details>
        `;
      }}).join('');
    }}

    function attachSectionToggleHandlers(root) {{
      root.querySelectorAll('details[data-section-id]').forEach(details => {{
        details.addEventListener('toggle', () => {{
          const id = details.getAttribute('data-section-id');
          if (details.open) expandedSections.add(id);
          else expandedSections.delete(id);
          updateToggleSectionsButton();
        }});
      }});
    }}

    function renderDependencies(details, status) {{
      if (!details || details.length === 0) return '';
      const unfinished = details.filter(d => !d.finished);
      const why = status === 'BLOCKED' && unfinished.length > 0
        ? `<div class="st-chip st-BLOCKED rounded-lg px-2.5 py-1.5 text-xs">Blocked by ${{unfinished.map(d => esc(d.id)).join(', ')}}: not completed or superseded yet.</div>`
        : '';
      const rows = details.map(d => `
        <div class="flex items-center gap-2 px-2 py-1.5 bg-zinc-950/60">
          ${{d.status ? statusChip(d.status) : '<span class="text-[10px] font-mono text-red-400">missing</span>'}}
          <span class="font-mono text-[11px] text-zinc-300">${{esc(d.id)}}</span>
          <span class="truncate text-[11px] text-zinc-400">${{esc(d.title || '')}}</span>
        </div>
      `).join('');
      return `
        <div class="space-y-1.5 pt-2">
          <div class="font-semibold text-zinc-400 uppercase tracking-wider text-[10px]">Depends on (${{details.length}})</div>
          ${{why}}
          <div class="divide-y divide-zinc-800 rounded border border-zinc-800">${{rows}}</div>
        </div>
      `;
    }}

    // Status Digest Bar
    // Icon-only chips (dot + count) so the view switcher, search box and every status count
    // fit on one toolbar row; the status name lives in the native title tooltip instead.
    function updateStatsDigest() {{
      statsDigest.innerHTML = '';
      const allActive = filters.statuses.size === 0;
      const totalChip = document.createElement('button');
      totalChip.className = `flex items-center gap-1 px-2 py-1 rounded-md border text-xs transition ${{allActive ? 'bg-zinc-800 text-white border-zinc-700' : 'bg-zinc-900/60 text-zinc-400 border-zinc-800 hover:bg-zinc-800'}}`;
      totalChip.title = 'All tasks';
      totalChip.setAttribute('aria-label', `All tasks: ${{statsData.total || 0}}`);
      totalChip.setAttribute('aria-pressed', String(allActive));
      totalChip.innerHTML = `${{renderIcon('layers', 'w-3.5 h-3.5')}}<strong>${{statsData.total || 0}}</strong>`;
      totalChip.onclick = () => {{
        filters.statuses.clear();
        renderAll();
      }};
      statsDigest.appendChild(totalChip);

      window.STATUS_GROUPS.forEach((group, groupIndex) => {{
        if (groupIndex > 0) {{
          const divider = document.createElement('div');
          divider.className = 'w-px h-4 bg-zinc-800 mx-0.5 flex-shrink-0';
          statsDigest.appendChild(divider);
        }}
        Object.keys(window.STATUS_THEMES).filter(code => window.STATUS_THEMES[code].group === group.code).forEach(code => {{
          const theme = getTheme(code);
          const count = statsData[code] || 0;
          const active = filters.statuses.has(code);
          const chip = document.createElement('button');
          chip.className = `st-chip st-${{code}} flex items-center gap-1 px-1.5 py-1 rounded-md text-xs transition hover:brightness-125 ${{active ? 'ring-2 ring-white/60' : ''}} ${{count === 0 && !active ? 'opacity-50' : ''}}`;
          chip.title = theme.label;
          chip.setAttribute('aria-label', `${{theme.label}}: ${{count}}`);
          chip.setAttribute('aria-pressed', String(active));
          chip.innerHTML = `${{renderIcon(theme.icon, 'w-3.5 h-3.5')}}<strong>${{count}}</strong>`;
          chip.onclick = () => {{
            if (active) filters.statuses.delete(code);
            else filters.statuses.add(code);
            renderAll();
          }};
          statsDigest.appendChild(chip);
        }});
      }});
    }}

    function fillSelect(select, values, current, allLabel) {{
      const options = [...new Set([...values, ...(current ? [current] : [])])].sort();
      select.innerHTML = `<option value="">${{allLabel}}</option>` + options.map(v => `<option value="${{esc(v)}}">${{esc(v)}}</option>`).join('');
      select.value = current;
    }}

    function collectTasks(nodes, out = []) {{
      nodes.forEach(n => {{
        if (n.kind === 'task') out.push(n);
        collectTasks(n.children || [], out);
      }});
      return out;
    }}

    function populateFilterOptions() {{
      const tasks = collectTasks(treeData);
      fillSelect(repoFilter, tasks.map(t => t.target_repo || NO_REPO), filters.repo, 'All repos');
      fillSelect(modelFilter, tasks.flatMap(t => t.acceptable_models || []), filters.model, 'All models');
    }}

    function renderFilterBar() {{
      const pills = [];
      filters.statuses.forEach(c => pills.push([`Status: ${{getTheme(c).label}}`, () => filters.statuses.delete(c)]));
      if (filters.repo) pills.push([`Repo: ${{filters.repo}}`, () => {{ filters.repo = ''; }}]);
      if (filters.model) pills.push([`Model: ${{filters.model}}`, () => {{ filters.model = ''; }}]);
      if (filters.q) pills.push([`Text: ${{filters.q}}`, () => {{ filters.q = ''; searchBox.value = ''; }}]);

      activeFilters.innerHTML = '';
      pills.forEach(([label, remove]) => {{
        const pill = document.createElement('button');
        pill.className = 'flex items-center gap-1 px-2 py-0.5 rounded-full border border-zinc-600 bg-zinc-800 text-zinc-200 text-[11px] hover:bg-zinc-700';
        pill.setAttribute('aria-label', `Remove filter ${{label}}`);
        pill.innerHTML = `<span>${{esc(label)}}</span>${{renderIcon('x', 'w-3 h-3')}}`;
        pill.onclick = () => {{
          remove();
          renderAll();
        }};
        activeFilters.appendChild(pill);
      }});
      if (pills.length > 0) {{
        const clear = document.createElement('button');
        clear.className = 'px-2 py-0.5 rounded-full text-[11px] text-zinc-300 underline hover:text-white';
        clear.textContent = 'Clear all';
        clear.onclick = () => {{
          filters.statuses.clear();
          filters.repo = '';
          filters.model = '';
          filters.q = '';
          searchBox.value = '';
          renderAll();
        }};
        activeFilters.appendChild(clear);
      }}
      repoFilter.value = filters.repo;
      modelFilter.value = filters.model;
    }}

    repoFilter.addEventListener('change', () => {{
      filters.repo = repoFilter.value;
      renderAll();
    }});
    modelFilter.addEventListener('change', () => {{
      filters.model = modelFilter.value;
      renderAll();
    }});

    // Legend
    function renderLegend() {{
      legendBody.innerHTML = window.STATUS_GROUPS.map(group => {{
        const rows = Object.values(window.STATUS_THEMES).filter(t => t.group === group.code).map(t => `
          <div class="flex items-start gap-2 py-1">
            <div class="w-36 flex-shrink-0">${{statusChip(t.code)}}</div>
            <p class="text-xs text-zinc-300">${{esc(t.description)}}</p>
          </div>
        `).join('');
        return `<div><div class="text-[10px] uppercase tracking-wider text-zinc-400 mt-2">${{esc(group.label)}}</div>${{rows}}</div>`;
      }}).join('');
    }}

    function setLegendOpen(open) {{
      legendPanel.classList.toggle('hidden', !open);
      legendBtn.setAttribute('aria-expanded', String(open));
    }}

    legendBtn.addEventListener('click', () => setLegendOpen(legendPanel.classList.contains('hidden')));
    legendCloseBtn.addEventListener('click', () => setLegendOpen(false));
    document.addEventListener('keydown', (e) => {{
      if (e.key === 'Escape') setLegendOpen(false);
    }});

    function applyGraphFilter() {{
      if (!visNodesDS) return;
      visNodesDS.update(graphData.nodes.map(n => {{
        const dim = n.kind === 'task' && !(taskPasses(n) && textAccepts(n, false));
        return {{ id: n.id, opacity: dim ? 0.2 : 1 }};
      }}));
    }}

    // Sidebar Tree Rendering
    function renderTree(nodes) {{
      treeList.innerHTML = '';

      function createNodeRow(node, depth = 0, parentTextOk = false) {{
        if (!nodeVisible(node, parentTextOk)) return;
        const textOk = textAccepts(node, parentTextOk);

        const effectiveStatus = node.virtual_status || node.status;
        const hasChildren = node.children && node.children.length > 0;
        const isCollapsed = collapsedNodes.has(node.id);

        const row = document.createElement('div');
        row.className = `px-2.5 py-1.5 rounded-lg cursor-pointer text-xs group transition ${{selectedNodeId === node.id ? 'bg-zinc-800 text-white font-medium border border-zinc-700' : 'text-zinc-400 hover:bg-zinc-850 hover:text-zinc-200'}}`;
        row.style.paddingLeft = `${{depth * 14 + 8}}px`;

        let chevron = `<span class="w-3.5 h-3.5 inline-block"></span>`;
        if (hasChildren) {{
          chevron = `<button class="p-0.5 hover:text-white toggle-btn">${{renderIcon(isCollapsed ? 'chevron-right' : 'chevron-down', 'w-3.5 h-3.5')}}</button>`;
        }}

        let progressHtml = '';
        if (node.progress && node.progress.total > 0) {{
          const p = progressParts(node);
          progressHtml = `<span class="text-[10px] font-mono text-zinc-400 mr-1" title="${{esc(progressText(node))}}">${{p.completed}}/${{p.total}}</span>`;
        }}

        const progressBarRow = progressHtml ? `<div class="pt-1.5">${{progressBar(node, 'h-1')}}</div>` : '';

        row.innerHTML = `
          <div class="flex items-center justify-between gap-2">
            <div class="flex items-center gap-1.5 min-w-0 truncate">
              ${{chevron}}
              ${{statusDot(effectiveStatus)}}
              <span class="font-mono text-[10px] text-zinc-400 uppercase">${{node.id}}</span>
              <span class="truncate">${{node.title}}</span>
            </div>
            <div class="flex items-center gap-1 flex-shrink-0">
              ${{progressHtml}}
            </div>
          </div>
          ${{progressBarRow}}
        `;

        const toggleBtn = row.querySelector('.toggle-btn');
        if (toggleBtn) {{
          toggleBtn.onclick = (e) => {{
            e.stopPropagation();
            if (isCollapsed) collapsedNodes.delete(node.id);
            else collapsedNodes.add(node.id);
            renderTree(treeData);
            renderUnifiedDocument();
          }};
        }}

        row.onclick = () => selectNode(node.id);
        treeList.appendChild(row);

        if (hasChildren && !isCollapsed) {{
          node.children.forEach(c => createNodeRow(c, depth + 1, textOk));
        }}
      }}

      nodes.forEach(n => createNodeRow(n));
    }}

    searchBox.addEventListener('input', (e) => {{
      filters.q = e.target.value.toLowerCase();
      renderAll();
    }});

    // Select Node Action (Coordinates Tree, Document, and Graph)
    function selectNode(nodeId) {{
      selectedNodeId = nodeId;
      renderTree(treeData);

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
      allSectionIds = [];

      if (treeData.length === 0) {{
        unifiedDocument.innerHTML = '<div class="text-zinc-400 text-sm italic py-12 text-center">No specifications or tasks registered. Use CLI to add items.</div>';
        return;
      }}

      treeData.filter(root => nodeVisible(root)).forEach(spec => {{
        const specStatus = spec.virtual_status || spec.status;
        const specTextOk = textAccepts(spec, false);

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

        const specSectionsHtml = spec.sections && spec.sections.length > 0
          ? `<div class="space-y-2 pt-2">${{renderSections(spec.sections, spec.id)}}</div>`
          : '';

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
                ${{spec.children.filter(plan => nodeVisible(plan, specTextOk)).map(plan => renderPlanCard(plan, specTextOk)).join('')}}
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
                ${{statusChip(specStatus, 'text-xs')}}
              </div>
              <div class="flex items-center gap-2">
                ${{specPills}}
              </div>
            </div>
            <h1 class="text-2xl font-bold tracking-tight text-white">${{spec.title}}</h1>
            <div class="space-y-1.5">
              ${{progressBar(spec, 'h-2.5')}}
              <div class="text-xs text-zinc-400">${{esc(progressText(spec))}}</div>
            </div>
          </div>
          ${{specSectionsHtml}}
          ${{plansHtml}}
        `;

        unifiedDocument.appendChild(specCard);
      }});

      if (!unifiedDocument.hasChildNodes()) {{
        unifiedDocument.innerHTML = '<div class="text-zinc-400 text-sm italic py-12 text-center">No tasks match the active filters.</div>';
      }}

      attachCollapsibleHandlers();
      attachSectionToggleHandlers(unifiedDocument);
      updateToggleSectionsButton();
    }}

    function renderPlanCard(plan, parentTextOk = false) {{
      const planStatus = plan.virtual_status || plan.status;
      const isCollapsed = collapsedNodes.has(plan.id);

      const tasks = plan.children || [];
      const p = progressParts(plan);
      const planTextOk = textAccepts(plan, parentTextOk);
      const visibleTasks = tasks.filter(t => nodeVisible(t, planTextOk));
      const planSectionsHtml = plan.sections && plan.sections.length > 0
        ? `<div class="space-y-2 pt-2">${{renderSections(plan.sections, plan.id)}}</div>`
        : '';

      return `
        <div id="doc-node-${{plan.id}}" class="border border-zinc-800 rounded-xl bg-zinc-900/30 transition">
          <!-- Plan Header: sticky so the current plan stays identified while its tasks scroll by -->
          <div class="h-12 px-4 rounded-t-xl bg-zinc-900/95 backdrop-blur-sm border-b border-zinc-800 flex items-center justify-between cursor-pointer plan-header sticky top-0 z-20" data-node-id="${{plan.id}}">
            <div class="flex items-center gap-2.5 min-w-0 truncate">
              <button class="text-zinc-400 hover:text-white flex-shrink-0">${{renderIcon(isCollapsed ? 'chevron-right' : 'chevron-down', 'w-4 h-4')}}</button>
              <span class="font-mono text-xs font-semibold text-emerald-400 flex-shrink-0">${{plan.id}}</span>
              <span class="text-base font-semibold text-zinc-200 truncate">${{plan.title}}</span>
              ${{statusChip(planStatus, 'text-[11px]')}}
            </div>
            <div class="flex items-center gap-3 flex-shrink-0">
              <div class="flex items-center gap-2">
                <div class="w-40">${{progressBar(plan, 'h-2')}}</div>
                <span class="font-mono text-xs text-zinc-400" title="Completed of total tasks">${{p.completed}}/${{p.total}}</span>
              </div>
            </div>
          </div>

          <div class="px-4 py-1.5 text-[11px] text-zinc-400 border-b border-zinc-800/60 bg-zinc-900/40">${{esc(progressText(plan))}}</div>

          <!-- Plan Body -->
          <div class="plan-body ${{isCollapsed ? 'hidden' : ''}} p-4 space-y-4">
            ${{planSectionsHtml}}
            <div class="space-y-3">
              <div class="text-[11px] font-semibold uppercase tracking-wider text-zinc-400 flex items-center gap-2">
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

      const sectionsHtml = renderSections(task.sections, task.id);
      const depsHtml = renderDependencies(task.dependency_details, taskStatus);

      return `
        <div id="doc-node-${{task.id}}" class="border border-zinc-800/80 rounded-lg bg-zinc-950/40 hover:border-zinc-700 transition">
          <!-- Task Header: sticky one level below the plan header it belongs to -->
          <div class="h-10 px-3 rounded-t-lg flex items-center justify-between cursor-pointer task-header bg-zinc-900/90 backdrop-blur-sm hover:bg-zinc-900 sticky top-12 z-10" data-node-id="${{task.id}}">
            <div class="flex items-center gap-2 min-w-0 truncate">
              <button class="text-zinc-500 hover:text-white flex-shrink-0">${{renderIcon(isCollapsed ? 'chevron-right' : 'chevron-down', 'w-3.5 h-3.5')}}</button>
              ${{statusIcon(taskStatus)}}
              <span class="font-mono text-xs font-semibold text-emerald-400 flex-shrink-0">${{task.id}}</span>
              <span class="text-sm font-medium text-zinc-200 truncate">${{task.title}}</span>
            </div>
            <div class="flex items-center gap-2 flex-shrink-0">
              ${{modelPills}}
              <span class="px-1.5 py-0.5 rounded bg-zinc-900 border border-zinc-800 text-zinc-400 font-mono text-[10px]">P${{task.priority || 50}}</span>
              ${{statusChip(taskStatus)}}
            </div>
          </div>

          <!-- Task Body -->
          <div class="task-body ${{isCollapsed ? 'hidden' : ''}} p-3.5 bg-zinc-950/80 border-t border-zinc-800/60 space-y-2">
            ${{leaseBanner}}
            ${{verificationsHtml}}
            ${{depsHtml}}
            <div class="space-y-2 pt-2">${{sectionsHtml}}</div>
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
          renderTree(treeData);
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
    // Shape per node kind so spec/plan/task/review_gate read as distinct at a glance,
    // independent of the status colouring the fill and border already carry.
    const GRAPH_SHAPE_BY_KIND = {{
      spec: {{ shape: 'hexagon' }},
      plan: {{ shape: 'box', shapeProperties: {{ borderRadius: 14 }} }},
      task: {{ shape: 'box', shapeProperties: {{ borderRadius: 3 }} }},
      review_gate: {{ shape: 'diamond' }}
    }};

    function renderGraph(graph) {{
      const container = document.getElementById('network-canvas');

      const visNodes = graph.nodes.map(n => {{
        const theme = getTheme(n.status);
        return {{
          id: n.id,
          label: `${{n.id}}\\n${{n.title}}\\n*${{theme.label}}*`,
          ...(GRAPH_SHAPE_BY_KIND[n.kind] || GRAPH_SHAPE_BY_KIND.task),
          margin: 16,
          widthConstraint: {{ minimum: 170, maximum: 260 }},
          color: {{
            background: theme.graph_bg,
            border: theme.graph_border,
            highlight: {{ background: theme.graph_bg, border: '#ffffff' }}
          }},
          font: {{ color: '#f3f4f6', face: 'Inter', size: 16, multi: 'md' }},
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

      visNodesDS = new vis.DataSet(visNodes);
      const data = {{ nodes: visNodesDS, edges: new vis.DataSet(visEdges) }};
      const options = {{
        layout: {{
          hierarchical: {{
            direction: 'UD',
            sortMethod: 'directed',
            levelSeparation: 240,
            nodeSpacing: 320
          }}
        }},
        physics: false,
        interaction: {{ hover: true, selectConnectedEdges: true }}
      }};

      if (networkInstance) {{
        networkInstance.destroy();
      }}
      networkInstance = new vis.Network(container, data, options);
      applyGraphFilter();

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
      const status = detail.virtual_status || n.status;

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
            <div class="text-zinc-400 font-mono text-[11px]">${{detail.lease.branch_name}}</div>
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

      body.innerHTML = `
        <div class="flex items-center gap-2">
          ${{statusChip(status, 'text-xs')}}
          <span class="px-2 py-0.5 rounded bg-zinc-950 border border-zinc-800 font-mono text-zinc-400 text-xs">Prio: ${{n.priority || 50}}</span>
        </div>
        ${{leaseBanner}}
        ${{verificationsHtml}}
        ${{renderDependencies(detail.dependency_details, status)}}
        <div class="space-y-2 pt-2">${{renderSections(detail.sections, n.id)}}</div>
      `;
      attachSectionToggleHandlers(body);
    }}

    // Fetch and Load Data
    async function loadAllData() {{
      if (isStaticMode) {{
        treeData = window.STATIC_DATA.tree || [];
        graphData = window.STATIC_DATA.graph || {{ nodes: [], edges: [] }};
        statsData = window.STATIC_DATA.stats || {{}};
        populateFilterOptions();
        renderAll();
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

        populateFilterOptions();
        renderAll();
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
          connectionPill.title = 'Synced';
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
          connectionPill.title = 'Disconnected';
          connectionDot.className = 'w-1.5 h-1.5 rounded-full bg-red-400';
          setTimeout(connectWS, 3000);
        }};
      }}
      connectWS();
    }} else {{
      connectionStatus.textContent = 'Static Export';
      connectionPill.title = 'Static Export';
      connectionDot.className = 'w-1.5 h-1.5 rounded-full bg-zinc-500';
    }}

    // Initialize
    readHash();
    renderLegend();
    window.addEventListener('hashchange', () => {{
      readHash();
      populateFilterOptions();
      renderAll();
    }});
    loadAllData();
  </script>
</body>
</html>
"""
