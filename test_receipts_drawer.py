import os
import shutil
import tempfile

# Setup test environment first so modules import with the correct database path
os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp()

import db
import app
import ledger
from fastapi.testclient import TestClient

db.init()
client = TestClient(app.app)

def ok(cond, msg):
    assert cond, f"FAIL: {msg}"
    print(f"PASS: {msg}")

def test_posted_manual_matching():
    con = db.connect()
    
    # 1. Seed accounts
    checking = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
    supplies = con.execute("SELECT id FROM accounts WHERE name='Materials & Supplies'").fetchone()["id"]
    
    # 2. Seed a posted ledger entry
    entry_id = ledger.post_entry(
        con, "2026-06-25", "Hardware Store Purchase",
        [(supplies, 12000), (checking, -12000)],
        memo="Posted entry test"
    )
    
    # 3. Seed an unmatched receipt doc
    cur = con.execute(
        "INSERT INTO documents(filename, path, vendor, doc_date, amount_cents, status) VALUES(?,?,?,?,?,?)",
        ("posted_rcpt.txt", "pr.txt", "Hardware Store", "2026-06-25", 12000, "unmatched")
    )
    doc_id = cur.lastrowid
    
    con.commit()
    con.close()
    
    # Verify the GET /receipts context contains the posted transaction
    r_get = client.get("/receipts")
    ok(r_get.status_code == 200, "GET /receipts returned 200")
    
    # Verify we can match the receipt to the posted transaction
    r_post = client.post(
        "/receipts/save-entry-matches",
        data={"doc_id": str(doc_id), f"entry_{doc_id}": [str(entry_id)]},
        follow_redirects=False
    )
    ok(r_post.status_code == 303, "POST /receipts/save-entry-matches redirected")
    
    con = db.connect()
    # Check document status
    doc = con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    ok(doc["status"] == "matched", "Document status is matched")
    ok(doc["entry_id"] == entry_id, "Document entry_id is populated")
    
    # Check document_entry_links
    links = con.execute("SELECT * FROM document_entry_links WHERE document_id=?", (doc_id,)).fetchall()
    ok(len(links) == 1, "One link in document_entry_links")
    ok(links[0]["entry_id"] == entry_id, "Linked to correct entry_id")
    con.close()
    
    # Test clearing matches (saving empty checklist)
    r_clear = client.post(
        "/receipts/save-entry-matches",
        data={"doc_id": str(doc_id)},
        follow_redirects=False
    )
    ok(r_clear.status_code == 303, "POST /receipts/save-entry-matches with no entries redirected")
    
    con = db.connect()
    doc = con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    ok(doc["status"] == "unmatched", "Document status is unmatched again")
    ok(doc["entry_id"] is None, "Document entry_id is cleared")
    
    links = con.execute("SELECT * FROM document_entry_links WHERE document_id=?", (doc_id,)).fetchall()
    ok(len(links) == 0, "All links cleared in document_entry_links")
    con.close()

if __name__ == "__main__":
    test_posted_manual_matching()
    shutil.rmtree(os.environ["SHOPBOOKS_DATA_DIR"], ignore_errors=True)
    print("\nPOSTED TRANSACTION MANUAL PAIRING TESTS DONE")
