// Runs before first paint so the page doesn't flash the wrong theme.
(function () {
  var pref = 'dark';
  try { pref = localStorage.getItem('hd-theme') || 'dark'; } catch (e) {}
  if (pref === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', pref);
})();
