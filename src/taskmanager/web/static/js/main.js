// Initialize
readHash();
renderLegend();
window.addEventListener('hashchange', () => {
  readHash();
  populateFilterOptions();
  renderAll();
});
loadAllData();
