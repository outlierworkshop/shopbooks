"""Test open invoice-to-staged-deposit matching and categorization.

Verifies:
 1. staged_invoice_matches finds a pair when amounts match and date is within range [-5, 120] days
 2. staged_invoice_matches returns empty when amounts differ
 3. staged_invoice_matches returns empty when dates are out of range (< -5 or > 120 days)
 4. Each invoice and staged deposit is used at most once (greedy, nearest date)
 5. _categorize_from_invoices updates staged category_id using fallback (history / default)
 6. _categorize_from_invoices updates staged category_id using AI (monkeypatched)
 7. Review page shows invoice number and customer on matched rows
 8. Auto-pay on post updates invoice status to paid and links matched_entry_id
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
SALES_ACH_ID = _con.execute("SELECT id FROM accounts WHERE name='Sales - ACH / Invoices'").fetchone()["id"]
OTHER_INC_ID = _con.execute("SELECT id FROM accounts WHERE name='Other Income'").fetchone()["id"]
_con.close()


def _seed():
    """Create a batch for the test bank account and a customer."""
    con = db.connect()
    con.execute("INSERT OR IGNORE INTO batches(id,filename,account_id,imported_at) VALUES(1,'test.pdf',?,?)",
                (BANK_ID, '2026-01-01'))
    con.execute("INSERT OR IGNORE INTO customers(id,name) VALUES(999,'Test Customer')")
    con.commit()
    con.close()


def test_match_by_amount_and_date():
    """Amount match + deposit date within [-5, 120] days of invoice date -> found."""
    _seed()
    con = db.connect()
    # invoice dated 2026-01-10
    con.execute("INSERT INTO invoices(id,number,customer_id,date,due_date,status) "
                "VALUES(300,'INV-300',999,'2026-01-10','2026-01-30','sent')")
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(300,'Test Items',1,12000)")
    
    # deposit date 2026-01-15 (5 days after invoice date -> within [-5, 120] range)
    # deposit amount is negative (money in)
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,status) "
                "VALUES(400,1,'2026-01-15','ACH DEPOSIT CUSTOMER',-12000,'pending')")
    con.commit()
    
    matches = webapp.staged_invoice_matches(con)
    assert 400 in matches, f"Expected staged id 400 in matches, got {matches}"
    assert matches[400]["id"] == 300
    assert matches[400]["number"] == "INV-300"
    assert matches[400]["customer"] == "Test Customer"
    
    con.execute("DELETE FROM staged WHERE id=400")
    con.execute("DELETE FROM invoices WHERE id=300")
    con.execute("DELETE FROM invoice_items WHERE invoice_id=300")
    con.commit()
    con.close()
    print("PASS: test_match_by_amount_and_date")


def test_no_match_different_amount():
    """Different amounts -> no match."""
    _seed()
    con = db.connect()
    con.execute("INSERT INTO invoices(id,number,customer_id,date,due_date,status) "
                "VALUES(301,'INV-301',999,'2026-01-10','2026-01-30','sent')")
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(301,'Test Items',1,12000)")
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,status) "
                "VALUES(401,1,'2026-01-15','ACH DEPOSIT',-99999,'pending')")
    con.commit()
    
    matches = webapp.staged_invoice_matches(con)
    assert 401 not in matches
    
    con.execute("DELETE FROM staged WHERE id=401")
    con.execute("DELETE FROM invoices WHERE id=301")
    con.execute("DELETE FROM invoice_items WHERE invoice_id=301")
    con.commit()
    con.close()
    print("PASS: test_no_match_different_amount")


def test_no_match_date_out_of_range():
    """Deposit date too far (>120 days or < -5 days) -> no match."""
    _seed()
    con = db.connect()
    # invoice dated 2026-01-10
    con.execute("INSERT INTO invoices(id,number,customer_id,date,due_date,status) "
                "VALUES(302,'INV-302',999,'2026-01-10','2026-01-30','sent')")
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(302,'Test Items',1,12000)")
    
    # deposit date 2026-05-15 (125 days after -> too far)
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,status) "
                "VALUES(402,1,'2026-05-15','ACH DEPOSIT',-12000,'pending')")
    con.commit()
    
    matches = webapp.staged_invoice_matches(con)
    assert 402 not in matches
    
    con.execute("DELETE FROM staged WHERE id=402")
    con.execute("DELETE FROM invoices WHERE id=302")
    con.execute("DELETE FROM invoice_items WHERE invoice_id=302")
    con.commit()
    con.close()
    print("PASS: test_no_match_date_out_of_range")


def test_greedy_one_to_one():
    """Two staged rows same amount, one invoice -> only one match (nearest date)."""
    _seed()
    con = db.connect()
    con.execute("INSERT INTO invoices(id,number,customer_id,date,due_date,status) "
                "VALUES(303,'INV-303',999,'2026-01-10','2026-01-30','sent')")
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(303,'Test Items',1,12000)")
    
    # Deposit A: 2026-01-12 (distance 2 days)
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,status) "
                "VALUES(403,1,'2026-01-12','ACH DEPOSIT A',-12000,'pending')")
    # Deposit B: 2026-01-11 (distance 1 day - nearest!)
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,status) "
                "VALUES(404,1,'2026-01-11','ACH DEPOSIT B',-12000,'pending')")
    con.commit()
    
    matches = webapp.staged_invoice_matches(con)
    assert len(matches) == 1
    assert 404 in matches, f"Expected nearest date staged 404, got {matches}"
    
    con.execute("DELETE FROM staged WHERE id IN (403,404)")
    con.execute("DELETE FROM invoices WHERE id=303")
    con.execute("DELETE FROM invoice_items WHERE invoice_id=303")
    con.commit()
    con.close()
    print("PASS: test_greedy_one_to_one")


def test_categorize_fallback_and_history():
    """Verify history-based fallback and default account selection when AI is off."""
    _seed()
    con = db.connect()
    con.execute("UPDATE settings SET value='' WHERE key='anthropic_api_key'")
    
    # 1. Past payment history check: Create a past paid invoice for customer 999
    # linked to a ledger entry with OTHER_INC_ID
    con.execute("INSERT INTO entries(id,date,payee) VALUES(500,'2026-01-01','Acme payment')")
    con.execute("INSERT INTO splits(entry_id,account_id,amount_cents) VALUES(500,?,?)", (OTHER_INC_ID, -12000))
    con.execute("INSERT INTO invoices(id,number,customer_id,date,due_date,status,matched_entry_id) "
                "VALUES(304,'INV-304',999,'2026-01-01','2026-01-15','paid',500)")
                
    # 2. Open invoice and staged row to test
    con.execute("INSERT INTO invoices(id,number,customer_id,date,due_date,status) "
                "VALUES(305,'INV-305',999,'2026-01-10','2026-01-30','sent')")
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(305,'Services',1,12000)")
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,category_id,status) "
                "VALUES(405,1,'2026-01-15','ACH DEPOSIT',-12000,NULL,'pending')")
    con.commit()
    
    matched, categorized, err = webapp._categorize_from_invoices(con)
    assert matched == 1
    assert categorized == 1
    assert "AI is off" in err
    
    row = con.execute("SELECT category_id FROM staged WHERE id=405").fetchone()
    assert row["category_id"] == OTHER_INC_ID, f"Expected category {OTHER_INC_ID}, got {row['category_id']}"
    
    con.execute("DELETE FROM staged WHERE id=405")
    con.execute("DELETE FROM invoices WHERE id IN (304,305)")
    con.execute("DELETE FROM invoice_items WHERE invoice_id=305")
    con.execute("DELETE FROM splits WHERE entry_id=500")
    con.execute("DELETE FROM entries WHERE id=500")
    con.commit()
    con.close()
    print("PASS: test_categorize_fallback_and_history")


def test_categorize_from_invoices_with_ai(monkeypatch_ai):
    """AI suggests category from invoice details -> staged category updated."""
    _seed()
    con = db.connect()
    con.execute("INSERT INTO invoices(id,number,customer_id,date,due_date,status) "
                "VALUES(306,'INV-306',999,'2026-01-10','2026-01-30','sent')")
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(306,'Services',1,12000)")
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,category_id,status) "
                "VALUES(406,1,'2026-01-15','ACH DEPOSIT',-12000,NULL,'pending')")
    con.commit()
    
    matched, categorized, err = webapp._categorize_from_invoices(con)
    assert matched == 1
    assert categorized == 1
    assert err is None
    
    row = con.execute("SELECT category_id FROM staged WHERE id=406").fetchone()
    assert row["category_id"] == SALES_ACH_ID
    
    con.execute("DELETE FROM staged WHERE id=406")
    con.execute("DELETE FROM invoices WHERE id=306")
    con.execute("DELETE FROM invoice_items WHERE invoice_id=306")
    con.commit()
    con.close()
    print("PASS: test_categorize_from_invoices_with_ai")


def test_review_page_shows_invoice():
    """GET /review shows the 📄 indicator and invoice number/customer details."""
    _seed()
    con = db.connect()
    con.execute("INSERT INTO invoices(id,number,customer_id,date,due_date,status) "
                "VALUES(307,'INV-307',999,'2026-01-10','2026-01-30','sent')")
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(307,'Services',1,12000)")
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,status) "
                "VALUES(407,1,'2026-01-15','ACH DEPOSIT',-12000,'pending')")
    con.commit()
    con.close()
    
    resp = client.get("/review")
    assert resp.status_code == 200
    assert "\U0001f4c4" in resp.text, "Expected document icon 📄 in HTML"
    assert "INV-307" in resp.text, "Expected invoice number in HTML"
    assert "Test Customer" in resp.text, "Expected customer name in HTML"
    
    con = db.connect()
    con.execute("DELETE FROM staged WHERE id=407")
    con.execute("DELETE FROM invoices WHERE id=307")
    con.execute("DELETE FROM invoice_items WHERE invoice_id=307")
    con.commit()
    con.close()
    print("PASS: test_review_page_shows_invoice")


def test_auto_pay_on_post():
    """Posting the staged transaction automatically marks the invoice as paid."""
    _seed()
    con = db.connect()
    con.execute("INSERT INTO invoices(id,number,customer_id,date,due_date,status) "
                "VALUES(308,'INV-308',999,'2026-01-10','2026-01-30','sent')")
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(308,'Services',1,12000)")
    con.execute("INSERT INTO staged(id,batch_id,date,description,amount_cents,status) "
                "VALUES(408,1,'2026-01-15','ACH DEPOSIT',-12000,'pending')")
    con.commit()
    con.close()
    
    # Post the staged transaction to SALES_ACH_ID
    resp = client.post("/review", data={"post_one": "408", f"cat_408": str(SALES_ACH_ID)}, follow_redirects=False)
    assert resp.status_code == 303
    
    con = db.connect()
    inv_row = con.execute("SELECT status, paid_date, matched_entry_id FROM invoices WHERE id=308").fetchone()
    assert inv_row["status"] == "paid"
    assert inv_row["paid_date"] == "2026-01-15"
    assert inv_row["matched_entry_id"] is not None
    
    # Cleanup
    entry_id = inv_row["matched_entry_id"]
    con.execute("DELETE FROM staged WHERE id=408")
    con.execute("DELETE FROM invoices WHERE id=308")
    con.execute("DELETE FROM invoice_items WHERE invoice_id=308")
    con.execute("DELETE FROM splits WHERE entry_id=?", (entry_id,))
    con.execute("DELETE FROM entries WHERE id=?", (entry_id,))
    con.commit()
    con.close()
    print("PASS: test_auto_pay_on_post")


if __name__ == "__main__":
    import ai as ai_mod
    
    # Run tests without AI first
    test_match_by_amount_and_date()
    test_no_match_different_amount()
    test_no_match_date_out_of_range()
    test_greedy_one_to_one()
    test_categorize_fallback_and_history()
    test_review_page_shows_invoice()
    test_auto_pay_on_post()
    
    # Monkeypatch AI for the AI test
    _original_available = ai_mod.available
    _original_categorize = ai_mod.categorize
    
    def _fake_available(con):
        return True
        
    def _fake_categorize(con, txns, names):
        # Always return the name of SALES_ACH_ID (which is "Sales - ACH / Invoices")
        return ["Sales - ACH / Invoices" for _ in txns]
        
    ai_mod.available = _fake_available
    ai_mod.categorize = _fake_categorize
    try:
        test_categorize_from_invoices_with_ai(monkeypatch_ai=True)
    finally:
        ai_mod.available = _original_available
        ai_mod.categorize = _original_categorize
        
    print("\nAll 8 invoice tests passed.")
