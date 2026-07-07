"""Regression tests for two code-review fixes:
  #1 the customer statement report counts multi-payment invoices (invoice_entry_links), not just
     paid_entry_id/matched_entry_id, so payment rows and the ending balance reconcile.
  #2 invoice/estimate lines persist item_id, linking a line back to the catalog item it came from.
Isolated via SHOPBOOKS_DATA_DIR (set BEFORE importing db/app)."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_reviewfix_")
import db  # noqa: E402
import ledger  # noqa: E402
import invoicing  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ok = lambda c, w: print(("PASS" if c else "FAIL"), w)
client = TestClient(appmod.app)

con = db.connect()
con.execute("INSERT INTO customers(name,email) VALUES('Acme Co','a@b.com')")
cust_id = con.execute("SELECT id FROM customers WHERE name='Acme Co'").fetchone()["id"]
bank = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
inc = con.execute("SELECT id FROM accounts WHERE type='income' LIMIT 1").fetchone()["id"]
con.commit()
con.close()

# ---- #1: an invoice paid by TWO linked partial payments ----
con = db.connect()
num = invoicing.next_number(con)
cur = con.execute(
    "INSERT INTO invoices(number,customer_id,date,due_date,memo,kind) VALUES(?,?,?,?,?,'invoice')",
    (num, cust_id, "2026-01-10", "2026-02-10", ""))
inv_id = cur.lastrowid
con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,?,?)",
            (inv_id, "Widget build", 1, 100000))
con.execute("UPDATE invoices SET status='sent' WHERE id=?", (inv_id,))
e1 = ledger.post_entry(con, "2026-01-20", "Acme partial 1", [(bank, 40000), (inc, -40000)])
e2 = ledger.post_entry(con, "2026-01-28", "Acme partial 2", [(bank, 60000), (inc, -60000)])
appmod._match_invoice_to_entry(con, inv_id, e1)
appmod._match_invoice_to_entry(con, inv_id, e2)
con.commit()

pays = invoicing.invoice_payment_entries(con, inv_id)
ok(len(pays) == 2 and sum(p["amount_cents"] for p in pays) == 100000,
   "invoice_payment_entries returns BOTH linked payments (sum $1000)")
ok(invoicing.invoice_outstanding_balance(con, inv_id) == 0, "invoice reads as fully paid via two links")
con.close()

# statement report must list BOTH payments (before the fix only the matched_entry_id one showed)
r = client.get(f"/customers/{cust_id}/report")
ok(r.status_code == 200, "customer statement renders")
ok(r.text.count("PMT-") == 2, "statement lists both payment rows (not just the last matched one)")

# ---- #2: invoice/estimate lines persist item_id ----
con = db.connect()
con.execute("INSERT INTO items(name,description,unit_cents) VALUES('Deck Board','Cedar 2x6',1250)")
item_id = con.execute("SELECT id FROM items WHERE name='Deck Board'").fetchone()["id"]
con.commit()
con.close()

client.post("/invoices/new", data={
    "customer_id": str(cust_id), "date": "2026-03-01", "due_date": "2026-03-31", "kind": "invoice",
    "item_id": str(item_id), "item_desc": "Cedar 2x6", "item_qty": "3", "item_price": "12.50",
})
con = db.connect()
row = con.execute("SELECT invoice_id, item_id FROM invoice_items WHERE description='Cedar 2x6'").fetchone()
new_inv = row["invoice_id"]
ok(row["item_id"] == item_id, "invoice line persists item_id chosen from the catalog")
con.close()

# editing keeps the linkage
client.post(f"/invoices/{new_inv}/edit", data={
    "customer_id": str(cust_id), "date": "2026-03-01", "due_date": "2026-03-31",
    "item_id": str(item_id), "item_desc": "Cedar 2x6 (updated)", "item_qty": "4", "item_price": "12.50",
})
con = db.connect()
row = con.execute("SELECT item_id, description FROM invoice_items WHERE invoice_id=?", (new_inv,)).fetchone()
ok(row["item_id"] == item_id and row["description"] == "Cedar 2x6 (updated)", "edit preserves item_id")
con.close()

# a manual line (no catalog pick) stores NULL item_id and still posts
client.post("/invoices/new", data={
    "customer_id": str(cust_id), "date": "2026-03-02", "due_date": "2026-03-31", "kind": "invoice",
    "item_id": "", "item_desc": "Custom labor", "item_qty": "1", "item_price": "500",
})
con = db.connect()
row = con.execute("SELECT item_id FROM invoice_items WHERE description='Custom labor'").fetchone()
ok(row and row["item_id"] is None, "a manual line stores NULL item_id (no false linkage)")
con.close()

# estimate -> invoice conversion carries item_id through
client.post("/estimates/new", data={
    "customer_id": str(cust_id), "date": "2026-04-01", "valid_until": "2026-05-01",
    "item_id": str(item_id), "item_desc": "Cedar 2x6", "item_qty": "2", "item_price": "12.50",
})
con = db.connect()
est_id = con.execute("SELECT id FROM invoices WHERE kind='estimate' ORDER BY id DESC LIMIT 1").fetchone()["id"]
con.close()
client.post(f"/estimates/{est_id}/convert")
con = db.connect()
conv = con.execute(
    "SELECT ii.item_id FROM invoice_items ii JOIN invoices i ON i.id=ii.invoice_id "
    "WHERE i.kind='invoice' AND ii.description='Cedar 2x6' ORDER BY ii.id DESC LIMIT 1").fetchone()
ok(conv and conv["item_id"] == item_id, "estimate->invoice conversion carries item_id")
con.close()

print("\nREVIEW FIX TESTS DONE")
