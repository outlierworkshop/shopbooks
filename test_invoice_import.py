"""QBO invoice import (records only). Isolated via SHOPBOOKS_DATA_DIR."""
import io
import os
import tempfile
from urllib.parse import unquote

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_qbinv_")
import db  # noqa: E402
import migrate  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ok = lambda c, w: print(("PASS" if c else "FAIL"), w)
loc = lambda r: unquote(r.headers.get("location", ""))
client = TestClient(appmod.app)

# QBO "Invoice List": Date, No., Customer, Due Date, Amount, Open Balance, Status
csv1 = (
    "Date,No.,Customer,Due Date,Amount,Open Balance,Status\n"
    "01/15/2026,1042,Acme Corp,02/14/2026,\"1,200.00\",\"0.00\",Paid\n"
    "02/03/2026,1043,Beta LLC,03/05/2026,\"450.00\",\"450.00\",Open\n"
    "Total,,,,\"1,650.00\",,\n"
)
inv = migrate.parse_invoices(csv1.encode())
ok(len(inv) == 2, f"two invoices parsed (got {len(inv)})")
by_num = {i["number"]: i for i in inv}
ok(by_num["1042"]["amount_cents"] == 120000 and by_num["1042"]["status"] == "paid", "paid invoice: amount + status")
ok(by_num["1043"]["status"] == "sent", "open invoice -> sent")
ok(by_num["1042"]["customer"] == "Acme Corp", "customer captured")

r = client.post("/invoices/import-qbo", files={"file": ("InvoiceList.csv", io.BytesIO(csv1.encode()), "text/csv")},
                follow_redirects=False)
ok(r.status_code == 303 and "Imported 2" in loc(r), "import route reports 2 imported")

con = db.connect()
ok(con.execute("SELECT COUNT(*) c FROM invoices").fetchone()["c"] == 2, "2 invoice records created")
ok(con.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"] == 2, "2 customers created")
# RECORDS ONLY: nothing posted to the ledger
ok(con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0, "no ledger entries (records only - no double count)")
paid = con.execute("SELECT status, paid_entry_id FROM invoices WHERE number='1042'").fetchone()
ok(paid["status"] == "paid" and paid["paid_entry_id"] is None, "paid invoice has NO posted entry")
total = con.execute("SELECT SUM(qty*unit_cents) t FROM invoice_items").fetchone()["t"]
ok(total == 165000, "summary line items carry the totals")
con.close()

# re-import dedupes on invoice number
r = client.post("/invoices/import-qbo", files={"file": ("InvoiceList.csv", io.BytesIO(csv1.encode()), "text/csv")},
                follow_redirects=False)
ok("0 invoice" in loc(r) and "2 already present" in loc(r), "re-import dedupes by number")
con = db.connect()
ok(con.execute("SELECT COUNT(*) c FROM invoices").fetchone()["c"] == 2, "still 2 invoices after re-import")
con.close()

# variant headers: Transaction Type / Num / Name / Total, mixed rows (a payment row is skipped)
csv2 = (
    "Date,Transaction Type,Num,Name,Total\n"
    "03/10/2026,Invoice,1044,Gamma Inc,\"$99.50\"\n"
    "03/12/2026,Payment,,Gamma Inc,\"-99.50\"\n"
)
inv2 = migrate.parse_invoices(csv2.encode())
ok(len(inv2) == 1 and inv2[0]["number"] == "1044", "variant headers parsed; non-invoice row skipped")

import shutil  # noqa: E402
shutil.rmtree(os.environ["SHOPBOOKS_DATA_DIR"], ignore_errors=True)
print("\nQBO-INVOICE-IMPORT TESTS DONE")
