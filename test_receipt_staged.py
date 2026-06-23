"""Test receipt-to-staged-transaction matching and categorization.

Verifies:
 1. staged_receipt_matches finds a pair when amounts match within 7 days
 2. staged_receipt_matches returns empty when amounts differ
 3. staged_receipt_matches returns empty when dates are >7 days apart
 4. Each receipt and staged row is used at most once (greedy, nearest date)
 5. _categorize_from_receipts updates staged category_id (AI monkeypatched)
 6. _categorize_from_receipts reports no-AI gracefully
 7. Review page shows receipt_vendor on matched rows
"""
import os, sys, tempfile
os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp()

import db, app as webapp
from fastapi.testclient import TestClient

client = TestClient(webapp.app)
db.init()

# Look up real seeded account IDs by name
_con = db.connect()
BANK_ID = _con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
TOOLS_ID = _con.execute("SELECT id FROM accounts WHERE name='Tools & Small Equipment'").fetchone()["id"]
OFFICE_ID = _con.execute("SELECT id FROM accounts WHERE name='Office Supplies'").fetchone()["id"]
_con.close()


def _seed():
    """Create a batch for the test bank account."""
    con = db.connect()
    con.execute("INSERT OR IGNORE INTO batches(id,filename,account_id,imported_at) VALUES(1,'test.pdf',?,?)",
                (BANK_ID, '2026-01-01'))
    con.commit()
    con.close()


def test_match_by_amount_and_date():
    """Amount match + within 7 days -> found."""
    _seed()
    con = db.connect()
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,status) "
                "VALUES(100,1,'2026-01-10','AMAZON MARKETPLACE',5000,'pending')")
    con.execute("INSERT INTO documents(id,filename,path,vendor,doc_date,amount_cents,status) "
                "VALUES(200,'rcpt.txt','/tmp/rcpt.txt','SpeTool Bits','2026-01-10',5000,'unmatched')")
    con.commit()
    matches = webapp.staged_receipt_matches(con)
    assert 100 in matches, f"Expected staged id 100 in matches, got {matches}"
    assert matches[100]["id"] == 200
    assert matches[100]["vendor"] == "SpeTool Bits"
    con.execute("DELETE FROM staged WHERE id=100")
    con.execute("DELETE FROM documents WHERE id=200")
    con.commit()
    con.close()
    print("PASS: test_match_by_amount_and_date")


def test_no_match_different_amount():
    """Different amounts -> no match."""
    _seed()
    con = db.connect()
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,status) "
                "VALUES(101,1,'2026-01-10','AMAZON',5000,'pending')")
    con.execute("INSERT INTO documents(id,filename,path,vendor,doc_date,amount_cents,status) "
                "VALUES(201,'rcpt.txt','/tmp/rcpt.txt','Amazon','2026-01-10',9999,'unmatched')")
    con.commit()
    matches = webapp.staged_receipt_matches(con)
    assert 101 not in matches, f"Should not match different amounts, got {matches}"
    con.execute("DELETE FROM staged WHERE id=101")
    con.execute("DELETE FROM documents WHERE id=201")
    con.commit()
    con.close()
    print("PASS: test_no_match_different_amount")


def test_no_match_date_too_far():
    """Same amount but >7 days apart -> no match."""
    _seed()
    con = db.connect()
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,status) "
                "VALUES(102,1,'2026-01-10','AMAZON',5000,'pending')")
    con.execute("INSERT INTO documents(id,filename,path,vendor,doc_date,amount_cents,status) "
                "VALUES(202,'rcpt.txt','/tmp/rcpt.txt','Amazon','2026-01-25',5000,'unmatched')")
    con.commit()
    matches = webapp.staged_receipt_matches(con)
    assert 102 not in matches, f"Should not match dates >7 days apart, got {matches}"
    con.execute("DELETE FROM staged WHERE id=102")
    con.execute("DELETE FROM documents WHERE id=202")
    con.commit()
    con.close()
    print("PASS: test_no_match_date_too_far")


def test_greedy_one_to_one():
    """Two staged rows same amount, one receipt -> only one match (nearest date)."""
    _seed()
    con = db.connect()
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,status) "
                "VALUES(103,1,'2026-01-10','AMAZON A',5000,'pending')")
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,status) "
                "VALUES(104,1,'2026-01-15','AMAZON B',5000,'pending')")
    con.execute("INSERT INTO documents(id,filename,path,vendor,doc_date,amount_cents,status) "
                "VALUES(203,'rcpt.txt','/tmp/rcpt.txt','Amazon','2026-01-14',5000,'unmatched')")
    con.commit()
    matches = webapp.staged_receipt_matches(con)
    assert len(matches) == 1, f"Expected exactly 1 match, got {len(matches)}: {matches}"
    assert 104 in matches, f"Expected nearest-date staged 104, got {matches}"
    con.execute("DELETE FROM staged WHERE id IN (103,104)")
    con.execute("DELETE FROM documents WHERE id=203")
    con.commit()
    con.close()
    print("PASS: test_greedy_one_to_one")


