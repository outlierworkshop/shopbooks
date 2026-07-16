"""Progress billing: bill part of a quoted job (percent or dollar amount) against an estimate, and
receive a partial payment. The invoice IS the portion (its own lines) while the parent estimate keeps
the full scope, which the invoice/PDF/email show for reference but don't charge.
Isolation: SHOPBOOKS_DATA_DIR before importing db."""
import io
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_progress_")

import db  # noqa: E402
db.init()
import invoicing  # noqa: E402
import ledger  # noqa: E402
import app as appmod  # noqa: E402
from email.message import EmailMessage  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from testutil import ok  # noqa: E402

client = TestClient(appmod.app)
con = db.connect()
db.set_setting(con, "sales_tax_rate", "6.25")
cust = con.execute("INSERT INTO customers(name,email) VALUES('Job Customer','j@x.com')").lastrowid
con.commit()


def make_estimate(number, lines):
    """lines = [(description, qty, unit_cents, taxable)]"""
    eid = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,kind,status) "
                      "VALUES(?,?,?,?,'estimate','sent')",
                      (number, cust, "2026-07-16", "2026-08-16")).lastrowid
    for d, q, u, t in lines:
        con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,taxable) "
                    "VALUES(?,?,?,?,?)", (eid, d, q, u, t))
    con.commit()
    return eid


def bill(est_id, kind, value):
    r = client.post(f"/estimates/{est_id}/bill",
                    data={"portion_kind": kind, "portion_value": value}, follow_redirects=False)
    return r


def inv_id_from(r):
    return int(r.headers["location"].split("/invoices/")[1].split("?")[0])


# --- an all-taxable $2,000 job: total = $2,000 + 6.25% = $2,125 --------------------------------
est = make_estimate("EST-9001", [("Custom bracket", 2, 50000, 1), ("Finishing", 1, 100000, 1)])
job_sub = invoicing.invoice_subtotal(con, est)
est_total = invoicing.invoice_total(con, est)
ok(job_sub == 200000 and est_total == 212500, f"estimate = $2,000 job + 6.25% tax = $2,125 (got {est_total})")
ok(invoicing.estimate_remaining_subtotal(con, est) == 200000, "nothing billed yet -> whole job remaining")

# --- bill 50% ---------------------------------------------------------------------------------
r = bill(est, "percent", "50")
ok(r.status_code == 303 and "/invoices/" in r.headers["location"], "billing a portion creates an invoice")
inv1 = inv_id_from(r)
ok(invoicing.invoice_subtotal(con, inv1) == 100000, "invoice 1 bills $1,000 pre-tax (50% of the job)")
ok(invoicing.invoice_total(con, inv1) == 106250, "invoice 1 total = $1,000 + 6.25% tax = $1,062.50")
ok(invoicing.estimate_billed_subtotal(con, est) == 100000
   and invoicing.estimate_remaining_subtotal(con, est) == 100000, "billed/remaining track after invoice 1")

# the invoice shows the whole job but charges only its portion
p = invoicing.progress_info(con, inv1)
ok(p and p["is_partial"] and p["percent"] == 50.0, "progress info marks it a 50% partial")
ok([i["description"] for i in p["scope_items"]] == ["Custom bracket", "Finishing"],
   "scope carries the estimate's full line items")
ok(p["job_subtotal"] == 200000 and p["this_subtotal"] == 100000,
   "scope shows the $2,000 job while the invoice bills $1,000")

# --- bill the remainder as a dollar amount; the two invoices sum EXACTLY to the estimate -------
r2 = bill(est, "amount", "1000.00")
inv2 = inv_id_from(r2)
ok(invoicing.invoice_subtotal(con, inv2) == 100000, "a dollar-amount portion bills $1,000")
ok(invoicing.invoice_total(con, inv1) + invoicing.invoice_total(con, inv2) == est_total,
   "the two invoices sum EXACTLY to the estimate total, tax included")
ok(invoicing.estimate_remaining_subtotal(con, est) == 0, "job now fully billed")
r3 = bill(est, "percent", "10")
ok("err=" in r3.headers["location"], "billing a fully-billed estimate is refused")

# --- over-billing is clamped to what remains ---------------------------------------------------
est2 = make_estimate("EST-9002", [("Work", 1, 100000, 1)])
bill(est2, "percent", "150")
ok(invoicing.estimate_remaining_subtotal(con, est2) == 0, "asking for 150% bills only the remainder")

# --- mixed taxable / non-taxable splits proportionally and sums exactly ------------------------
est3 = make_estimate("EST-9003", [("Materials", 1, 150000, 1), ("Labor", 1, 50000, 0)])
inv3 = inv_id_from(bill(est3, "percent", "50"))
rows = con.execute("SELECT unit_cents, taxable FROM invoice_items WHERE invoice_id=? "
                   "ORDER BY taxable DESC", (inv3,)).fetchall()
ok(len(rows) == 2, "a mixed estimate bills a taxable and a non-taxable line")
ok(rows[0]["unit_cents"] == 75000 and rows[0]["taxable"] == 1, "taxable portion = 50% of $1,500")
ok(rows[1]["unit_cents"] == 25000 and rows[1]["taxable"] == 0, "non-taxable portion = 50% of $500")
ok(invoicing.invoice_subtotal(con, inv3) == 100000, "the split lines sum to the portion exactly")
ok(invoicing.invoice_tax(con, inv3) == round(75000 * 6.25 / 100), "tax applies only to the taxable portion")

