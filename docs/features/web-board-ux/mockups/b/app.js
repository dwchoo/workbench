/* Mockup-only demo: theme toggle and banner close/reopen. Pages are complete without it. */
(function () {
  var d = document.documentElement, K = 'wbx-theme';
  d.classList.add('js');
  try { var t = localStorage.getItem(K); if (t) d.setAttribute('data-theme', t); } catch (x) {}
  document.addEventListener('click', function (ev) {
    if (ev.target.closest('[data-theme-toggle]')) {
      var cur = d.getAttribute('data-theme');
      var dark = cur ? cur === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
      var next = dark ? 'light' : 'dark';
      d.setAttribute('data-theme', next);
      try { localStorage.setItem(K, next); } catch (x) {}
    } else if (ev.target.closest('[data-banner-close]')) {
      d.setAttribute('data-banner', 'collapsed');
    } else if (ev.target.closest('[data-banner-open]')) {
      d.removeAttribute('data-banner');
    }
  });
})();
