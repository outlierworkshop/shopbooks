"""What an ESTIMATE looks like when it goes out (PDF + email), vs an invoice.

An estimate is a quote, not a bill:
  - it carries **no due date** on the sent document (the due_date column still holds the internal
    "valid until" date — it just isn't printed/emailed);
  - it **must** show the memo, which is what the job actually is ("Davidov Cello Quotation").
    The PDF already had it under NOTES; the HTML email dropped it entirely, and also hardcoded
    "INVOICE" and a "Due:" line on every document — so an emailed quote announced itself as an
    invoice with a due date.

Invoices must be unaffected: they keep their due date, INVOICE label and "Total due".

Isolated via SHOPBOOKS_DATA_DIR before importing db."""
import io
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_estdoc_")

import pdfplumber  # noqa: E402
import db  # noqa: E402
import invoicing  # noqa: E402
from testutil import ok  # noqa: E402

db.init()
con = db.connect()
con.execute("INSERT INTO customers(name,email,address) VALUES('Collin G','c@test.local','1 Main St')")
cust = con.execute("SELECT id FROM customers").fetchone()["id"]


def make(kind, number, memo):
    con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,memo,kind) "
                "VALUES(?,?,'2026-08-01','2026-08-31',?,?,?)",
                (number, cust, "draft" if kind == "estimate" else "sent", memo, kind))
    did = con.execute("SELECT id FROM invoices WHERE number=?", (number,)).fetchone()["id"]
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,taxable) "
                "VALUES(?,'Design',1,50000,0)", (did,))
    con.commit()
    return did


def pdf_text(doc_id):
    d, items, total = invoicing.get_invoice(con, doc_id)
    raw = invoicing.render_pdf(con, d, items, total)
    return "".join((p.extract_text() or "") for p in pdfplumber.open(io.BytesIO(raw)).pages)


def email_html(doc_id):
    d, _, total = invoicing.get_invoice(con, doc_id)
    return invoicing.invoice_email_html(con, d, total, "A note.", None, "/settings/logo")


est = make("estimate", "EST-9100", "Davidov Cello Quotation")
inv = make("invoice", "INV-9200", "Bench work")

# --- the estimate PDF ---------------------------------------------------------------------------
et = pdf_text(est)
ok("ESTIMATE" in et.replace(" ", ""), "the estimate PDF is labelled ESTIMATE")
ok("Davidov Cello Quotation" in et, "the estimate PDF shows the memo (what the job is)")
ok("NOTES" in et, "the memo sits under a NOTES heading")
ok("Valid until" not in et, "the estimate PDF has no 'Valid until' line")
ok("Due" not in et, "the estimate PDF has no due date at all")
ok("2026-08-31" not in et, "the due_date value itself never appears on the estimate PDF")
ok("2026-08-01" in et, "the estimate PDF still shows its own date")

# --- the estimate email -------------------------------------------------------------------------
eh = email_html(est)
ok(">ESTIMATE<" in eh or "ESTIMATE<br>" in eh, "the estimate email is labelled ESTIMATE, not INVOICE")
ok("INVOICE" not in eh, "the estimate email never says INVOICE")
ok("Davidov Cello Quotation" in eh, "the estimate email includes the memo")
ok("NOTES" in eh, "the emailed memo has a NOTES heading")
ok("Due:" not in eh, "the estimate email shows no due date")
ok("2026-08-31" not in eh, "the due_date value never appears in the estimate email")
ok("Estimated total" in eh, "the estimate email totals to an 'Estimated total', matching the PDF")

# --- invoices are untouched ----------------------------------------------------------------------
it = pdf_text(inv)
ok("Due" in it and "2026-08-31" in it, "an invoice PDF still carries its due date")
ih = email_html(inv)
ok("INVOICE" in ih, "an invoice email is still labelled INVOICE")
ok("Due:" in ih and "2026-08-31" in ih, "an invoice email still shows the due date")
ok("Total due" in ih, "an invoice email still totals to 'Total due'")
ok("Bench work" in ih, "an invoice email carries its memo too (same NOTES block)")

# --- a blank memo simply omits the block ----------------------------------------------------------
blank = make("estimate", "EST-9101", "")
bh = email_html(blank)
ok("NOTES" not in bh, "no memo -> no empty NOTES block in the email")
ok("Davidov" not in bh, "the blank-memo estimate doesn't inherit another document's memo")

con.close()
print("\nESTIMATE DOCUMENT TESTS DONE")
