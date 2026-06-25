import os
import shutil

# Setup test environment first so modules import with the correct database path
os.environ["SHOPBOOKS_DATA_DIR"] = os.path.join(os.path.dirname(__file__), ".test_auto_cat_data")
shutil.rmtree(os.environ["SHOPBOOKS_DATA_DIR"], ignore_errors=True)

import db
import app
import ledger
import ai
from fastapi.testclient import TestClient

# Monkeypatch AI to simulate receipt extraction and categorization in test
ai.available = lambda con: True
ai.extract_receipt = lambda con, path: {"vendor": "Office Depot", "date": "2026-05-14", "total": 45.00}
ai.categorize = lambda con, txns, names: ["Materials & Supplies" for _ in txns]

db.init()
client = TestClient(app.app)

def ok(cond, msg):
    assert cond, f"FAIL: {msg}"
    print(f"PASS: {msg}")

def test_auto_cat():
    con = db.connect()
    # 1. Seed accounts
    checking = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
    supplies = con.execute("SELECT id FROM accounts WHERE name='Materials & Supplies'").fetchone()["id"]
    postage = con.execute("SELECT id FROM accounts WHERE name='Shipping & Postage'").fetchone()["id"]
    
    # 2. Seed an unmatched receipt
    # Note: 12.50 amount, dated 2026-05-10
    con.execute(
        "INSERT INTO documents(filename, path, vendor, doc_date, amount_cents, sha256, status) VALUES(?,?,?,?,?,?,?)",
        ("test_receipt.txt", "dummy_rcpt.txt", "USPS", "2026-05-10", 1250, "sha_test1", "unmatched")
    )
    con.commit()
    con.close()

    # 3. Import a statement with a matching transaction
    # We simulate statement import POST /import
    # 12.50 amount (money out is negative in bank statement CSV -> parsed as positive 1250)
    csv_data = (
        "Date,Description,Amount\n"
        "05/12/2026,USPS POSTAGE,-12.50\n"
    )
    
    r = client.post(
        "/import",
        data={"account_id": str(checking)},
        files={"file": ("stmt.csv", csv_data.encode("utf-8"), "text/csv")}
    )
    ok(r.status_code == 303 or r.status_code == 200, "statement import request completed")

    # Verify that the staged row got automatically matched and categorized
    con = db.connect()
    st = con.execute("SELECT * FROM staged WHERE status='pending' AND amount_cents=1250").fetchone()
    print("ST ROW IS:", dict(st) if st else None)
    ok(st is not None, "pending staged transaction found")
    ok(st["amount_cents"] == 1250, "amount matches")
    # Verify that the receipt is indeed matching in staged_receipt_matches.
    matches = app.staged_receipt_matches(con)
    ok(st["id"] in matches, "receipt match exists in staged_receipt_matches")
    ok(matches[st["id"]][0]["sha256"] == "sha_test1", "matched the correct receipt by SHA")
    con.close()

    # 4. Now test receipt upload auto-matching.
    # Seed a new staged row: 45.00, dated 2026-05-15
    con = db.connect()
    cur = con.execute(
        "INSERT INTO staged(batch_id, date, description, amount_cents, status) VALUES(?,?,?,?,?)",
        (st["batch_id"], "2026-05-15", "Office Depot", 4500, "pending")
    )
    staged_id = cur.lastrowid
    con.commit()
    con.close()

    # Upload a receipt for 45.00, dated 2026-05-14
    receipt_data = b"Office Depot receipt contents"
    r2 = client.post(
        "/receipts/upload",
        files={"files": ("office_depot.txt", receipt_data, "text/plain")}
    )
    print("R2 STATUS:", r2.status_code)
    ok(r2.status_code == 200, "receipt upload completed")

    con = db.connect()
    uploaded_doc = con.execute("SELECT * FROM documents WHERE filename='office_depot.txt'").fetchone()
    ok(uploaded_doc is not None, "uploaded receipt found in database")
    # Verify that the staged row matches it
    matches2 = app.staged_receipt_matches(con)
    ok(staged_id in matches2, "receipt matched to pending transaction after upload")
    ok(matches2[staged_id][0]["id"] == uploaded_doc["id"], "linked to the uploaded doc id")
    con.close()

    # 5. Test auto-linking matched receipt on post.
    # We post the transaction
    r3 = client.post(
        "/review",
        data={"post_one": str(staged_id), f"cat_{staged_id}": str(supplies)},
        follow_redirects=False
    )
    ok(r3.status_code == 303, "posted pending transaction")

    # Verify that the receipt doc is now automatically matched and linked to the ledger entry in documents table
    con = db.connect()
    posted_st = con.execute("SELECT * FROM staged WHERE id=?", (staged_id,)).fetchone()
    ok(posted_st["status"] == "posted", "staged transaction is posted")
    entry_id = posted_st["entry_id"]
    ok(entry_id is not None, "ledger entry was created")

    linked_doc = con.execute("SELECT * FROM documents WHERE id=?", (uploaded_doc["id"],)).fetchone()
    ok(linked_doc["status"] == "matched", "receipt document marked as matched")
    ok(linked_doc["entry_id"] == entry_id, "receipt document linked to the correct entry_id")
    con.close()

    # 6. Test manual multiple receipt pairing
    # Seed another staged transaction: 150.00, dated 2026-05-20
    con = db.connect()
    cur = con.execute(
        "INSERT INTO staged(batch_id, date, description, amount_cents, status) VALUES(?,?,?,?,?)",
        (st["batch_id"], "2026-05-20", "Various Purchases", 15000, "pending")
    )
    manual_staged_id = cur.lastrowid
    
    # Seed two unmatched documents: 90.00 and 60.00
    cur1 = con.execute(
        "INSERT INTO documents(filename, path, vendor, doc_date, amount_cents, sha256, status) VALUES(?,?,?,?,?,?,?)",
        ("manual1.txt", "m1.txt", "Vendor A", "2026-05-19", 9000, "sha_manual1", "unmatched")
    )
    m1_id = cur1.lastrowid
    
    cur2 = con.execute(
        "INSERT INTO documents(filename, path, vendor, doc_date, amount_cents, sha256, status) VALUES(?,?,?,?,?,?,?)",
        ("manual2.txt", "m2.txt", "Vendor B", "2026-05-19", 6000, "sha_manual2", "unmatched")
    )
    m2_id = cur2.lastrowid
    con.commit()
    con.close()

    # Post to /review to manually pair them (simulating checking them in the list and clicking Save Matches)
    r4 = client.post(
        "/review",
        data={"save_matches": str(manual_staged_id), f"docs_{manual_staged_id}": [str(m1_id), str(m2_id)]},
        follow_redirects=False
    )
    ok(r4.status_code == 303, "manually saved matches successfully")

    # Verify that staged_receipt_matches returns both documents for this staged row
    con = db.connect()
    manual_matches = app.staged_receipt_matches(con)
    ok(manual_staged_id in manual_matches, "staged row has matches")
    ok(len(manual_matches[manual_staged_id]) == 2, "paired with exactly 2 documents")
    ok({d["id"] for d in manual_matches[manual_staged_id]} == {m1_id, m2_id}, "linked to correct document IDs")
    con.close()

    # Now post the transaction
    r5 = client.post(
        "/review",
        data={"post_one": str(manual_staged_id), f"cat_{manual_staged_id}": str(supplies)},
        follow_redirects=False
    )
    ok(r5.status_code == 303, "posted manually paired transaction")

    # Verify both documents are marked as matched and linked to the correct entry_id
    con = db.connect()
    posted_manual_st = con.execute("SELECT * FROM staged WHERE id=?", (manual_staged_id,)).fetchone()
    manual_entry_id = posted_manual_st["entry_id"]
    ok(manual_entry_id is not None, "ledger entry created for manually matched row")

    doc1 = con.execute("SELECT * FROM documents WHERE id=?", (m1_id,)).fetchone()
    doc2 = con.execute("SELECT * FROM documents WHERE id=?", (m2_id,)).fetchone()
    ok(doc1["status"] == "matched" and doc1["entry_id"] == manual_entry_id, "first manual receipt linked")
    ok(doc2["status"] == "matched" and doc2["entry_id"] == manual_entry_id, "second manual receipt linked")
    con.close()

test_auto_cat()
shutil.rmtree(os.environ["SHOPBOOKS_DATA_DIR"], ignore_errors=True)
print("\nRECEIPT AUTO CATEGORIZE TESTS DONE")
