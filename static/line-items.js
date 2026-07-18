/* Shared invoice/estimate line-item editor. The template provides window.standardItems (the catalog,
 * Jinja-rendered), window.incomeAccounts (for the "+ New service" mini-form), and the initial
 * <table id="items"> markup; this file supplies the behavior so invoice_new / invoice_edit /
 * estimate_new don't each carry their own copy:
 *   - syncTax(cb)        mirror the Tax checkbox into the hidden item_taxable input
 *   - onItemSelect(sel)  fill a row's description/price/tax from a chosen catalog item
 *   - addRow()           append a blank line row (adds the delete cell only when the header has one)
 *   - deleteRow(btn)     remove a row, keeping at least one line
 *   - openNewService / createService / cancelNewService  create a catalog service inline (name,
 *                        price, income account) when it isn't in the catalog yet, then fill the line
 * Loaded globally from base.html; harmless (a no-op) on pages without an #items table.
 */
function syncTax(cb) {
  var h = cb.parentNode.querySelector('input[name="item_taxable"]');
  if (h) h.value = cb.checked ? '1' : '0';
}

function onItemSelect(sel) {
  var opt = sel.options[sel.selectedIndex];
  if (!opt || !sel.value) return;
  var row = sel.closest('tr');
  var descInput = row.querySelector('input[name="item_desc"]');
  var priceInput = row.querySelector('input[name="item_price"]');
  if (descInput) descInput.value = opt.getAttribute('data-desc') || '';
  if (priceInput) priceInput.value = opt.getAttribute('data-price') || '';
  var cb = row.querySelector('.tax-check');
  if (cb) { cb.checked = opt.getAttribute('data-taxable') === '1'; syncTax(cb); }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

/* The "+ New service" link that sits under each line's item picker (only when income accounts exist
 * to assign the sale to). */
function newServiceLinkHtml() {
  if (!window.incomeAccounts || !window.incomeAccounts.length) return '';
  return '<a href="javascript:void(0)" class="new-service-link muted" onclick="openNewService(this)"'
    + ' style="font-size:12px">+ New service</a>';
}

function addRow() {
  var t = document.getElementById('items');
  if (!t) return;
  var items = window.standardItems || [];
  var selectHtml = '';
  if (items.length > 0) {
    var optionsHtml = '<option value="">-- Choose a Product/Service --</option>';
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      optionsHtml += '<option value="' + it.id + '" data-desc="' + escapeHtml(it.description)
        + '" data-price="' + it.price + '" data-taxable="' + it.taxable + '">' + escapeHtml(it.name) + '</option>';
    }
    selectHtml = '<select name="item_id" class="item-select" onchange="onItemSelect(this)" style="width:100%; margin-bottom:4px;">'
      + optionsHtml + '</select>';
  }
  var html = '<td>' + selectHtml
      + '<input type="text" name="item_desc" style="width:100%" placeholder="Line description">'
      + newServiceLinkHtml() + '</td>'
    + '<td><input type="number" name="item_qty" value="1" step="0.25" min="0" style="width:70px"></td>'
    + '<td><input type="text" name="item_price" placeholder="0.00" style="width:100px"></td>'
    + '<td style="text-align:center"><input type="checkbox" class="tax-check" onchange="syncTax(this)">'
      + '<input type="hidden" name="item_taxable" value="0"></td>';
  var row = t.insertRow(-1);
  row.innerHTML = html;
  ensureRowControls(row);   // grip + up/down + delete, and drag wiring
}

function deleteRow(btn) {
  var r = btn.parentNode.parentNode;
  if (r.parentNode.rows.length > 2) {
    r.parentNode.removeChild(r);
  } else {
    alert('An invoice or estimate must have at least one line item.');
  }
}

/* ---- reorder (arrows + drag) and blank spacer lines --------------------------------------- */
var SPACER_MARKER = '__SB_SPACER__';   // must match _parse_line_items in routes_invoices.py
var _dragRow = null;

function actionsCellHtml() {
  return '<td class="row-actions" style="white-space:nowrap;text-align:right">'
    + '<span class="drag-grip" title="Drag to reorder" style="cursor:grab;padding:0 6px;user-select:none">⠿</span>'
    + '<button type="button" class="small ghost" title="Move up" onclick="moveRow(this,-1)">▲</button>'
    + '<button type="button" class="small ghost" title="Move down" onclick="moveRow(this,1)">▼</button>'
    + '<button type="button" class="small danger" title="Remove" onclick="deleteRow(this)">✕</button></td>';
}

