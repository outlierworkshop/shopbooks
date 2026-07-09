"""Tests for estimates / quotes (#35). Estimates are invoices rows with kind='estimate' — they never
post to the ledger or match deposits, and an accepted one converts into a real invoice (copying items).
Isolated via SHOPBOOKS_DATA_DIR; exercised over HTTP with TestClient.
"""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_esttest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db          # noqa: E402
import invoicing   # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed

db.init()
con = db.connect()
con.execute("INSERT INTO customers(name,email,address) VALUES('Acme Co','a@acme.test','1 Way')")
con.commit()
cust = con.execute("SELECT id FROM customers").fetchone()["id"]
con.close()

from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402
c = TestClient(app.app)

# --- create an estimate (two line items) -------------------------------------
r = c.post("/estimates/new", data={
    "customer_id": cust, "date": "2026-03-01", "valid_until": "2026-03-31", "memo": "Proposed build",
    "item_desc": ["Design", "Materials"], "item_qty": ["10", "1"], "item_price": ["100.00", "250.00"],
}, follow_redirects=False)
ok(r.status_code == 303 and r.headers["location"].startswith("/estimates/"), "creating an estimate redirects to its page")
est_id = int(r.headers["location"].rsplit("/", 1)[1])

con = db.connect()
est = con.execute("SELECT * FROM invoices WHERE id=?", (est_id,)).fetchone()
ok(est["kind"] == "estimate", "stored with kind='estimate'")
ok(est["number"].startswith("EST-"), "estimate gets an EST- number")
ok(invoicing.invoice_total(con, est_id) == 125000, "estimate total = 10*100 + 250 = $1,250")

# --- estimates DON'T appear among invoices, and don't post to the ledger ------
ok(app._invoice_rows(con) == [] or all(i["kind"] == "invoice" for i in app._invoice_rows(con)),
   "estimates never show up in the invoice list")
ok(con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0, "creating an estimate posts nothing to the ledger")
con.close()

# --- the estimate page renders; the invoice list/AR helpers ignore it ---------
ok(c.get(f"/estimates/{est_id}").status_code == 200, "estimate view renders")
ok("ESTIMATE" in c.get(f"/estimates/{est_id}").text, "estimate view shows the ESTIMATE heading")
ok(c.get(f"/estimates/{est_id}/pdf").status_code == 200, "estimate PDF renders")
# visiting it as an invoice bounces back to the estimate view
ir = c.get(f"/invoices/{est_id}", follow_redirects=False)
ok(ir.status_code == 303 and ir.headers["location"] == f"/estimates/{est_id}",
   "an estimate id under /invoices redirects to the estimate view")

# --- convert to an invoice ----------------------------------------------------
r2 = c.post(f"/estimates/{est_id}/convert", follow_redirects=False)
ok(r2.status_code == 303 and r2.headers["location"].startswith("/invoices/"), "convert redirects to the new invoice")
inv_id = int(r2.headers["location"].split("/invoices/")[1].split("?")[0])

con = db.connect()
inv = con.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
est = con.execute("SELECT * FROM invoices WHERE id=?", (est_id,)).fetchone()
ok(inv["kind"] == "invoice" and inv["number"].startswith("INV-"), "conversion creates a real INV- invoice")
ok(invoicing.invoice_total(con, inv_id) == 125000, "line items copied (same $1,250 total)")
ok(est["status"] == "accepted" and est["converted_invoice_id"] == inv_id,
   "the estimate is marked accepted and linked to the invoice it became")
n_items = con.execute("SELECT COUNT(*) c FROM invoice_items WHERE invoice_id=?", (inv_id,)).fetchone()["c"]
ok(n_items == 2, "both line items were copied to the invoice")
ok(any(i["id"] == inv_id for i in app._invoice_rows(con)), "the converted invoice now appears in the invoice list")
con.close()

# --- converting again just returns the existing invoice (no duplicate) --------
r3 = c.post(f"/estimates/{est_id}/convert", follow_redirects=False)
ok(r3.headers["location"] == f"/invoices/{inv_id}", "re-converting returns the existing invoice, doesn't duplicate")
con = db.connect()
ok(con.execute("SELECT COUNT(*) c FROM invoices WHERE kind='invoice'").fetchone()["c"] == 1,
   "still exactly one invoice after a second convert attempt")
con.close()

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nESTIMATE TESTS DONE")
