function renderDependencies(details, status) {
  if (!details || details.length === 0) return '';
  const unfinished = details.filter(d => !d.finished);
  const why = status === 'BLOCKED' && unfinished.length > 0
    ? `<div class="st-chip st-BLOCKED rounded-lg px-2.5 py-1.5 text-xs">Blocked by ${unfinished.map(d => esc(d.id)).join(', ')}: not completed or superseded yet.</div>`
    : '';
  const rows = details.map(d => `
    <div class="flex items-center gap-2 px-2 py-1.5 bg-zinc-950/60">
      ${d.status ? statusIcon(d.status) : '<span class="text-[10px] font-mono text-red-400">missing</span>'}
      <span class="font-mono text-[11px] text-zinc-300">${esc(d.id)}</span>
      <span class="truncate text-[11px] text-zinc-400">${esc(d.title || '')}</span>
    </div>
  `).join('');
  return `
    <div class="space-y-1.5 pt-2">
      <div class="font-semibold text-zinc-400 uppercase tracking-wider text-[10px]">Depends on (${details.length})</div>
      ${why}
      <div class="divide-y divide-zinc-800 rounded border border-zinc-800">${rows}</div>
    </div>
  `;
}


// Slide-over Node Inspector for Graph
async function showGraphInspector(nodeId) {
  selectedNodeId = nodeId;
  graphInspector.classList.remove('hidden');

  let detail = null;
  if (isStaticMode) {
    detail = (window.STATIC_DATA.details || {})[nodeId];
  } else {
    try {
      const res = await fetch(`/api/nodes/${nodeId}`);
      if (res.ok) detail = await res.json();
    } catch (e) {
      console.error('Failed to load node detail:', e);
    }
  }

  if (!detail) return;

  const n = detail.node;
  const status = detail.virtual_status || n.status;

  document.getElementById('inspector-kind').textContent = n.kind;
  document.getElementById('inspector-id').textContent = n.id;
  document.getElementById('inspector-title').textContent = n.title;

  const body = document.getElementById('inspector-body');
  let leaseBanner = '';
  if (detail.lease) {
    leaseBanner = `
      <div class="p-2.5 bg-blue-950/40 border border-blue-800/80 rounded-lg text-xs space-y-1">
        <div class="text-blue-300 font-semibold flex items-center gap-1.5">
          ${renderIcon('flame', 'w-3.5 h-3.5')}
          <span>In-Flight Active Lease</span>
        </div>
        <div class="text-zinc-400 font-mono">Agent: ${detail.lease.agent_id}</div>
        <div class="text-zinc-400 font-mono text-[11px]">${detail.lease.branch_name}</div>
      </div>
    `;
  }

  let verificationsHtml = '';
  if (detail.verifications && detail.verifications.length > 0) {
    verificationsHtml = `
      <div class="space-y-1.5 pt-2">
        <div class="font-semibold text-zinc-400 uppercase tracking-wider text-[10px]">Verifications</div>
        <div class="divide-y divide-zinc-800 rounded border border-zinc-800 font-mono text-[11px]">
          ${detail.verifications.map(v => `
            <div class="p-2 bg-zinc-950/60 flex items-center justify-between">
              <span class="text-emerald-400">${v.verification_type}</span>
              <span class="text-zinc-300">${v.target_path}</span>
            </div>
          `).join('')}
        </div>
      </div>
    `;
  }

  body.innerHTML = `
    <div class="flex items-center gap-2">
      ${statusIcon(status, 'w-4 h-4')}
      <span class="px-2 py-0.5 rounded bg-zinc-950 border border-zinc-800 font-mono text-zinc-400 text-xs">Prio: ${n.priority || 50}</span>
    </div>
    ${leaseBanner}
    ${verificationsHtml}
    ${renderDependencies(detail.dependency_details, status)}
    ${renderSections(detail.sections, n.id)}
  `;
  attachSectionToggleHandlers(body);
}

