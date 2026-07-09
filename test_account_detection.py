"""Test for account auto-detection and the two-step import process. Isolated via SHOPBOOKS_DATA_DIR."""
import io
import os
import tempfile
import json
from pathlib import Path

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_acct_det_")
import db  # noqa: E402
import app as appmod  # noqa: E402
import importer  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
client = TestClient(appmod.app)

# 1. Test account auto-detection function directly
con = db.connect()
try:
    # Rename some default accounts to match expected types or check them
    con.execute("UPDATE accounts SET name='EasternBankBusinessChecking' WHERE id=1")
    con.execute("UPDATE accounts SET name='ChaseInk' WHERE id=2")
    con.commit()
    
    # Test cases:
    # Case A: Filename matches
    det_a = importer.detect_account_id(con, "Easternbank.csv", "Some generic statement body text here")
    ok(det_a == 1, f"Detects Easternbank from filename (got {det_a}, expected 1)")
    
    # Case B: Content matches
    det_b = importer.detect_account_id(con, "statement.pdf", "Activity on Chase Ink card statement")
    ok(det_b == 2, f"Detects ChaseInk from content (got {det_b}, expected 2)")

    # Case C: Fallback to first active account
    det_c = importer.detect_account_id(con, "stmt.pdf", "Hello world no matches")
    ok(det_c is not None, f"Fallback to first active account is not None (got {det_c})")
finally:
    con.close()


# 2. Test the two-step /import and /import/confirm HTTP flow
csv_data = "Date,Description,Amount\n01/05/2026,OFFICE DEPOT,-15.50\n01/06/2026,STARBUCKS,-4.25\n"

# Step 1: POST to /import without account_id
# This should NOT directly import but return the confirmation screen
response1 = client.post("/import", files={"file": ("Easternbank_stmt.csv", io.BytesIO(csv_data.encode()), "text/csv")})
ok(response1.status_code == 200, f"Step 1 status code is 200 (got {response1.status_code})")
html_content = response1.text
ok("Confirm Import" in html_content, "Redirects to confirm template or renders it")
ok("Confirm Target Account" in html_content, "Dropdown prompt is shown")
ok("EasternBankBusinessChecking" in html_content, "Detected account name is in dropdown options")

# Extract the hidden form fields from response html using simple string parsing
def get_hidden_field(html, name):
    marker = f'name="{name}" value="'
    idx = html.find(marker)
    if idx == -1:
        # try single quotes
        marker = f"name='{name}' value='"
        idx = html.find(marker)
    if idx == -1:
        return ""
    start = idx + len(marker)
    end = html.find('"', start)
    if end == -1:
        end = html.find("'", start)
    return html[start:end]

import html
filename = get_hidden_field(html_content, "filename")
temp_file_path = get_hidden_field(html_content, "temp_file_path")
txns_json = html.unescape(get_hidden_field(html_content, "txns_json"))

ok(filename == "Easternbank_stmt.csv", f"Parsed hidden filename correctly: {filename}")
ok(temp_file_path != "", f"Parsed temp_file_path correctly: {temp_file_path}")
ok(txns_json != "", "Parsed txns_json correctly")

txns = json.loads(txns_json)
ok(len(txns) == 2, f"Parsed 2 transactions: {len(txns)}")
ok(txns[0]["description"] == "OFFICE DEPOT", f"First txn is OFFICE DEPOT (got {txns[0]['description']})")

# Step 2: POST /import/confirm with custom/overridden account_id
# Let's override to ChaseInk (account ID 2)
response2 = client.post("/import/confirm", data={
    "filename": filename,
    "temp_file_path": temp_file_path,
    "account_id": "2",  # Override to ChaseInk
    "txns_json": txns_json,
    "note": ""
}, follow_redirects=False)

# Should redirect to /review
ok(response2.status_code == 303, f"Step 2 redirects to review (got {response2.status_code})")
ok(response2.headers.get("Location", "").startswith("/review"), f"Redirects to review (got {response2.headers.get('Location')})")

# Verify staged transactions are in the database under account_id = 2 (ChaseInk)
con = db.connect()
try:
    staged = con.execute("SELECT st.*, b.account_id FROM staged st JOIN batches b ON b.id=st.batch_id").fetchall()
    ok(len(staged) == 2, f"Staged 2 transactions in database (got {len(staged)})")
    for r in staged:
        ok(r["account_id"] == 2, f"Staged transaction has account_id 2 (got {r['account_id']})")
        ok(r["status"] == "pending", f"Staged transaction is pending (got {r['status']})")
        
    # Verify temp CSV file is deleted
    ok(not Path(temp_file_path).exists(), "Temp CSV statement file was deleted successfully")
finally:
    con.close()

print("\nACCOUNT DETECTION & TWO-STEP IMPORT TESTS DONE")
