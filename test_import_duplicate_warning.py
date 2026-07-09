"""Import duplicate statement warning test. Isolated via SHOPBOOKS_DATA_DIR."""
import io
import os
import tempfile
import json
from pathlib import Path

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_dup_warn_")
import db  # noqa: E402
import app as appmod  # noqa: E402
import importer  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
client = TestClient(appmod.app)

con = db.connect()
try:
    con.execute("UPDATE accounts SET name='EasternBankBusinessChecking' WHERE id=1")
    con.commit()
    checking_id = con.execute("SELECT id FROM accounts WHERE name='EasternBankBusinessChecking'").fetchone()["id"]
finally:
    con.close()

# Let's prepare a CSV statement
csv_data = "Date,Description,Amount\n01/05/2026,OFFICE DEPOT,-15.50\n01/06/2026,STARBUCKS,-4.25\n"

# 1. Perform the first import (direct ingestion via single-step backward compatibility POST)
r_init = client.post(
    "/import",
    files={"file": ("stmt_jan.csv", io.BytesIO(csv_data.encode()), "text/csv")},
    data={"account_id": str(checking_id)},
    follow_redirects=False
)
ok(r_init.status_code == 303, f"First statement imported successfully (got {r_init.status_code})")

# 2. Test Case A: Import another statement with the exact same filename
# This should trigger the filename duplicate warning in Step 1 (the confirm view)
r_case_a = client.post(
    "/import",
    files={"file": ("stmt_jan.csv", io.BytesIO(csv_data.encode()), "text/csv")},
    follow_redirects=False
)
ok(r_case_a.status_code == 200, f"Step 1 returned 200 (renders confirm screen) (got {r_case_a.status_code})")
ok("already been imported" in r_case_a.text, 
   "Renders filename duplicate warning correctly")

# 3. Test Case B: Import another statement with a DIFFERENT filename but same transaction contents
# This should trigger the transaction overlap duplicate warning in Step 1 (the confirm view)
r_case_b = client.post(
    "/import",
    files={"file": ("stmt_jan_different_filename.csv", io.BytesIO(csv_data.encode()), "text/csv")},
    follow_redirects=False
)
ok(r_case_b.status_code == 200, f"Step 1 returned 200 (renders confirm screen) (got {r_case_b.status_code})")
ok("transactions already exist" in r_case_b.text,
   "Renders transaction overlap duplicate warning correctly")

print("\nIMPORT DUPLICATE STATEMENT WARNING TESTS DONE")
