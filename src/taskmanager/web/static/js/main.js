// Initialize
readHash();
renderLegend();
window.tmStore.setFilters(filtersToF());
window.addEventListener('hashchange', () => {
  readHash();
  window.tmStore.setFilters(filtersToF());
  scheduleRender();
});
scheduleRender();
