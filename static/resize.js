/* ShopBooks draggable column resizing.
 * Any <table class="resizable"> (ideally with a <colgroup> of default widths) gets a drag handle
 * on each column's right edge. The table uses fixed layout at width:auto, so dragging a divider
 * changes ONLY that column's width — every column to its right keeps its own width and shifts
 * along with the divider (the table grows/shrinks; it never steals width from the next column).
 * Widths persist per page+table in localStorage. Plays nicely with sort.js (handle swallows the
 * click so it doesn't also sort). Wrap the table in .table-responsive so a wide table scrolls.
 */
(function () {
  'use strict';
  function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  function load(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } }

  function initTable(table, key) {
    var rows = table.rows, headerRow = null, i;
    for (i = 0; i < rows.length; i++) {
      if (rows[i].getElementsByTagName('th').length) { headerRow = rows[i]; break; }
    }
    if (!headerRow) return;
    var ths = Array.prototype.slice.call(headerRow.cells);
    var cols = Array.prototype.slice.call(table.querySelectorAll('colgroup > col'));
    table.style.tableLayout = 'fixed';
    table.style.width = 'auto';   // total width = sum of columns, so right columns keep their size

    function setW(idx, px) {
      px = Math.max(44, Math.round(px));
      if (cols[idx]) cols[idx].style.width = px + 'px';
      ths[idx].style.width = px + 'px';
    }

    var saved = load(key) || {};
    ths.forEach(function (th, idx) {
      var px = saved[idx]
        || (cols[idx] && parseFloat(cols[idx].style.width))
        || th.getBoundingClientRect().width;
      setW(idx, px);
    });

    ths.forEach(function (th, idx) {
      th.style.position = 'relative';
      var h = document.createElement('span');
      h.className = 'col-resize';
      th.appendChild(h);
      h.addEventListener('click', function (e) { e.stopPropagation(); }); // don't trigger sort
      h.addEventListener('mousedown', function (e) {
        e.preventDefault();
        e.stopPropagation();
        var startX = e.pageX, startW = th.getBoundingClientRect().width;
        function move(ev) { setW(idx, startW + (ev.pageX - startX)); }
        function up() {
          document.removeEventListener('mousemove', move);
          document.removeEventListener('mouseup', up);
          document.body.style.cursor = '';
          var w = {};
          ths.forEach(function (t, i2) { w[i2] = t.getBoundingClientRect().width; });
          save(key, w);
        }
        document.body.style.cursor = 'col-resize';
        document.addEventListener('mousemove', move);
        document.addEventListener('mouseup', up);
      });
    });
  }

  function init() {
    var path = location.pathname;
    Array.prototype.forEach.call(document.querySelectorAll('table.resizable'), function (t, idx) {
      initTable(t, 'sbcol:' + path + ':' + idx);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
