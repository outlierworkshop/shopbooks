"""A recorded invoice payment credits the income accounts the INVOICE's own lines post to, rather
than one account picked by hand at payment time.

Each line's account comes from its catalog item (items.income_account_id); a line typed freehand
falls back to the `income_id` passed in (the picker, still there for exactly that case). Shares are
proportional to line amounts, allocated so they sum EXACTLY to the income portion — the entry has to
balance to the cent, on partial payments too.

Isolated via SHOPBOOKS_DATA_DIR before importing db."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_incsplit_")

import db  # noqa: E402
import invoicing  # noqa: E402
from testutil import ok  # noqa: E402

db.init()
con = db.connect()

bank = con.execute("SELECT id FROM accounts WHERE kind='bank' LIMIT 1").fetchone()["id"]
fab = con.execute("INSERT INTO accounts(name,kind,type,active) VALUES('Fabrication','category','income',1)").lastrowid
dsn = con.execute("INSERT INTO accounts(name,kind,type,active) VALUES('Design Income','category','income',1)").lastrowid
shp = con.execute("INSERT INTO accounts(name,kind,type,active) VALUES('Shipping Income','category','income',1)").lastrowid
misc = con.execute("INSERT INTO accounts(name,kind,type,active) VALUES('Misc Income','category','income',1)").lastrowid
i_fab = con.execute("INSERT INTO items(name,unit_cents,income_account_id,active) VALUES('Milling',0,?,1)", (fab,)).lastrowid
i_dsn = con.execute("INSERT INTO items(name,unit_cents,income_account_id,active) VALUES('Modeling',0,?,1)", (dsn,)).lastrowid
i_shp = con.execute("INSERT INTO items(name,unit_cents,income_account_id,active) VALUES('Postage',0,?,1)", (shp,)).lastrowid
con.execute("INSERT INTO customers(name,email) VALUES('Cust','c@t.local')")
cust = con.execute("SELECT id FROM customers").fetchone()["id"]
con.commit()

n = [2000]


def make_invoice(lines, taxable=False):
    """lines = [(item_id|None, qty, unit_cents)]"""
    n[0] += 1
    num = f"INV-{n[0]}"
    con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,memo,kind) "
                "VALUES(?,?,'2026-08-01','2026-08-31','sent','','invoice')", (num, cust))
    iid = con.execute("SELECT id FROM invoices WHERE number=?", (num,)).fetchone()["id"]
    for item_id, qty, unit in lines:
        con.execute("INSERT INTO invoice_items(invoice_id,item_id,description,qty,unit_cents,taxable) "
                    "VALUES(?,?,?,?,?,?)", (iid, item_id, "line", qty, unit, 1 if taxable else 0))
    con.commit()
    return iid


def legs_by_account(entry_id):
    return {r["account_id"]: r["amount_cents"] for r in
            con.execute("SELECT account_id, amount_cents FROM splits WHERE entry_id=?", (entry_id,))}


def unbalanced():
    return con.execute("SELECT COUNT(*) FROM (SELECT entry_id FROM splits GROUP BY entry_id "
                       "HAVING SUM(amount_cents)!=0)").fetchone()[0]


# --- the headline case: Ben's real shape — $2,200 Fabrication + $750 Design ----------------------
inv = make_invoice([(i_fab, 1, 200000), (i_fab, 1, 20000), (i_dsn, 1, 75000)])
split = invoicing.invoice_income_split(con, inv, 295000, misc)
ok(dict(split) == {fab: 220000, dsn: 75000},
   "income splits across the invoice's own accounts ($2,200 Fabrication / $750 Design)")
ok(sum(c for _, c in split) == 295000, "the split sums exactly to the income amount")

eid = invoicing.record_invoice_payment(con, inv, into_account_id=bank, income_id=misc,
                                       amount_cents=295000, date="2026-08-02")
con.commit()
legs = legs_by_account(eid)
ok(legs[bank] == 295000, "the bank is debited the full payment")
ok(legs[fab] == -220000 and legs[dsn] == -75000, "both income accounts are credited their share")
ok(misc not in legs, "the fallback account isn't touched when every line names its own")
ok(unbalanced() == 0, "the ledger stays balanced")

# --- a PARTIAL payment splits in the same proportions -------------------------------------------
inv2 = make_invoice([(i_fab, 1, 200000), (i_dsn, 1, 100000)])          # 2:1
e2 = invoicing.record_invoice_payment(con, inv2, into_account_id=bank, income_id=misc,
                                      amount_cents=150000, date="2026-08-02")
con.commit()
l2 = legs_by_account(e2)
ok(l2[fab] == -100000 and l2[dsn] == -50000, "a partial payment splits 2:1 like the invoice")
ok(-(l2[fab] + l2[dsn]) == 150000, "the partial's income legs sum to the payment")
ok(con.execute("SELECT status FROM invoices WHERE id=?", (inv2,)).fetchone()["status"] == "partially_paid",
   "a partial still marks the invoice partially paid")
ok(unbalanced() == 0, "ledger balanced after the partial")

# --- freehand lines (no catalog item) fall back to the picked account ---------------------------
inv3 = make_invoice([(i_fab, 1, 100000), (None, 1, 100000)])
s3 = dict(invoicing.invoice_income_split(con, inv3, 200000, misc))
ok(s3 == {fab: 100000, misc: 100000}, "a line with no item falls back to the chosen account")

inv4 = make_invoice([(None, 1, 50000), (None, 1, 25000)])
ok(dict(invoicing.invoice_income_split(con, inv4, 75000, misc)) == {misc: 75000},
   "an invoice with no catalog items behaves exactly as before (one account)")

# --- exact-cent allocation when the proportions don't divide evenly -----------------------------
inv5 = make_invoice([(i_fab, 1, 1), (i_dsn, 1, 1), (i_shp, 1, 1)])     # 1/3 each of 1000 cents
s5 = invoicing.invoice_income_split(con, inv5, 1000, misc)
ok(sum(c for _, c in s5) == 1000, "an uneven 3-way split still sums to the exact cent")
ok(sorted(c for _, c in s5) == [333, 333, 334], "the leftover cent lands on one account, not lost")

# --- sales tax still goes to Sales Tax Payable, income split under it ---------------------------
db.set_setting(con, "sales_tax_rate", "6.25")
con.commit()
inv6 = make_invoice([(i_fab, 1, 100000), (i_dsn, 1, 100000)], taxable=True)
total6 = invoicing.invoice_total(con, inv6)
e6 = invoicing.record_invoice_payment(con, inv6, into_account_id=bank, income_id=misc,
                                      amount_cents=total6, date="2026-08-02")
con.commit()
l6 = legs_by_account(e6)
tax_acct = invoicing.sales_tax_account_id(con)
ok(l6[tax_acct] == -invoicing.invoice_tax(con, inv6), "collected sales tax still lands in Sales Tax Payable")
ok(l6[fab] == -100000 and l6[dsn] == -100000, "the pre-tax income splits across both accounts")
ok(l6[bank] == total6, "the bank gets the full tax-inclusive payment")
ok(unbalanced() == 0, "ledger balanced with tax + a split income side")

# --- a PROGRESS invoice inherits its parent estimate's accounts ---------------------------------
# Billing a portion of an estimate creates ONE summary line with no catalog item (by design), so the
# progress invoice names no accounts of its own. It must split like the job it's billing against.
db.set_setting(con, "sales_tax_rate", "0")
con.commit()
n[0] += 1
con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,memo,kind) "
            "VALUES('EST-7000',?,'2026-08-01','2026-08-31','accepted','job','estimate')", (cust,))
est = con.execute("SELECT id FROM invoices WHERE number='EST-7000'").fetchone()["id"]
for item_id, qty, unit in [(i_fab, 1, 200000), (i_dsn, 1, 100000)]:      # 2/3 Fabrication, 1/3 Design
    con.execute("INSERT INTO invoice_items(invoice_id,item_id,description,qty,unit_cents,taxable) "
                "VALUES(?,?,'line',?,?,0)", (est, item_id, qty, unit))
# the progress invoice: one itemless summary line for 50% of the job, linked back to the estimate
con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,memo,kind,estimate_id) "
            "VALUES('INV-7001',?,'2026-08-01','2026-08-31','sent','','invoice',?)", (cust, est))
prog = con.execute("SELECT id FROM invoices WHERE number='INV-7001'").fetchone()["id"]
con.execute("INSERT INTO invoice_items(invoice_id,item_id,description,qty,unit_cents,taxable) "
            "VALUES(?,NULL,'50% of estimate EST-7000',1,150000,0)", (prog,))
con.commit()

sp = dict(invoicing.invoice_income_split(con, prog, 150000, misc))
ok(sp == {fab: 100000, dsn: 50000},
   "a progress invoice splits by its parent ESTIMATE's accounts (2/3 Fabrication, 1/3 Design)")
ok(misc not in sp, "the fallback isn't used when the parent estimate names the accounts")

ep = invoicing.record_invoice_payment(con, prog, into_account_id=bank, income_id=misc,
                                      amount_cents=150000, date="2026-08-02")
con.commit()
lp = legs_by_account(ep)
ok(lp[fab] == -100000 and lp[dsn] == -50000, "paying a progress invoice credits both job accounts")
ok(unbalanced() == 0, "ledger balanced for a progress-invoice payment")

# a plain invoice with no estimate parent still just uses the fallback (no accidental inheritance)
inv7 = make_invoice([(None, 1, 40000)])
ok(dict(invoicing.invoice_income_split(con, inv7, 40000, misc)) == {misc: 40000},
   "an ordinary itemless invoice is unaffected — it still uses the fallback")

con.close()
print("\nINVOICE INCOME SPLIT TESTS DONE")
