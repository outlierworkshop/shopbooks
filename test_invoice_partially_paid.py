import os
import shutil
import tempfile

# Setup test environment first so modules import with the correct database path
os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_partiallypaid_")

import db
import app
import ledger
import invoicing
from fastapi.testclient import TestClient

db.init()
client = TestClient(app.app)

def ok(cond, msg):
    assert cond, f"FAIL: {msg}"
    print(f"PASS: {msg}")

def test_invoice_partially_paid_matching():
    con = db.connect()
    
    # 1. Seed accounts
    checking = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
    income = con.execute("INSERT INTO accounts(name,type,kind) VALUES('Fabrication','income','category')").lastrowid
    cust = con.execute("INSERT INTO customers(name) VALUES('Test Customer')").lastrowid
    
    # 2. Seed invoice of $100 (10000 cents)
    inv_id = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,memo) "
                         "VALUES('1056',?,?,?,'sent','Partially Paid Test')",
                         (cust, "2026-03-01", "2026-03-31")).lastrowid
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,10000)",
                (inv_id, "Item Description"))
                
    # 3. Seed two separate posted deposit entries
    dep1 = ledger.post_entry(con, "2026-03-10", "Partial Acme 1", [(checking, 4000), (income, -4000)])
    dep2 = ledger.post_entry(con, "2026-03-25", "Partial Acme 2", [(checking, 6000), (income, -6000)])
    
    con.commit()
    con.close()
    
    # 4. Match the first deposit ($40) via save-matches route
    r_match = client.post(
        f"/invoices/{inv_id}/save-matches",
        data={"entry_ids": [str(dep1)]},
        follow_redirects=False
    )
    ok(r_match.status_code == 303, "POST save-matches redirected")
    
    con = db.connect()
    # Verify invoice status is partially_paid, paid_date is set, payments_total is 4000
    inv = con.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    ok(inv["status"] == "partially_paid", f"Invoice status is partially_paid, got {inv['status']}")
    ok(inv["paid_date"] == "2026-03-10", "Paid date is 2026-03-10")
    
    # Verify payments_total
    pay_total = invoicing.invoice_payments_total(con, inv_id)
    ok(pay_total == 4000, f"payments_total is 4000, got {pay_total}")
    
    # Verify entries.customer_id is set to cust
    entry = con.execute("SELECT customer_id FROM entries WHERE id=?", (dep1,)).fetchone()
    ok(entry["customer_id"] == cust, "entries.customer_id is set to customer")
    
    # Verify AR aging reflects outstanding balance of 6000 cents
    aging = invoicing.ar_aging(con, "2026-03-31")
    ok(aging["total"] == 6000, f"AR aging total is 6000, got {aging['total']}")
    con.close()
    
    # 5. Match both deposits ($40 + $60 = $100)
    r_match2 = client.post(
        f"/invoices/{inv_id}/save-matches",
        data={"entry_ids": [str(dep1), str(dep2)]},
        follow_redirects=False
    )
    ok(r_match2.status_code == 303, "POST save-matches 2 redirected")
    
    con = db.connect()
    # Verify invoice status is paid, paid_date is latest (2026-03-25), payments_total is 10000
    inv = con.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    ok(inv["status"] == "paid", "Invoice status is paid")
    ok(inv["paid_date"] == "2026-03-25", "Paid date is 2026-03-25")
    pay_total = invoicing.invoice_payments_total(con, inv_id)
    ok(pay_total == 10000, "payments_total is 10000")
    
    # Verify both entry customer_ids are set
    e1 = con.execute("SELECT customer_id FROM entries WHERE id=?", (dep1,)).fetchone()
    e2 = con.execute("SELECT customer_id FROM entries WHERE id=?", (dep2,)).fetchone()
    ok(e1["customer_id"] == cust, "dep1 customer_id is set")
    ok(e2["customer_id"] == cust, "dep2 customer_id is set")
    con.close()
    
    # 6. Delete dep2 (leaving only dep1 matched)
    con = db.connect()
    ledger.delete_entry(con, dep2)
    con.commit()
    con.close()
    
    con = db.connect()
    # Verify status reverted to partially_paid, paid_date updated to dep1's date, and dep2 customer_id is cleaned up
    inv = con.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    ok(inv["status"] == "partially_paid", "Invoice status reverted to partially_paid")
    ok(inv["paid_date"] == "2026-03-10", "Paid date updated to 2026-03-10")
    
    # Verify AR aging outstanding balance is back to 6000
    aging = invoicing.ar_aging(con, "2026-03-31")
    ok(aging["total"] == 6000, "AR aging total reverted to 6000")
    con.close()
    
    # 7. Record remaining payment of $60 via Record Payment endpoint
    r_pay = client.post(
        f"/invoices/{inv_id}/pay",
        data={"paid_date": "2026-03-28", "bank_id": str(checking), "income_id": str(income)},
        follow_redirects=False
    )
    ok(r_pay.status_code == 303, "POST pay redirected")
    
    con = db.connect()
    # Verify invoice status is paid, and paid_entry_id is populated
    inv = con.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    ok(inv["status"] == "paid", "Invoice status became paid via posted payment")
    ok(inv["paid_date"] == "2026-03-28", "Paid date set to 2026-03-28")
    ok(inv["paid_entry_id"] is not None, "paid_entry_id is set")
    
    # Verify the posted entry amount is the outstanding amount ($60)
    splits = con.execute("SELECT amount_cents FROM splits WHERE entry_id=? AND account_id=?", 
                         (inv["paid_entry_id"], checking)).fetchone()
    ok(splits["amount_cents"] == 6000, f"Posted split amount is 6000, got {splits['amount_cents']}")
    
    # Verify the posted entry has customer_id set
    entry = con.execute("SELECT customer_id FROM entries WHERE id=?", (inv["paid_entry_id"],)).fetchone()
    ok(entry["customer_id"] == cust, "posted entry customer_id is set")
    con.close()
    
    # 8. Test set-customer direct route
    r_cust = client.post(
        f"/entry/{dep1}/customer",
        data={"customer_id": ""}, # untag
        follow_redirects=False
    )
    ok(r_cust.status_code == 303, "POST untag customer redirected")
    
    con = db.connect()
    entry = con.execute("SELECT customer_id FROM entries WHERE id=?", (dep1,)).fetchone()
    ok(entry["customer_id"] is None, "customer_id was untagged")
    con.close()

if __name__ == "__main__":
    test_invoice_partially_paid_matching()
    shutil.rmtree(os.environ["SHOPBOOKS_DATA_DIR"], ignore_errors=True)
    print("\nINVOICE PARTIALLY PAID TESTS DONE")