# --- PDF + email show the full job scope but charge the portion --------------------------------
import pdfplumber  # noqa: E402
inv, items, total = invoicing.get_invoice(con, inv1)
txt = pdfplumber.open(io.BytesIO(invoicing.render_pdf(con, inv, items, total))).pages[0].extract_text()
ok("FULL JOB" in txt and "Custom bracket" in txt and "Finishing" in txt,
   "PDF shows the full job scope from the estimate")
ok("EST-9001" in txt, "PDF names the parent estimate")
ok(ledger.fmt_cents(106250) in txt, "PDF still charges only this invoice's portion")
# the scope must carry qty/unit, not just a lump amount (a bare total tells the customer nothing)
ok("QTY" in txt and "UNIT" in txt, "PDF scope has Qty/Unit columns")
ok(ledger.fmt_cents(50000) in txt, "PDF scope shows the $500 unit price of the 2x bracket line")

msg = EmailMessage()
invoicing._apply_invoice_email(msg, con, inv, total, "note", "plain")
html = [pt.get_content() for pt in msg.walk() if pt.get_content_type() == "text/html"][0]
ok("FULL JOB" in html and "Custom bracket" in html, "email shows the full job scope")
ok("Total due" in html and ledger.fmt_cents(106250) in html, "email charges only the portion")
ok(">QTY<" in html and ">UNIT<" in html, "email scope has Qty/Unit columns")
ok(f">${ledger.fmt_cents(50000)}<" in html, "email scope shows the unit price, not just a lump sum")

# --- receiving a PARTIAL payment ---------------------------------------------------------------
bank = con.execute("INSERT INTO accounts(name,kind,type,active) VALUES('Prog Bank','bank','asset',1)").lastrowid
inc = con.execute("INSERT INTO accounts(name,kind,type,active) VALUES('Prog Income','category','income',1)").lastrowid
con.commit()
pay = {"paid_date": "2026-07-16", "bank_id": str(bank), "income_id": str(inc)}

r = client.post(f"/invoices/{inv1}/pay", data={**pay, "amount": "500.00"}, follow_redirects=False)
ok(r.status_code == 303, "a partial payment posts")
row = con.execute("SELECT status, paid_entry_id FROM invoices WHERE id=?", (inv1,)).fetchone()
ok(row["status"] == "partially_paid" and row["paid_entry_id"] is None,
   "a partial leaves the invoice partially paid (linked, not owned as paid_entry_id)")
ok(invoicing.invoice_payments_total(con, inv1) == 50000, "payments total counts the partial")
ok(invoicing.invoice_outstanding_balance(con, inv1) == 106250 - 50000, "balance drops by the partial")

eid = con.execute("SELECT entry_id FROM invoice_entry_links WHERE invoice_id=?", (inv1,)).fetchone()["entry_id"]
legs = {r2c["account_id"]: r2c["amount_cents"] for r2c in con.execute(
    "SELECT account_id, amount_cents FROM splits WHERE entry_id=?", (eid,)).fetchall()}
ok(legs[bank] == 50000, "the deposit hits the bank account")
ok(legs[invoicing.sales_tax_account_id(con)] == -round(50000 * 6250 / 106250),
   "collected sales tax is split proportionally on the partial")
bad = con.execute("SELECT COUNT(*) FROM (SELECT entry_id FROM splits GROUP BY entry_id "
                  "HAVING SUM(amount_cents)!=0)").fetchone()[0]
ok(bad == 0, "ledger stays balanced")

# paying the rest (blank amount = whole balance) closes it
client.post(f"/invoices/{inv1}/pay", data={**pay, "amount": ""}, follow_redirects=False)
ok(con.execute("SELECT status FROM invoices WHERE id=?", (inv1,)).fetchone()["status"] == "paid",
   "paying the rest closes the invoice")
ok(invoicing.invoice_outstanding_balance(con, inv1) == 0,
   "nothing outstanding once fully paid (earlier partial still counts)")

# a percentage payment takes that share of the balance
client.post(f"/invoices/{inv2}/pay", data={**pay, "amount": "50%"}, follow_redirects=False)
ok(invoicing.invoice_payments_total(con, inv2) == round(106250 * 0.5),
   "a '50%' amount pays half the outstanding balance")

# --- the pages render (catches template errors) -------------------------------------------------
est_page = client.get(f"/estimates/{est}").text
ok("Billing this job" in est_page and "Remaining to bill" in est_page,
   "estimate page shows the progress-billing panel")
ok("EST-9001" in est_page and "Billed to date" in est_page, "estimate panel lists billed-to-date")
est2_page = client.get(f"/estimates/{est2}").text
ok("Fully billed" in est2_page, "a fully-billed estimate says so instead of offering the form")

inv_page = client.get(f"/invoices/{inv1}").text
ok("Full job" in inv_page and "Custom bracket" in inv_page and "Finishing" in inv_page,
   "invoice page shows the full job scope from the estimate")
ok("only the portion on this invoice is charged" in inv_page, "the scope is labelled as not charged")
ok("Job total (before tax)" in inv_page, "the scope shows the job total")

con.close()
print("\nPROGRESS BILLING TESTS DONE")
