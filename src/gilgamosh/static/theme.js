'use strict';
// Loaded blocking in <head>: the saved theme must apply before the first paint.
(() => {
  let saved = null;
  try { saved = localStorage.getItem('theme'); } catch { /* private mode: follow the system */ }
  const dark = saved ? saved === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = dark ? 'gilgamosh-dark' : 'gilgamosh';
})();
