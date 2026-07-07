/* Live type-ahead for the global nav search. Debounced fetch to /search.json?q=, renders a dropdown
 * of top matches under the box. Arrow keys move the highlight, Enter opens it (or, with nothing
 * highlighted, submits the form for the full /search results page), Esc/outside-click closes.
 * Dependency-free; progressive enhancement — the form still works if this doesn't load.
 */
(function () {
  'use strict';
  var form = document.querySelector('form.nav-search');
  var input = form && form.querySelector('input[name="q"]');
  if (!input) return;

  var box = document.createElement('div');
  box.className = 'search-dropdown';
  box.style.display = 'none';
  form.appendChild(box);

  var items = [];      // {type,label,sub,url}
  var active = -1;     // highlighted index
  var seq = 0;         // request sequence, to drop stale responses
  var timer = null;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function close() { box.style.display = 'none'; box.innerHTML = ''; items = []; active = -1; }

  function render() {
    if (!items.length) {
      box.innerHTML = '<div class="search-empty">No quick matches — press Enter to search everything.</div>';
    } else {
      box.innerHTML = items.map(function (it, i) {
        return '<a href="' + esc(it.url) + '" class="search-item" data-i="' + i + '">'
          + '<span class="stype">' + esc(it.type) + '</span>'
          + '<span class="slabel">' + esc(it.label) + '</span>'
          + (it.sub ? '<span class="ssub">' + esc(it.sub) + '</span>' : '')
          + '</a>';
      }).join('');
    }
    var q = encodeURIComponent(input.value.trim());
    box.innerHTML += '<a href="/search?q=' + q + '" class="search-all">See all results →</a>';
    box.style.display = 'block';
    active = -1;
  }

  function highlight(n) {
    var links = box.querySelectorAll('a.search-item');
    if (!links.length) return;
    if (active >= 0 && links[active]) links[active].classList.remove('active');
    active = (n + links.length) % links.length;
    links[active].classList.add('active');
    links[active].scrollIntoView({ block: 'nearest' });
  }

  function fetchNow() {
    var q = input.value.trim();
    if (!q) { close(); return; }
    var mine = ++seq;
    fetch('/search.json?q=' + encodeURIComponent(q), { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (data) { if (mine === seq) { items = data || []; render(); } })
      .catch(function () { /* ignore; the form submit still works */ });
  }

  input.addEventListener('input', function () {
    clearTimeout(timer);
    timer = setTimeout(fetchNow, 160);
  });
  input.addEventListener('focus', function () { if (input.value.trim() && box.innerHTML) box.style.display = 'block'; });

  input.addEventListener('keydown', function (e) {
    if (box.style.display === 'none') return;
    if (e.key === 'ArrowDown') { e.preventDefault(); highlight(active + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); highlight(active - 1); }
    else if (e.key === 'Enter') {
      var links = box.querySelectorAll('a.search-item');
      if (active >= 0 && links[active]) { e.preventDefault(); window.location.href = links[active].getAttribute('href'); }
      // else: let the form submit to /search
    } else if (e.key === 'Escape') { close(); }
  });

  document.addEventListener('click', function (e) {
    if (!form.contains(e.target)) close();
  });
})();
