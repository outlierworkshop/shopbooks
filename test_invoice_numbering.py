"""Invoice/estimate starting numbers are settable in Settings and drive the next number.
Isolation: SHOPBOOKS_DATA_DIR before importing db."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_numbering_")

import db          # noqa: E402
import invoicing   # noqa: E402
from testutil import ok  # noqa: E402

db.init()
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
client = TestClient(appmod.app)
con = db.connect()

# --- set the starting numbers via the Settings form ---
r = client.post("/settings", data={"next_invoice_number": "2000", "next_estimate_number": "2000"},
                follow_redirects=False)
ok(r.status_code == 303, "saving settings redirects")
ok(db.get_setting(con, "next_invoice_number", "") == "2000", "next invoice number saved as 2000")
ok(db.get_setting(con, "next_estimate_number", "") == "2000", "next estimate number saved as 2000")

# --- the next created invoice/estimate use those numbers, then count up ---
ok(invoicing.next_number(con) == "INV-2000", "next invoice is INV-2000")
ok(invoicing.next_number(con) == "INV-2001", "invoice number counts up (INV-2001)")
ok(invoicing.next_estimate_number(con) == "EST-2000", "next estimate is EST-2000")
ok(invoicing.next_estimate_number(con) == "EST-2001", "estimate number counts up (EST-2001)")

# --- garbage / blank input keeps the current value (no crash, no reset) ---
db.set_setting(con, "next_invoice_number", "2002")
client.post("/settings", data={"next_invoice_number": "not-a-number"}, follow_redirects=False)
ok(db.get_setting(con, "next_invoice_number", "") == "2002", "a non-numeric entry is ignored (keeps 2002)")
client.post("/settings", data={"next_invoice_number": ""}, follow_redirects=False)
ok(db.get_setting(con, "next_invoice_number", "") == "2002", "a blank entry keeps the current number")

con.close()
print("\nINVOICE NUMBERING TESTS DONE")
