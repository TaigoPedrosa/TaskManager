// Initialize
// A static export has no /api/waves to simulate against, so it drops the Waves segment
// outright (removed rather than hidden: setViewMode rewrites the button's className on every
// switch).
if (isStaticMode) {
  viewWavesBtn.remove();
}
renderLegend();
// Replacing rather than pushing the first entry also rewrites an old link's hash filters
// into the query string.
navigate(readLocation(), { replace: true });
window.tmStore.setFilters(filtersToF());
scheduleRender();
