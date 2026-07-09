"""Reconcile upload test. Isolated via SHOPBOOKS_DATA_DIR."""
import io
import os
import tempfile
from pathlib import Path

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_rec_up_")
import db  # noqa: E402
import app as appmod  # noqa: E402
import importer  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
client = TestClient(appmod.app)

# 1. Initialize database and set up accounts
con = db.connect()
try:
    con.execute("UPDATE accounts SET name='EasternBankBusinessChecking' WHERE id=1")
    con.commit()
finally:
    con.close()

# 2. Test uploading a CSV statement for reconciliation
csv_data = "Date,Description,Amount\n01/05/2026,OFFICE DEPOT,-15.50\n01/22/2026,STARBUCKS,-4.25\n"

# POST to /reconcile/upload with follow_redirects=False to verify the redirect behavior
response = client.post(
    "/reconcile/upload",
    files={"file": ("Easternbank_stmt.csv", io.BytesIO(csv_data.encode()), "text/csv")},
    follow_redirects=False
)

ok(response.status_code == 303, f"Upload status code is 303 (got {response.status_code})")
location = response.headers.get("Location", "")
ok(location.startswith("/reconcile/1"), f"Redirects to EasternBankBusinessChecking account (got {location})")
ok("date=2026-01-22" in location, f"Pre-fills the latest transaction date 2026-01-22 (got {location})")
ok("balance=" in location, f"Pre-fills the balance query parameter (got {location})")

# Verify temporary files are deleted
con = db.connect()
try:
    # Ensure no temp_rec_ files remain in the docs folder
    docs_dir = Path(db.DOCS)
    if docs_dir.exists():
        temp_files = list(docs_dir.glob("temp_rec_*"))
        ok(len(temp_files) == 0, f"All temporary files cleaned up (found {len(temp_files)})")
    else:
        ok(True, "Docs directory was not created or cleaned up")
finally:
    con.close()

print("\nRECONCILE UPLOAD TESTS DONE")
