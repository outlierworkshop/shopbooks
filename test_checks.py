"""Check writing/printing: amount-to-words, per-account numbering, the ledger posting, the PDF, the
payee list, and the write→preview→record flow. Isolation: SHOPBOOKS_DATA_DIR before importing db."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_checks_")

import db        # noqa: E402
import checks    # noqa: E402
from testutil import ok  # noqa: E402

db.init()
con = db.connect()

bank = con.execute("INSERT INTO accounts(name,kind,type,active) VALUES('Check Test Bank','bank','asset',1)").lastrowid
exp = con.execute("INSERT INTO accounts(name,kind,type,active) VALUES('Check Test Supplies','category','expense',1)").lastrowid
con.commit()

# --- amount to words ----------------------------------------------------------
ok(checks.amount_to_words(0) == "Zero and 00/100", "zero")
ok(checks.amount_to_words(100) == "One and 00/100", "$1.00")
ok(checks.amount_to_words(4567) == "Forty-five and 67/100", "$45.67 uses a hyphenated ten")
ok(checks.amount_to_words(123456) == "One thousand two hundred thirty-four and 56/100", "$1,234.56")
ok(checks.amount_to_words(100000) == "One thousand and 00/100", "$1,000.00 (no trailing zero words)")
ok(checks.amount_to_words(200000000) == "Two million and 00/100", "$2,000,000.00")

# --- numbering (per account) --------------------------------------------------
ok(checks.next_check_number(con, bank) is None, "no checks yet -> None (owner types the first number)")

# --- create_and_post books a balanced payment + records the check -------------
cid = checks.create_and_post(con, account_id=bank, payee_id=None, payee_name="McMaster-Carr",
                             date="2026-07-14", amount_cents=123456, memo="hardware", category_id=exp,
                             check_number=1001)
con.commit()
chk = checks.get_check(con, cid)
ok(chk["status"] == "printed" and chk["check_number"] == 1001, "check recorded as printed")
legs = con.execute("SELECT account_id, amount_cents FROM splits WHERE entry_id=?", (chk["entry_id"],)).fetchall()
bysign = {r["account_id"]: r["amount_cents"] for r in legs}
ok(bysign[exp] == 123456 and bysign[bank] == -123456, "category debited, bank credited")
bad = con.execute("SELECT COUNT(*) FROM (SELECT entry_id FROM splits GROUP BY entry_id HAVING SUM(amount_cents)!=0)").fetchone()[0]
ok(bad == 0, "ledger stays balanced")
ok(checks.next_check_number(con, bank) == 1002, "next number counts up from the last printed")

# --- PDF renders --------------------------------------------------------------
pdf = checks.render_check_pdf(con, dict(chk))
ok(pdf[:4] == b"%PDF" and len(pdf) > 800, "check renders to a PDF")

# --- US date + DWE001 window address block ------------------------------------
ok(checks._us_date("2026-07-14") == "07/14/2026", "ISO date -> US MM/DD/YYYY on the check face")
ok(checks._us_date("whenever") == "whenever", "an unparseable date passes straight through")
ok(checks._payee_address_lines(con, {"payee_addr": "742 Evergreen Terrace\nSpringfield, MA 01101"})
   == ["742 Evergreen Terrace", "Springfield, MA 01101"], "address splits into window-block lines")
ok(checks._payee_address_lines(con, {"payee_addr": "  "}) == [], "no address -> window block skipped")
pdf_addr = checks.render_check_pdf(con, {"account_id": bank, "payee_name": "Acme", "date": "2026-07-14",
           "amount_cents": 5000, "memo": "m", "category_id": exp, "check_number": 1200,
           "payee_addr": "1 A St\nB, MA 02000"})
ok(pdf_addr[:4] == b"%PDF", "check with a window address block still renders")

# --- void unwinds the ledger entry --------------------------------------------
checks.void_check(con, cid)
con.commit()
ok(checks.get_check(con, cid)["status"] == "void", "check marked void")
ok(con.execute("SELECT COUNT(*) c FROM entries WHERE id=?", (chk["entry_id"],)).fetchone()["c"] == 0,
   "voiding removed the posted entry")

# --- resolve_payee (existing + new) -------------------------------------------
pid = con.execute("INSERT INTO payees(name) VALUES('Existing Vendor')").lastrowid
con.commit()
got_id, got_name = checks.resolve_payee(con, {"payee_id": str(pid)})
ok(got_id == pid and got_name == "Existing Vendor", "resolve_payee returns the picked payee")
new_id, new_name = checks.resolve_payee(con, {"new_payee_name": "Brand New Vendor", "new_payee_email": "v@x.com"})
ok(new_id != pid and new_name == "Brand New Vendor", "resolve_payee creates a new payee from a typed name")
addr_id, _ = checks.resolve_payee(con, {"new_payee_name": "Mailed Vendor",
                                        "new_payee_address": "9 Elm St\nWorcester, MA 01601"})
ok(con.execute("SELECT address FROM payees WHERE id=?", (addr_id,)).fetchone()["address"]
   == "9 Elm St\nWorcester, MA 01601", "resolve_payee stores a new payee's mailing address")
try:
    checks.resolve_payee(con, {})
    ok(False, "resolve_payee should require a payee")
except ValueError:
    ok(True, "resolve_payee raises when neither picked nor typed")
con.commit()

# --- routes / flow ------------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
client = TestClient(appmod.app)

ok(client.get("/checks").status_code == 200, "checks list renders")
ok(client.get("/checks/new").status_code == 200, "write-a-check page renders")
ok(client.get("/payees").status_code == 200, "payees page renders")

# add a payee via the route
client.post("/payees", data={"name": "Route Payee", "email": "", "address": "", "phone": "", "notes": ""},
            follow_redirects=False)
ok(con.execute("SELECT COUNT(*) c FROM payees WHERE name='Route Payee'").fetchone()["c"] == 1, "payee added via route")

check_form = {"account_id": str(bank), "payee_id": "", "new_payee_name": "Flow Payee", "new_payee_email": "",
              "date": "2026-07-15", "amount": "250.00", "category_id": str(exp), "memo": "flow test",
              "check_number": "2001"}

prev = client.post("/checks/preview", data=check_form)
ok(prev.status_code == 200 and b"record it" in prev.content and b"/checks/preview.pdf" in prev.content,
   "preview shows the PDF iframe and the confirm button")
pdfr = client.get("/checks/preview.pdf", params={"account_id": bank, "payee_name": "Flow Payee",
                  "date": "2026-07-15", "amount_cents": 25000, "category_id": exp, "memo": "x", "check_number": 2001})
ok(pdfr.status_code == 200 and pdfr.headers["content-type"] == "application/pdf", "preview.pdf returns a PDF")

r = client.post("/checks/print", data=check_form, follow_redirects=False)
ok(r.status_code == 303, "printing confirms with a redirect")
rec = con.execute("SELECT * FROM checks WHERE account_id=? AND check_number=2001", (bank,)).fetchone()
ok(rec is not None and rec["payee_name"] == "Flow Payee" and rec["amount_cents"] == 25000,
   "check recorded with the new (auto-created) payee and amount")
ok(con.execute("SELECT COUNT(*) c FROM entries WHERE id=?", (rec["entry_id"],)).fetchone()["c"] == 1,
   "printing posted the ledger entry")
ok(checks.next_check_number(con, bank) == 2002, "next number advanced after printing")

# reusing a live check number is refused (jam-safety)
dup = client.post("/checks/print", data=check_form)
ok(b"already recorded" in dup.content, "a duplicate check number on the same account is refused")
ok(con.execute("SELECT COUNT(*) c FROM checks WHERE account_id=? AND check_number=2001 AND status='printed'",
               (bank,)).fetchone()["c"] == 1, "the duplicate was not double-recorded")

con.close()
print("\nCHECK TESTS DONE")