function ensureRowControls(row) {
  if (!row.querySelector('.row-actions')) row.insertAdjacentHTML('beforeend', actionsCellHtml());
  enableDrag(row);
}

function moveRow(btn, dir) {
  var t = document.getElementById('items'); if (!t) return;
  var row = btn.closest('tr');
  var rows = Array.prototype.slice.call(t.rows, 1);   // skip the header row
  var i = rows.indexOf(row), j = i + dir;
  if (i < 0 || j < 0 || j >= rows.length) return;
  if (dir < 0) row.parentNode.insertBefore(row, rows[j]);
  else row.parentNode.insertBefore(rows[j], row);
}

function enableDrag(row) {
  var grip = row.querySelector('.drag-grip');
  if (!grip || row._dragWired) return;
  row._dragWired = true;
  // only draggable while the grip is held, so text selection in inputs still works
  grip.addEventListener('mousedown', function () { row.setAttribute('draggable', 'true'); });
  grip.addEventListener('mouseup', function () { row.removeAttribute('draggable'); });
  row.addEventListener('dragstart', function (e) {
    _dragRow = row; e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', ''); } catch (_) {}
  });
  row.addEventListener('dragend', function () { row.removeAttribute('draggable'); _dragRow = null; });
  row.addEventListener('dragover', function (e) {
    if (!_dragRow || _dragRow === row || row.rowIndex === 0) return;
    e.preventDefault();
    var rect = row.getBoundingClientRect();
    var after = (e.clientY - rect.top) > rect.height / 2;
    row.parentNode.insertBefore(_dragRow, after ? row.nextSibling : row);
  });
}

function addSpacer() {
  var t = document.getElementById('items'); if (!t) return;
  var row = t.insertRow(-1);
  row.className = 'spacer-row';
  row.innerHTML =
    '<td><em class="muted">— blank spacer line —</em>'
      + '<input type="hidden" name="item_id" value="">'
      + '<input type="hidden" name="item_desc" value="' + SPACER_MARKER + '"></td>'
    + '<td><input type="hidden" name="item_qty" value="0"></td>'
    + '<td><input type="hidden" name="item_price" value="0"></td>'
    + '<td style="text-align:center"><input type="hidden" name="item_taxable" value="0"></td>';
  ensureRowControls(row);
}

/* Inject the reorder column, per-row controls, and the "+ Blank line" button on any page with the
   line-item editor (invoice_new / invoice_edit / estimate_new), so the templates stay minimal. */
function ensureEditorControls() {
  var t = document.getElementById('items'); if (!t) return;
  var header = t.rows[0];
  if (header && !header.querySelector('.row-actions-h')) {
    header.insertAdjacentHTML('beforeend', '<th class="row-actions-h" style="width:132px"></th>');
  }
  for (var i = 1; i < t.rows.length; i++) ensureRowControls(t.rows[i]);
  var addBtn = document.querySelector('button[onclick^="addRow"]');
  if (addBtn && !document.getElementById('addSpacerBtn')) {
    var b = document.createElement('button');
    b.type = 'button'; b.id = 'addSpacerBtn'; b.className = 'ghost';
    b.textContent = '+ Blank line';
    b.title = 'Insert a blank spacer line for legibility';
    b.setAttribute('onclick', 'addSpacer()');
    b.style.marginLeft = '8px';
    addBtn.parentNode.insertBefore(b, addBtn.nextSibling);
  }
}

