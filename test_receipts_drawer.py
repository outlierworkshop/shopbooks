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


def test_receipt_matched_to_multiple_shows_on_every_register_row():
    """A receipt split across two transactions must show on BOTH in the register — not just the first
    (documents.entry_id only stores the first; the register reads document_entry_links)."""
    con = db.connect()
    checking = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
    supplies = con.execute("SELECT id FROM accounts WHERE name='Materials & Supplies'").fetchone()["id"]
    e1 = ledger.post_entry(con, "2026-06-10", "Lumber - part 1", [(supplies, 8000), (checking, -8000)])
    e2 = ledger.post_entry(con, "2026-06-11", "Lumber - part 2", [(supplies, 4000), (checking, -4000)])
    doc_id = con.execute(
        "INSERT INTO documents(filename,path,vendor,doc_date,amount_cents,status) VALUES(?,?,?,?,?,?)",
        ("lumber.jpg", "l.jpg", "Lumber Co", "2026-06-10", 12000, "unmatched")).lastrowid
    con.commit()
    con.close()

    r = client.post("/receipts/save-entry-matches",
                    data={"doc_id": str(doc_id), f"entry_{doc_id}": [str(e1), str(e2)]},
                    follow_redirects=False)
    ok(r.status_code == 303, "matched the receipt to two transactions")

    con = db.connect()
    _acct, rows = ledger.register(con, checking)
    by_entry = {row["entry_id"]: row for row in rows}
    ok(by_entry[e1]["doc_id"] == doc_id, "receipt shows on the first matched transaction")
    ok(by_entry[e2]["doc_id"] == doc_id, "receipt ALSO shows on the second matched transaction")
    con.close()


def test_receipt_split_across_staged_links_to_every_posted_entry():
    """The real report: match one receipt to TWO staged transactions, then post both. The receipt must
    link to BOTH resulting entries (and leave no stale staged link) — before the fix, posting the first
    flipped the receipt to 'matched' so the second post couldn't find it, and it showed on one register
    row only."""
    import staging
    con = db.connect()
    checking = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
    supplies = con.execute("SELECT id FROM accounts WHERE name='Materials & Supplies'").fetchone()["id"]
    batch = con.execute("INSERT INTO batches(filename,account_id) VALUES('b.csv',?)", (checking,)).lastrowid
    s1 = con.execute("INSERT INTO staged(batch_id,date,description,amount_cents,status) "
                     "VALUES(?,?,?,?,'pending')", (batch, "2026-06-10", "Lumber A", 8000)).lastrowid
    s2 = con.execute("INSERT INTO staged(batch_id,date,description,amount_cents,status) "
                     "VALUES(?,?,?,?,'pending')", (batch, "2026-06-11", "Lumber B", 4000)).lastrowid
    doc_id = con.execute(
        "INSERT INTO documents(filename,path,vendor,doc_date,amount_cents,status) VALUES(?,?,?,?,?,?)",
        ("lumber.jpg", "l.jpg", "Lumber Co", "2026-06-10", 12000, "unmatched")).lastrowid
    con.execute("INSERT INTO document_staged_links(document_id,staged_id) VALUES(?,?)", (doc_id, s1))
    con.execute("INSERT INTO document_staged_links(document_id,staged_id) VALUES(?,?)", (doc_id, s2))
    con.commit()

    # post both staged rows (as Review does)
    staging._post_staged(con, s1, supplies)
    staging._post_staged(con, s2, supplies)
    con.commit()

    e1 = con.execute("SELECT entry_id FROM staged WHERE id=?", (s1,)).fetchone()["entry_id"]
    e2 = con.execute("SELECT entry_id FROM staged WHERE id=?", (s2,)).fetchone()["entry_id"]
    links = {r["entry_id"] for r in con.execute(
        "SELECT entry_id FROM document_entry_links WHERE document_id=?", (doc_id,)).fetchall()}
    ok(e1 in links and e2 in links, "receipt links to BOTH posted entries")
    ok(con.execute("SELECT COUNT(*) c FROM document_staged_links WHERE document_id=?",
                   (doc_id,)).fetchone()["c"] == 0, "no stale staged links remain")

    _acct, rows = ledger.register(con, checking)
    by_entry = {row["entry_id"]: row["doc_id"] for row in rows}
    ok(by_entry.get(e1) == doc_id and by_entry.get(e2) == doc_id,
       "the receipt shows on BOTH transactions in the register")
    con.close()


if __name__ == "__main__":
    test_posted_manual_matching()
    test_receipt_matched_to_multiple_shows_on_every_register_row()
    test_receipt_split_across_staged_links_to_every_posted_entry()
    shutil.rmtree(os.environ["SHOPBOOKS_DATA_DIR"], ignore_errors=True)
    print("\nPOSTED TRANSACTION MANUAL PAIRING TESTS DONE")
