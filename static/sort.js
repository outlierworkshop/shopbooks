/* ShopBooks client-side sorting, with the chosen sort remembered per page.
 * - Any <table class="sortable">: click a column header to sort; click again to reverse.
 * - Any card list: a toolbar with [data-sortbar="#listSelector"] and buttons carrying
 *   [data-field] sorts the [data-sortitem] children of that list by their data-<field> attr.
 * Money ("$1,234.56", "(45.00)"), plain numbers, ISO dates (YYYY-MM-DD) and text are auto-detected.
 * The active sort is saved in localStorage keyed by page path + table/list, so it survives the
 * full page reload that happens after posting/skipping/saving. No dependencies.
 */
(function () {
  'use strict';

  // ---------- persistence ----------
  function save(key, val) {
    try { localStorage.setItem(key, JSON.stringify(val)); } catch (e) { /* ignore */ }
  }
  function load(key) {
    try { return JSON.parse(localStorage.getItem(key)); } catch (e) { return null; }
  }

  // ---------- value helpers ----------
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
      if (!v || v === '—' || v === '-') continue;
      seen++;
      if (/^-?\$?\(?[\d,]+(\.\d+)?\)?%?$/.test(v)) nums++;
      else if (/^\d{4}-\d{2}-\d{2}/.test(v)) dates++;
    }
    if (seen === 0) return 'text';
    if (nums === seen) return 'num';
    if (dates === seen) return 'date';
    return 'text';
  }

  function comparator(type) {
    return function (a, b) {
      if (type === 'num') {
        var na = parseNum(a), nb = parseNum(b);
        if (na === null && nb === null) return 0;
        if (na === null) return 1;   // blanks last
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
  function bodyRows(table, headerRow) {
    return Array.prototype.slice.call(table.rows).filter(function (r) {
      if (r === headerRow) return false;
      if (r.classList.contains('no-sort')) return false;
      if (r.parentNode && r.parentNode.tagName === 'TFOOT') return false;
      return r.getElementsByTagName('td').length > 0;
    });
  }

  function applyTableSort(table, headerRow, ths, col, asc) {
    var rows = bodyRows(table, headerRow);
    if (rows.length) {
      var vals = rows.map(function (r) { return cellText(r.cells[col]); });
      var type = ths[col].getAttribute('data-sort-type') || detectType(vals);
      var cmp = comparator(type);
      rows.map(function (r, i) { return [vals[i], i, r]; })
        .sort(function (x, y) { var d = cmp(x[0], y[0]); return d !== 0 ? (asc ? d : -d) : x[1] - y[1]; })
        .forEach(function (t) { t[2].parentNode.appendChild(t[2]); });
    }
    ths.forEach(function (h) {
      h.removeAttribute('data-dir');
      var s = h.querySelector('.sort-arrow'); if (s) s.parentNode.removeChild(s);
    });
    ths[col].setAttribute('data-dir', asc ? 'asc' : 'desc');
    var arrow = document.createElement('span');
    arrow.className = 'sort-arrow';
    arrow.textContent = asc ? ' ▲' : ' ▼';
    ths[col].appendChild(arrow);
  }

  function initTable(table, key) {
    var rows = Array.prototype.slice.call(table.rows), headerRow = null, i;
    for (i = 0; i < rows.length; i++) {
      if (rows[i].getElementsByTagName('th').length) { headerRow = rows[i]; break; }
    }
    if (!headerRow) return;
    var ths = Array.prototype.slice.call(headerRow.cells);
    ths.forEach(function (th, col) {
      if (th.classList.contains('no-sort') || th.textContent.trim() === '') return;
      th.classList.add('sortable-th');
      th.addEventListener('click', function () {
        var asc = th.getAttribute('data-dir') !== 'asc';
        applyTableSort(table, headerRow, ths, col, asc);
        save(key, { col: col, dir: asc ? 'asc' : 'desc' });
      });
    });
    var saved = load(key);
    if (saved && typeof saved.col === 'number' && ths[saved.col] &&
        ths[saved.col].classList.contains('sortable-th')) {
      applyTableSort(table, headerRow, ths, saved.col, saved.dir !== 'desc');
    }
  }

  // ---------- card lists ----------
  function applyListSort(list, field, type, asc) {
    var items = Array.prototype.slice.call(list.querySelectorAll('[data-sortitem]'));
    if (items.length < 2) return;
    var cmp = comparator(type);
    items.map(function (el, i) { return [el.getAttribute('data-' + field) || '', i, el]; })
      .sort(function (x, y) { var d = cmp(x[0], y[0]); return d !== 0 ? (asc ? d : -d) : x[1] - y[1]; })
      .forEach(function (t) { list.appendChild(t[2]); });
  }

  function initBar(bar, key) {
    var list = document.querySelector(bar.getAttribute('data-sortbar'));
    if (!list) return;
    var btns = Array.prototype.slice.call(bar.querySelectorAll('[data-field]'));
    function mark(active, asc) {
      btns.forEach(function (b) { b.removeAttribute('data-dir'); b.textContent = b.textContent.replace(/\s*[▲▼]$/, ''); });
      active.setAttribute('data-dir', asc ? 'asc' : 'desc');
      active.textContent = active.textContent.replace(/\s*[▲▼]$/, '') + (asc ? ' ▲' : ' ▼');
    }
    btns.forEach(function (btn) {
      btn.addEventListener('click', function () {
        var field = btn.getAttribute('data-field');
        var type = btn.getAttribute('data-type') || 'text';
        var asc = btn.getAttribute('data-dir') !== 'asc';
        applyListSort(list, field, type, asc);
        mark(btn, asc);
        save(key, { field: field, type: type, dir: asc ? 'asc' : 'desc' });
      });
    });
    var saved = load(key);
    if (saved && saved.field) {
      for (var i = 0; i < btns.length; i++) {
        if (btns[i].getAttribute('data-field') === saved.field) {
          applyListSort(list, saved.field, saved.type || 'text', saved.dir !== 'desc');
          mark(btns[i], saved.dir !== 'desc');
          break;
        }
      }
    }
  }

  function init() {
    var path = location.pathname;
    Array.prototype.forEach.call(document.querySelectorAll('table.sortable'), function (t, idx) {
      initTable(t, 'sbsort:t:' + path + ':' + idx);
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-sortbar]'), function (bar) {
      initBar(bar, 'sbsort:l:' + path + ':' + bar.getAttribute('data-sortbar'));
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
