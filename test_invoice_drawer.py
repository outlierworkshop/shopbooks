import os
import shutil
import tempfile
from datetime import date as date_cls

# Setup test environment first so modules import with the correct database path
os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_invdrawer_")

import db
import app
import ledger
from fastapi.testclient import TestClient

db.init()
client = TestClient(app.app)

def ok(cond, msg):
    assert cond, f"FAIL: {msg}"
    print(f"PASS: {msg}")

def test_invoice_multi_payment_matching():
    con = db.connect()
    
    # 1. Seed accounts
    checking = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
    income = con.execute("INSERT INTO accounts(name,type,kind) VALUES('Fabrication','income','category')").lastrowid
    cust = con.execute("INSERT INTO customers(name) VALUES('Acme Corp')").lastrowid
    
    # 2. Seed invoice
    inv_id = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,memo) "
                         "VALUES('1055',?,?,?,'sent','Imported from QuickBooks')",
                         (cust, "2026-03-01", "2026-03-31")).lastrowid
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,120000)",
                (inv_id, "Imported from QuickBooks (invoice total)"))
                
    # 3. Seed two separate posted deposit entries
    dep1 = ledger.post_entry(con, "2026-03-10", "Partial ACH Acme 1", [(checking, 50000), (income, -50000)])
    dep2 = ledger.post_entry(con, "2026-03-25", "Partial ACH Acme 2", [(checking, 70000), (income, -70000)])
    
    con.commit()
    con.close()
    
    # 4. Match both deposits to the invoice via save-matches route
    r_match = client.post(
        f"/invoices/{inv_id}/save-matches",
        data={"entry_ids": [str(dep1), str(dep2)]},
        follow_redirects=False
    )
    ok(r_match.status_code == 303, "POST save-matches redirected")
    
    con = db.connect()
    # Verify invoice status, paid_date, matched_entry_id (should be dep1 as first index fallback)
    inv = con.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    ok(inv["status"] == "paid", "Invoice status is paid")
    ok(inv["paid_date"] == "2026-03-25", "Paid date is the latest deposit date (2026-03-25)")
    ok(inv["matched_entry_id"] == dep1, "matched_entry_id is set to first entry_id")
    
    # Verify both links exist in invoice_entry_links
    links = con.execute("SELECT * FROM invoice_entry_links WHERE invoice_id=? ORDER BY entry_id", (inv_id,)).fetchall()
    ok(len(links) == 2, "Two links exist in invoice_entry_links")
    ok(links[0]["entry_id"] == dep1, "First link matches dep1")
    ok(links[1]["entry_id"] == dep2, "Second link matches dep2")
    con.close()
    
    # 5. Delete dep2 (latest deposit)
    con = db.connect()
    ledger.delete_entry(con, dep2)
    con.commit()
    con.close()
    
    # Verify the remaining links are recomputed, paid_date shifts to dep1's date, status remains paid
    con = db.connect()
    inv = con.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    ok(inv["status"] == "paid", "Invoice status remains paid")
    ok(inv["paid_date"] == "2026-03-10", "Paid date updated to dep1's date (2026-03-10)")
    ok(inv["matched_entry_id"] == dep1, "matched_entry_id is still dep1")
    
    links = con.execute("SELECT * FROM invoice_entry_links WHERE invoice_id=?", (inv_id,)).fetchall()
    ok(len(links) == 1, "Only one link remains in invoice_entry_links")
    ok(links[0]["entry_id"] == dep1, "Remaining link is dep1")
    con.close()
    
    # 6. Delete dep1 (first deposit)
    con = db.connect()
    ledger.delete_entry(con, dep1)
    con.commit()
    con.close()
    
    # Verify status reverts to sent, paid_date is NULL, matched_entry_id is NULL
    con = db.connect()
    inv = con.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    ok(inv["status"] == "sent", "Invoice status reverted to sent")
    ok(inv["paid_date"] is None, "Paid date is NULL")
    ok(inv["matched_entry_id"] is None, "matched_entry_id is NULL")
    
    links = con.execute("SELECT * FROM invoice_entry_links WHERE invoice_id=?", (inv_id,)).fetchall()
    ok(len(links) == 0, "No links remain in invoice_entry_links")
    con.close()

if __name__ == "__main__":
    test_invoice_multi_payment_matching()
    shutil.rmtree(os.environ["SHOPBOOKS_DATA_DIR"], ignore_errors=True)
    print("\nINVOICE MULTI-PAYMENT MATCHING TESTS DONE")
