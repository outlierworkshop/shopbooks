"""The line-item editor's running total (invoice_new / invoice_edit / estimate_new / estimate edit).

The total itself is computed in the browser (static/line-items.js) so it updates as you type, but it
must never disagree with the total the document is actually saved with. That contract has two halves,
and this file pins both:

  1. The page gives the script what it needs — `window.salesTaxRate` from the same setting
     `invoicing.sales_tax_rate` reads, and the editor script itself.
  2. The server-side arithmetic the JS mirrors stays put:
         line  = round(qty * unit_cents)                 -- SQLite round(), half away from zero
         tax   = round(taxable_subtotal * rate / 100)    -- Python round(), half to EVEN
         total = subtotal + tax
     The tax case is the subtle one: 63000 * 6.25% = 3937.5 exactly, and Python's banker's rounding
     makes that 3938, not 3937. line-items.js implements roundHalfEven() to match. If anyone changes
     the server's rounding, these assertions fail and the JS needs the same change.

Isolated via SHOPBOOKS_DATA_DIR before importing db/app."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_totals_")

import db  # noqa: E402
import invoicing  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
from testutil import ok  # noqa: E402

db.init()
con = db.connect()
db.set_setting(con, "sales_tax_rate", "6.25")
con.execute("INSERT INTO customers(name,email) VALUES('Quote Co','q@test.local')")
cust = con.execute("SELECT id FROM customers").fetchone()["id"]
con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,memo,kind) "
            "VALUES('EST-9000',?,'2026-08-01','2026-08-31','draft','Scroll job','estimate')", (cust,))
est = con.execute("SELECT id FROM invoices WHERE number='EST-9000'").fetchone()["id"]
for desc, qty, unit, taxable in [("Design", 1, 50000, 0), ("Milling", 6, 10000, 1), ("Materials", 1, 3000, 1)]:
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,taxable) VALUES(?,?,?,?,?)",
                (est, desc, qty, unit, taxable))
con.commit()

# --- 2. the arithmetic the browser mirrors ------------------------------------------------------
ok(invoicing.invoice_subtotal(con, est) == 113000, "subtotal = 50000 + 6*10000 + 3000")
ok(invoicing.invoice_tax(con, est) == 3938,
   "tax on 63000 @6.25% = 3937.5 -> 3938 (Python's round is half-to-EVEN; JS mirrors it)")
ok(invoicing.invoice_total(con, est) == 116938, "total = subtotal + tax")

# a second banker's-rounding case, in the other direction: 3000 @6.25% = 187.5 -> 188
con.execute("UPDATE invoice_items SET taxable=0 WHERE description='Milling' AND invoice_id=?", (est,))
con.commit()
ok(invoicing.invoice_tax(con, est) == 188, "tax on 3000 @6.25% = 187.5 -> 188, matching roundHalfEven()")
con.execute("UPDATE invoice_items SET taxable=1 WHERE description='Milling' AND invoice_id=?", (est,))
con.commit()
con.close()

# --- 1. the page hands the script what it needs -------------------------------------------------
client = TestClient(appmod.app)
page = client.get(f"/estimates/{est}/edit")
ok(page.status_code == 200, "the estimate edit page renders")
ok("window.salesTaxRate = 6.25" in page.text,
   "the page exposes the configured sales-tax rate to the editor script")
ok("line-items.js" in page.text, "the edit page loads the shared line-item editor")
ok('id="items"' in page.text, "the edit page has the line-item table the total is computed from")

# the same editor (and therefore the same running total) backs the other line-item pages
for path in (f"/invoices/new", "/estimates/new"):
    r = client.get(path)
    ok(r.status_code == 200 and "line-items.js" in r.text and "window.salesTaxRate" in r.text,
       f"{path} also gets the running total")

# with no sales tax configured the rate is 0 -- the script then shows just the line-items total
con = db.connect()
db.set_setting(con, "sales_tax_rate", "0")
con.commit()
con.close()
ok("window.salesTaxRate = 0" in client.get(f"/estimates/{est}/edit").text,
   "a blank/zero tax rate reaches the script as 0 (no tax line shown)")

# --- the script actually implements the mirrored arithmetic -------------------------------------
js = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "line-items.js"),
          encoding="utf-8").read()
for fn in ("computeTotals", "roundHalfEven", "parseMoneyCents", "updateTotals"):
    ok(fn in js, f"line-items.js defines {fn}()")
ok("spacer-row" in js.split("function computeTotals")[1][:900],
   "computeTotals skips blank spacer lines (they carry no money)")

print("\nLINE ITEM TOTALS TESTS DONE")