/* ---- inline "create a service" ------------------------------------------------------------ */
function openNewService(link) {
  var cell = link.parentNode;
  if (cell.querySelector('.new-service-panel')) return;   // already open
  link.style.display = 'none';
  var row = link.closest('tr');
  var curDesc = (row.querySelector('input[name="item_desc"]') || {}).value || '';
  var curPrice = (row.querySelector('input[name="item_price"]') || {}).value || '';
  var opts = '';
  (window.incomeAccounts || []).forEach(function (a) {
    opts += '<option value="' + a.id + '">' + escapeHtml(a.name) + '</option>';
  });
  var panel = document.createElement('div');
  panel.className = 'new-service-panel';
  panel.style.cssText = 'margin-top:6px;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--card-2)';
  panel.innerHTML =
    '<div style="font-size:12px;font-weight:600;margin-bottom:4px">New service &mdash; saved to Products &amp; Services</div>'
    + '<input class="ns-name" type="text" placeholder="Service name" style="width:100%;margin-bottom:4px" value="' + escapeHtml(curDesc) + '">'
    + '<input class="ns-price" type="text" placeholder="Price (0.00)" style="width:100%;margin-bottom:4px" value="' + escapeHtml(curPrice) + '">'
    + '<select class="ns-acct" style="width:100%;margin-bottom:6px"><option value="">&mdash; Income account it posts to &mdash;</option>' + opts + '</select>'
    + '<div class="toolbar" style="gap:8px;align-items:center">'
    + '<button type="button" class="small" onclick="createService(this)">Create &amp; use</button>'
    + '<button type="button" class="small ghost" onclick="cancelNewService(this)">Cancel</button>'
    + '<span class="ns-err error" style="font-size:12px"></span></div>';
  cell.appendChild(panel);
  panel.querySelector('.ns-name').focus();
}

function cancelNewService(btn) {
  var panel = btn.closest('.new-service-panel');
  var cell = panel.parentNode;
  panel.remove();
  var link = cell.querySelector('.new-service-link');
  if (link) link.style.display = '';
}

function createService(btn) {
  var panel = btn.closest('.new-service-panel');
  var name = panel.querySelector('.ns-name').value.trim();
  var price = panel.querySelector('.ns-price').value.trim();
  var acct = panel.querySelector('.ns-acct').value;
  var err = panel.querySelector('.ns-err');
  err.textContent = '';
  if (!name) { err.textContent = 'Enter a name.'; return; }
  if (!acct) { err.textContent = 'Choose an income account.'; return; }
  btn.disabled = true;
  var body = new URLSearchParams();
  body.set('name', name);
  body.set('unit_price', price || '0');
  body.set('income_account_id', acct);
  body.set('description', name);
  fetch('/items/quick-create', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: body.toString()
  }).then(function (r) {
    return r.json().then(function (j) { return { ok: r.ok, j: j }; });
  }).then(function (res) {
    btn.disabled = false;
    if (!res.ok) { err.textContent = (res.j && res.j.error) || 'Could not create the service.'; return; }
    var it = res.j;
    window.standardItems = window.standardItems || [];
    window.standardItems.push({ id: it.id, name: it.name, description: it.description, price: it.price, taxable: it.taxable });
    // add the new item to every line's picker so it can be reused on other lines
    document.querySelectorAll('select.item-select').forEach(function (sel) {
      var o = document.createElement('option');
      o.value = it.id; o.textContent = it.name;
      o.setAttribute('data-desc', it.description || '');
      o.setAttribute('data-price', it.price || '');
      o.setAttribute('data-taxable', it.taxable || 0);
      sel.appendChild(o);
    });
    // select + autofill on this row (or fill fields directly if the row has no picker yet)
    var row = panel.closest('tr');
    var sel = row.querySelector('select.item-select');
    if (sel) { sel.value = it.id; onItemSelect(sel); }
    else {
      var d = row.querySelector('input[name="item_desc"]'); if (d) d.value = it.description || it.name;
      var p = row.querySelector('input[name="item_price"]'); if (p) p.value = it.price || '';
    }
    cancelNewService(btn);
  }).catch(function () {
    btn.disabled = false; err.textContent = 'Network error — try again.';
  });
}

/* Add the "+ New service" link to the rows already on the page at load. */
document.addEventListener('DOMContentLoaded', function () {
  var t = document.getElementById('items');
  if (!t) return;
  ensureEditorControls();
  if (window.incomeAccounts && window.incomeAccounts.length) {
    for (var i = 1; i < t.rows.length; i++) {   // row 0 is the header
      var tr = t.rows[i];
      if (tr.classList.contains('spacer-row')) continue;
      var cell = tr.cells[0];
      if (cell && !cell.querySelector('.new-service-link')) {
        cell.insertAdjacentHTML('beforeend', newServiceLinkHtml());
      }
    }
  }
});
