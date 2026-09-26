// Initialize
// A static export has no /api/waves to simulate against, so it opens on the one view its
// embedded rows/edges can actually render. Runs here, after core.js has declared
// VIEW_BTN_ACTIVE/VIEW_BTN_INACTIVE, not at core.js's own top level where they are still
// in their temporal dead zone.
if (isStaticMode) setViewMode(window.VIEW_MODES.GRAPH);
readHash();
renderLegend();
window.tmStore.setFilters(filtersToF());
window.addEventListener('hashchange', () => {
  readHash();
  window.tmStore.setFilters(filtersToF());
  scheduleRender();
});
scheduleRender();
