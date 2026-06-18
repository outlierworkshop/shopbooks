/* ShopBooks client-side sorting.
 * - Any <table class="sortable">: click a column header to sort; click again to reverse.
 * - Any card list: a toolbar with [data-sortbar="#listSelector"] and buttons carrying
 *   [data-field] sorts the [data-sortitem] children of that list by their data-<field> attr.
 * Money ("$1,234.56", "(45.00)"), plain numbers, ISO dates (YYYY-MM-DD) and text are auto-detected.
 * No dependencies; degrades gracefully if JS is off (tables just stay in their server order).
 */
(function () {
  'use strict';

  function parseNum(v) {
    var s = String(v).replace(/[$,%\s]/g, '');
    if (/^\(.*\)$/.test(s)) s = '-' + s.slice(1, -1);
    var f = parseFloat(s);
    return isNaN(f) ? null : f;
  }

  function detectType(values) {
    var seen = 0, nums = 0, dates = 0;
    for (var i = 0; i < values.length; i++) {
      var v = values[i].trim();
      if (!v || v === '—' || v === '-') continue; // blanks / em-dash
      seen++;
      if (/^-?\$?\(?[\d,]+(\.\d+)?\)?%?$/.test(v)) nums++;
      else if (/^\d{4}-\d{2}-\d{2}/.test(v)) dates++;
    }
    if (seen === 0) return 'text';
    if (nums === seen) return 'num';
    if (dates === seen) return 'date'; // ISO dates sort correctly as text too
    return 'text';
  }

  function comparator(type) {
    return function (a, b) {
      if (type === 'num') {
        var na = parseNum(a), nb = parseNum(b);
        if (na === null && nb === null) return 0;
        if (na === null) return 1;   // blanks sort last (ascending)
        if (nb === null) return -1;
        return na - nb;
      }
      return a.trim().toLowerCase().localeCompare(b.trim().toLowerCase(), undefined, { numeric: true });
    };
  }

  function cellText(td) {
    if (!td) return '';
    var f = td.querySelector('input, select, textarea');
    if (f) {
      if (f.tagName === 'SELECT') { var o = f.options[f.selectedIndex]; return o ? o.text : ''; }
      return f.value || '';
    }
    return td.textContent.replace(/\s+/g, ' ').trim();
  }

  // ---------- tables ----------
  function initTable(table) {
    var rows = Array.prototype.slice.call(table.rows), headerRow = null, i;
    for (i = 0; i < rows.length; i++) {
      if (rows[i].getElementsByTagName('th').length) { headerRow = rows[i]; break; }
    }
    if (!headerRow) return;
    var ths = Array.prototype.slice.call(headerRow.cells);
    ths.forEach(function (th, col) {
      if (th.classList.contains('no-sort') || th.textContent.trim() === '') return;
      th.classList.add('sortable-th');
      th.addEventListener('click', function () { sortTable(table, headerRow, ths, col, th); });
    });
  }

  function bodyRows(table, headerRow) {
    return Array.prototype.slice.call(table.rows).filter(function (r) {
      if (r === headerRow) return false;
      if (r.classList.contains('no-sort')) return false;
      if (r.parentNode && r.parentNode.tagName === 'TFOOT') return false;
      return r.getElementsByTagName('td').length > 0; // skip header-only rows
    });
  }

  function sortTable(table, headerRow, ths, col, th) {
    var rows = bodyRows(table, headerRow);
    if (rows.length < 2) return;
    var vals = rows.map(function (r) { return cellText(r.cells[col]); });
    var type = th.getAttribute('data-sort-type') || detectType(vals);
    var asc = th.getAttribute('data-dir') !== 'asc';
    var cmp = comparator(type);
    rows.map(function (r, i) { return [vals[i], i, r]; })
      .sort(function (x, y) { var d = cmp(x[0], y[0]); return d !== 0 ? (asc ? d : -d) : x[1] - y[1]; })
      .forEach(function (t) { t[2].parentNode.appendChild(t[2]); });
    ths.forEach(function (h) {
      h.removeAttribute('data-dir');
      var s = h.querySelector('.sort-arrow'); if (s) s.parentNode.removeChild(s);
    });
    th.setAttribute('data-dir', asc ? 'asc' : 'desc');
    var arrow = document.createElement('span');
    arrow.className = 'sort-arrow';
    arrow.textContent = asc ? ' ▲' : ' ▼';
    th.appendChild(arrow);
  }

  // ---------- card lists ----------
  function initBar(bar) {
    var list = document.querySelector(bar.getAttribute('data-sortbar'));
    if (!list) return;
    var btns = Array.prototype.slice.call(bar.querySelectorAll('[data-field]'));
    btns.forEach(function (btn) {
      btn.addEventListener('click', function () {
        var field = btn.getAttribute('data-field');
        var type = btn.getAttribute('data-type') || 'text';
        var asc = btn.getAttribute('data-dir') !== 'asc';
        var items = Array.prototype.slice.call(list.querySelectorAll('[data-sortitem]'));
        if (items.length < 2) return;
        var cmp = comparator(type);
        items.map(function (el, i) { return [el.getAttribute('data-' + field) || '', i, el]; })
          .sort(function (x, y) { var d = cmp(x[0], y[0]); return d !== 0 ? (asc ? d : -d) : x[1] - y[1]; })
          .forEach(function (t) { list.appendChild(t[2]); });
        btns.forEach(function (b) { b.removeAttribute('data-dir'); b.textContent = b.textContent.replace(/\s*[▲▼]$/, ''); });
        btn.setAttribute('data-dir', asc ? 'asc' : 'desc');
        btn.textContent = btn.textContent.replace(/\s*[▲▼]$/, '') + (asc ? ' ▲' : ' ▼');
      });
    });
  }

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll('table.sortable'), initTable);
    Array.prototype.forEach.call(document.querySelectorAll('[data-sortbar]'), initBar);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