def test_categorize_from_receipts_with_ai(monkeypatch_ai):
    """AI suggests category from receipt content -> staged.category_id updated."""
    _seed()
    con = db.connect()
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,category_id,status) "
                "VALUES(105,1,'2026-02-01','AMAZON MARKETPLACE',8000,NULL,'pending')")
    con.execute("INSERT INTO documents(id,filename,path,vendor,doc_date,amount_cents,status) "
                "VALUES(205,'rcpt.txt','/tmp/rcpt.txt','SpeTool Router Bits','2026-02-01',8000,'unmatched')")
    con.commit()
    matched, categorized, err = webapp._categorize_from_receipts(con)
    con.commit()
    assert matched == 1, f"Expected 1 match, got {matched}"
    assert categorized == 1, f"Expected 1 categorized, got {categorized}"
    assert err is None, f"Expected no error, got {err}"
    row = con.execute("SELECT category_id FROM staged WHERE id=105").fetchone()
    assert row["category_id"] == TOOLS_ID, f"Expected category {TOOLS_ID} (Tools), got {row['category_id']}"
    con.execute("DELETE FROM staged WHERE id=105")
    con.execute("DELETE FROM documents WHERE id=205")
    con.commit()
    con.close()
    print("PASS: test_categorize_from_receipts_with_ai")


def test_categorize_no_ai():
    """No AI key -> matches found but 0 categorized, error message returned."""
    _seed()
    con = db.connect()
    con.execute("UPDATE settings SET value='' WHERE key='anthropic_api_key'")
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,status) "
                "VALUES(106,1,'2026-03-01','PURCHASE',3000,'pending')")
    con.execute("INSERT INTO documents(id,filename,path,vendor,doc_date,amount_cents,status) "
                "VALUES(206,'rcpt.txt','/tmp/rcpt.txt','Some Store','2026-03-01',3000,'unmatched')")
    con.commit()
    matched, categorized, err = webapp._categorize_from_receipts(con)
    assert matched == 1
    assert categorized == 0
    assert err is not None and "AI is off" in err
    con.execute("DELETE FROM staged WHERE id=106")
    con.execute("DELETE FROM documents WHERE id=206")
    con.commit()
    con.close()
    print("PASS: test_categorize_no_ai")


def test_review_page_shows_receipt_vendor():
    """Review page includes receipt_vendor for matched rows."""
    _seed()
    con = db.connect()
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,status) "
                "VALUES(107,1,'2026-04-01','AMAZON',7500,'pending')")
    con.execute("INSERT INTO documents(id,filename,path,vendor,doc_date,amount_cents,status) "
                "VALUES(207,'rcpt.txt','/tmp/rcpt.txt','Router Bits Co','2026-04-01',7500,'unmatched')")
    con.commit()
    con.close()
    resp = client.get("/review")
    assert resp.status_code == 200
    assert "\U0001f4ce" in resp.text, "Expected paperclip indicator on review page"
    assert "Router Bits Co" in resp.text, "Expected receipt vendor in tooltip"
    con = db.connect()
    con.execute("DELETE FROM staged WHERE id=107")
    con.execute("DELETE FROM documents WHERE id=207")
    con.commit()
    con.close()
    print("PASS: test_review_page_shows_receipt_vendor")


def test_match_no_date_on_receipt():
    """Receipt with no doc_date still matches by amount (no date filter)."""
    _seed()
    con = db.connect()
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,status) "
                "VALUES(108,1,'2026-05-01','PURCHASE',4200,'pending')")
    con.execute("INSERT INTO documents(id,filename,path,vendor,doc_date,amount_cents,status) "
                "VALUES(208,'rcpt.txt','/tmp/rcpt.txt','Vendor',NULL,4200,'unmatched')")
    con.commit()
    matches = webapp.staged_receipt_matches(con)
    assert 108 in matches, f"Should match when receipt has no date, got {matches}"
    con.execute("DELETE FROM staged WHERE id=108")
    con.execute("DELETE FROM documents WHERE id=208")
    con.commit()
    con.close()
    print("PASS: test_match_no_date_on_receipt")


if __name__ == "__main__":
    import ai as ai_mod

    # --- run non-AI tests ---
    test_match_by_amount_and_date()
    test_no_match_different_amount()
    test_no_match_date_too_far()
    test_greedy_one_to_one()
    test_categorize_no_ai()
    test_review_page_shows_receipt_vendor()
    test_match_no_date_on_receipt()

    # --- monkeypatch AI for the categorization test ---
    _original_available = ai_mod.available
    _original_categorize = ai_mod.categorize

    def _fake_available(con):
        return True

    def _fake_categorize(con, txns, names):
        # Return "Tools & Small Equipment" for anything mentioning router/bits/tool
        results = []
        for t in txns:
            desc = t.get("description", "").lower()
            if "router" in desc or "bit" in desc or "tool" in desc:
                results.append("Tools & Small Equipment")
            else:
                results.append(names[0] if names else "Office Supplies")
        return results

    ai_mod.available = _fake_available
    ai_mod.categorize = _fake_categorize
    try:
        test_categorize_from_receipts_with_ai(monkeypatch_ai=True)
    finally:
        ai_mod.available = _original_available
        ai_mod.categorize = _original_categorize

    print("\nAll 8 tests passed.")
