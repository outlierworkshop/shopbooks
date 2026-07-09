"""Sales tax: taxable flag on items + invoice lines, tax-inclusive totals, and payment splitting
collected tax into the Sales Tax Payable liability. Isolated via SHOPBOOKS_DATA_DIR."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_salestax_")
import db  # noqa: E402
import ledger  # noqa: E402
import invoicing  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
client = TestClient(appmod.app)

con = db.connect()
db.set_setting(con, "sales_tax_rate", "10")  # 10% for easy math
con.commit()
tax_acct = con.execute("SELECT id FROM accounts WHERE name='Sales Tax Payable'").fetchone()
ok(tax_acct is not None, "Sales Tax Payable liability account exists (seeded/ensured)")
tax_acct_id = tax_acct["id"]
con.execute("INSERT INTO customers(name) VALUES('Taxable Co')")
cust = con.execute("SELECT id FROM customers WHERE name='Taxable Co'").fetchone()["id"]
bank = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
inc = con.execute("SELECT id FROM accounts WHERE type='income' LIMIT 1").fetchone()["id"]
con.commit()
con.close()

# ---- tax_allocation math ----
ok(invoicing.tax_allocation(15000, 1000, 16000) == (15000, 1000), "full payment splits into subtotal + tax")
ok(invoicing.tax_allocation(15000, 1000, 8000) == (7500, 500), "partial payment splits proportionally")
ok(invoicing.tax_allocation(15000, 0, 15000) == (15000, 0), "no tax -> all income")

# ---- item taxable persists via the form ----
client.post("/items", data={"name": "Widget", "unit_price": "100.00", "taxable": "1"})
client.post("/items", data={"name": "Consulting", "unit_price": "50.00"})  # no taxable
con = db.connect()
widget = con.execute("SELECT id, taxable FROM items WHERE name='Widget'").fetchone()
service = con.execute("SELECT id, taxable FROM items WHERE name='Consulting'").fetchone()
con.close()
ok(widget["taxable"] == 1, "item created as taxable")
ok(service["taxable"] == 0, "item created as non-taxable by default")

# ---- invoice with one taxable + one non-taxable line ----
client.post("/invoices/new", data={
    "customer_id": str(cust), "date": "2026-06-01", "due_date": "2026-07-01", "kind": "invoice",
    "item_id": [str(widget["id"]), str(service["id"])],
    "item_desc": ["Widget", "Consulting"],
    "item_qty": ["1", "1"],
    "item_price": ["100.00", "50.00"],
    "item_taxable": ["1", "0"],
})
con = db.connect()
inv_id = con.execute("SELECT id FROM invoices WHERE customer_id=? ORDER BY id DESC LIMIT 1", (cust,)).fetchone()["id"]
lines = con.execute("SELECT description, taxable FROM invoice_items WHERE invoice_id=? ORDER BY id", (inv_id,)).fetchall()
ok(lines[0]["taxable"] == 1 and lines[1]["taxable"] == 0, "invoice lines persist their taxable flags")

sub = invoicing.invoice_subtotal(con, inv_id)
tax = invoicing.invoice_tax(con, inv_id)
tot = invoicing.invoice_total(con, inv_id)
ok(sub == 15000, f"subtotal is $150 (got {sub})")
ok(tax == 1000, f"tax is 10% of the $100 taxable line = $10 (got {tax})")
ok(tot == 16000, f"total is tax-inclusive $160 (got {tot})")
con.close()

# ---- record full payment: splits income vs sales tax payable ----
client.post(f"/invoices/{inv_id}/pay", data={"paid_date": "2026-06-05", "bank_id": str(bank), "income_id": str(inc)})
con = db.connect()
row = con.execute("SELECT status, paid_entry_id FROM invoices WHERE id=?", (inv_id,)).fetchone()
legs = {r["account_id"]: r["amount_cents"] for r in
        con.execute("SELECT account_id, amount_cents FROM splits WHERE entry_id=?", (row["paid_entry_id"],)).fetchall()}
ok(row["status"] == "paid", "invoice marked paid")
ok(legs.get(bank) == 16000, "bank debited the full $160 received")
ok(legs.get(inc) == -15000, "income credited only the $150 subtotal")
ok(legs.get(tax_acct_id) == -1000, "the $10 tax booked to Sales Tax Payable (liability), not income")
ok(sum(legs.values()) == 0, "payment entry balances (zero-sum)")

ok(invoicing.invoice_payments_total(con, inv_id) == 16000, "payments_total counts income + tax = full $160")
ok(invoicing.invoice_outstanding_balance(con, inv_id) == 0, "nothing outstanding")
# liability display balance = what you owe the state
owed = ledger.display_balance("liability", ledger.raw_balance(con, tax_acct_id))
ok(owed == 1000, f"Sales Tax Payable shows $10 owed (got {owed})")
con.close()

# ---- a non-taxed invoice still books entirely to income (unchanged behavior) ----
client.post("/invoices/new", data={
    "customer_id": str(cust), "date": "2026-06-10", "due_date": "2026-07-10", "kind": "invoice",
    "item_id": [""], "item_desc": ["Plain job"], "item_qty": ["1"], "item_price": ["200.00"], "item_taxable": ["0"],
})
con = db.connect()
inv2 = con.execute("SELECT id FROM invoices WHERE customer_id=? ORDER BY id DESC LIMIT 1", (cust,)).fetchone()["id"]
con.close()
client.post(f"/invoices/{inv2}/pay", data={"paid_date": "2026-06-11", "bank_id": str(bank), "income_id": str(inc)})
con = db.connect()
pe = con.execute("SELECT paid_entry_id FROM invoices WHERE id=?", (inv2,)).fetchone()["paid_entry_id"]
legs2 = {r["account_id"]: r["amount_cents"] for r in
         con.execute("SELECT account_id, amount_cents FROM splits WHERE entry_id=?", (pe,)).fetchall()}
ok(legs2.get(inc) == -20000 and tax_acct_id not in legs2, "no-tax invoice books all $200 to income, no tax leg")

bad = con.execute("SELECT entry_id, SUM(amount_cents) t FROM splits GROUP BY entry_id HAVING t!=0").fetchall()
ok(not bad, "every entry balances (zero-sum)")
con.close()

print("\nSALES TAX TESTS DONE")
