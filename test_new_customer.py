"""New invoices/estimates can bill a customer who isn't on file yet: the create form takes a new
customer name (+ optional email) and creates the customer inline. Covers both invoices and
estimates, the inline-error path, and that the existing-customer path still works. Isolated via
SHOPBOOKS_DATA_DIR so it never touches real books."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_newcust_")

import db  # noqa: E402
db.init()
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
client = TestClient(appmod.app)


def con():
    return db.connect()


# ---- the new-invoice form exposes new-customer fields even with zero customers on file ----
r = client.get("/invoices/new")
ok(r.status_code == 200, "GET /invoices/new renders")
ok('name="new_customer_name"' in r.text, "invoice form has a new-customer name field")
ok('name="new_customer_email"' in r.text, "invoice form has a new-customer email field")
ok("Add a customer on the" not in r.text, "old 'add a customer first' gate is gone")

# ---- create an invoice for a brand-new customer (no customer_id) ----
before = con().execute("SELECT COUNT(*) FROM customers").fetchone()[0]
r = client.post("/invoices/new", data={
    "kind": "invoice", "date": "2026-07-12", "due_date": "2026-07-26", "memo": "",
    "customer_id": "", "new_customer_name": "Jane Luthier", "new_customer_email": "jane@example.com",
    "item_desc": "Fret dress", "item_qty": "1", "item_price": "120.00", "item_taxable": "0",
}, follow_redirects=False)
ok(r.status_code == 303, f"new-customer invoice creates + redirects (got {r.status_code})")
cust = con().execute("SELECT email FROM customers WHERE name='Jane Luthier'").fetchone()
ok(cust is not None, "the new customer was created")
ok(cust and cust[0] == "jane@example.com", "the new customer's email was saved")
ok(con().execute("SELECT COUNT(*) FROM customers").fetchone()[0] == before + 1, "exactly one customer created")
ok(con().execute("SELECT 1 FROM invoices i JOIN customers c ON c.id=i.customer_id "
                 "WHERE c.name='Jane Luthier'").fetchone() is not None, "invoice linked to the new customer")

# ---- neither an existing customer nor a new name -> friendly inline error, no crash ----
r = client.post("/invoices/new", data={
    "kind": "invoice", "date": "2026-07-12", "due_date": "2026-07-26",
    "customer_id": "", "new_customer_name": "",
    "item_desc": "x", "item_qty": "1", "item_price": "5.00", "item_taxable": "0",
}, follow_redirects=False)
ok(r.status_code == 200 and "enter a new customer" in r.text.lower(),
   "no customer chosen or entered -> inline error, no crash")

# ---- the existing-customer path still works ----
cid = con().execute("SELECT id FROM customers WHERE name='Jane Luthier'").fetchone()[0]
r = client.post("/invoices/new", data={
    "kind": "invoice", "date": "2026-07-12", "due_date": "2026-07-26",
    "customer_id": str(cid), "new_customer_name": "",
    "item_desc": "Setup", "item_qty": "1", "item_price": "80.00", "item_taxable": "0",
}, follow_redirects=False)
ok(r.status_code == 303, "existing-customer invoice still works")

# ---- estimates take the same new-customer path ----
r = client.get("/estimates/new")
ok('name="new_customer_name"' in r.text, "estimate form has new-customer fields")
r = client.post("/estimates/new", data={
    "date": "2026-07-12", "valid_until": "2026-07-26", "memo": "",
    "customer_id": "", "new_customer_name": "Bob Builder", "new_customer_email": "bob@example.com",
    "item_desc": "Deck quote", "item_qty": "1", "item_price": "500.00", "item_taxable": "0",
}, follow_redirects=False)
ok(r.status_code == 303, "new-customer estimate creates + redirects")
ok(con().execute("SELECT 1 FROM customers WHERE name='Bob Builder'").fetchone() is not None,
   "estimate created the new customer")

print("\nNEW CUSTOMER (invoices + estimates) TESTS DONE")
