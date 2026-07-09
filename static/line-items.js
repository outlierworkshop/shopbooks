/* Shared invoice/estimate line-item editor. The template provides window.standardItems (the catalog,
 * Jinja-rendered) and the initial <table id="items"> markup; this file supplies the behavior so
 * invoice_new / invoice_edit / estimate_new don't each carry their own copy:
 *   - syncTax(cb)        mirror the Tax checkbox into the hidden item_taxable input
 *   - onItemSelect(sel)  fill a row's description/price/tax from a chosen catalog item
 *   - addRow()           append a blank line row, matching the table's columns (adds the delete cell
 *                        only when the header has one — i.e. the edit page)
 *   - deleteRow(btn)     remove a row, keeping at least one line
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
      + '<input type="text" name="item_desc" style="width:100%" placeholder="Line description"></td>'
    + '<td><input type="number" name="item_qty" value="1" step="0.25" min="0" style="width:70px"></td>'
    + '<td><input type="text" name="item_price" placeholder="0.00" style="width:100px"></td>'
    + '<td style="text-align:center"><input type="checkbox" class="tax-check" onchange="syncTax(this)">'
      + '<input type="hidden" name="item_taxable" value="0"></td>';
  // The edit page carries a trailing delete-button column in its header; match it on new rows.
  var header = t.rows[0];
  if (header && header.cells.length > 4) {
    html += '<td><button type="button" class="small danger" onclick="deleteRow(this)">✕</button></td>';
  }
  t.insertRow(-1).innerHTML = html;
}

function deleteRow(btn) {
  var r = btn.parentNode.parentNode;
  if (r.parentNode.rows.length > 2) {
    r.parentNode.removeChild(r);
  } else {
    alert('An invoice or estimate must have at least one line item.');
  }
}
