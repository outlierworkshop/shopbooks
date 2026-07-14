"""Invoice/estimate/credit-memo PDF rendering: the clean-minimal render_pdf produces a valid PDF
for every kind, and _latin transliterates common non-latin-1 punctuation to ASCII (so fpdf2's
built-in fonts don't render em-dashes and curly quotes as "?"). Isolated via SHOPBOOKS_DATA_DIR."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_invpdf_")

import db  # noqa: E402
db.init()
import invoicing  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed


# ---- _latin: punctuation that isn't in latin-1 becomes clean ASCII, not "?" ----
ok(invoicing._latin("Custom mandolin — Model A") == "Custom mandolin - Model A", "em-dash -> hyphen")
ok(invoicing._latin("a – b") == "a - b", "en-dash -> hyphen")
ok(invoicing._latin("“curly” ‘quotes’") == '"curly" \'quotes\'', "curly quotes -> straight")
ok(invoicing._latin("wait…") == "wait...", "ellipsis -> ...")
ok(invoicing._latin("• bullet → arrow ™") == "- bullet -> arrow (TM)", "bullet/arrow/tm transliterated")
ok("?" not in invoicing._latin("mandolin — “A” … • → ™"), "no '?' left for common punctuation")
ok(invoicing._latin("Plain ASCII 123") == "Plain ASCII 123", "plain ASCII passes through unchanged")


# ---- render_pdf produces a valid PDF for every document kind (all money branches) ----
con = db.connect()
db.set_setting(con, "business_name", "Outlier Workshop")
db.set_setting(con, "sales_tax_rate", "7.0")
con.execute("INSERT INTO customers(name, email) VALUES('Marcus Reed', 'm@example.com')")
cid = con.execute("SELECT id FROM customers").fetchone()[0]


def make(kind, number):
    con.execute("INSERT INTO invoices(number,customer_id,date,due_date,memo,kind,status) "
                "VALUES(?,?,?,?,?,?,?)", (number, cid, "2026-07-12", "2026-07-26", "memo", kind, "sent"))
    iid = con.execute("SELECT id FROM invoices WHERE number=?", (number,)).fetchone()[0]
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,item_id,taxable) "
                "VALUES(?,?,?,?,?,?)", (iid, "Custom mandolin — Model A", 1, 320000, None, 1))
    con.commit()
    return iid


for kind, number in [("invoice", "INV-1"), ("estimate", "EST-1"), ("credit_memo", "CM-1")]:
    inv, items, total = invoicing.get_invoice(con, make(kind, number))
    pdf = invoicing.render_pdf(con, inv, items, total)
    ok(pdf[:4] == b"%PDF" and len(pdf) > 1500, f"{kind} renders a valid PDF ({len(pdf)} bytes)")

print("\nINVOICE PDF TESTS DONE")
