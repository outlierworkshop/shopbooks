"""Invoice/estimate line items: submitted order is preserved, and a blank spacer line (the
__SB_SPACER__ sentinel) is stored as an empty-description zero row, excluded from totals, rendered
as a gap, and round-tripped through the edit form. Isolated via SHOPBOOKS_DATA_DIR."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="sb_lineitems_")

import db  # noqa: E402
db.init()
import app as appmod  # noqa: E402
import invoicing  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # noqa: E402
client = TestClient(appmod.app)


def con():
    return db.connect()


c = con()
db.set_setting(c, "sales_tax_rate", "10")
c.execute("INSERT INTO customers(name, email) VALUES('Marcus', 'm@example.com')")
c.commit()
cid = c.execute("SELECT id FROM customers").fetchone()[0]

SPACER = ("__SB_SPACER__", "0", "0", "0")


def lines(*specs):
    """httpx posts repeated form keys as dict-with-list values; keep the per-field lists aligned."""
    return {
        "item_desc": [s[0] for s in specs],
        "item_qty": [s[1] for s in specs],
        "item_price": [s[2] for s in specs],
        "item_taxable": [s[3] for s in specs],
        "item_id": ["" for _ in specs],
    }


# ---- create: Item A, [blank], Item B, Item C ----
data = {"kind": "invoice", "date": "2026-07-16", "due_date": "2026-07-30", "memo": "",
        "customer_id": str(cid),
        **lines(("Item A", "1", "100.00", "1"), SPACER, ("Item B", "2", "50.00", "0"), ("Item C", "1", "25.00", "1"))}
r = client.post("/invoices/new", data=data, follow_redirects=False)
ok(r.status_code == 303, f"invoice with a spacer line creates (got {r.status_code})")
iid = con().execute("SELECT id FROM invoices ORDER BY id DESC LIMIT 1").fetchone()[0]

inv, items, total = invoicing.get_invoice(con(), iid)
ok([it["description"] for it in items] == ["Item A", "", "Item B", "Item C"],
   "line items are stored in submitted order, with the blank spacer in place")
ok(items[1]["description"] == "" and items[1]["qty"] == 0 and items[1]["unit_cents"] == 0,
   "the spacer is an empty-description, zero-qty, zero-price row")

# ---- the spacer contributes nothing to money ----
sub = invoicing.invoice_subtotal(con(), iid)
ok(sub == 22500, f"subtotal excludes the spacer (100 + 100 + 25 = 225.00; got {sub})")
ok(total == 23750, f"total = subtotal + 10% tax on the taxable lines (237.50; got {total})")

# ---- it renders everywhere without choking ----
ok(invoicing.render_pdf(con(), inv, items, total)[:4] == b"%PDF", "PDF renders with a spacer line")
r = client.get(f"/invoices/{iid}")
ok(r.status_code == 200 and "spacer-row" in r.text, "invoice view shows the blank spacer row")

# ---- reorder + spacer survive an edit (Item C, [blank], Item A, Item B) ----
edit = {"customer_id": str(cid), "date": "2026-07-16", "due_date": "2026-07-30", "memo": "",
        **lines(("Item C", "1", "25.00", "1"), SPACER, ("Item A", "1", "100.00", "1"), ("Item B", "2", "50.00", "0"))}
r = client.post(f"/invoices/{iid}/edit", data=edit, follow_redirects=False)
ok(r.status_code == 303, "edit saves the reordered lines")
_, items2, _ = invoicing.get_invoice(con(), iid)
ok([it["description"] for it in items2] == ["Item C", "", "Item A", "Item B"],
   "the edited order is preserved (reordering works) and the spacer stays")

# ---- the edit form re-renders the spacer via its sentinel so it round-trips ----
r = client.get(f"/invoices/{iid}/edit")
ok(r.status_code == 200 and 'value="__SB_SPACER__"' in r.text,
   "edit form re-emits the spacer sentinel for a clean round-trip")

# ---- a genuinely empty row (no sentinel) is still dropped ----
data = {"kind": "invoice", "date": "2026-07-16", "due_date": "2026-07-30", "memo": "",
        "customer_id": str(cid), **lines(("Real line", "1", "10.00", "0"), ("", "1", "", "0"))}
client.post("/invoices/new", data=data, follow_redirects=False)
iid2 = con().execute("SELECT id FROM invoices ORDER BY id DESC LIMIT 1").fetchone()[0]
_, items3, _ = invoicing.get_invoice(con(), iid2)
ok([it["description"] for it in items3] == ["Real line"], "an accidental blank row is still skipped")

print("\nLINE ITEMS (reorder + spacer) TESTS DONE")
