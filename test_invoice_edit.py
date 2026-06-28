import os
import shutil
import tempfile

# Setup test environment first so modules import with the correct database path
os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_invoiceedit_")

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

def test_invoice_and_estimate_editing():
    con = db.connect()
    
    # 1. Seed accounts and customers
    checking = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
    income = con.execute("INSERT INTO accounts(name,type,kind) VALUES('Fabrication','income','category')").lastrowid
    cust1 = con.execute("INSERT INTO customers(name) VALUES('Customer One')").lastrowid
    cust2 = con.execute("INSERT INTO customers(name) VALUES('Customer Two')").lastrowid
    
    # 2. Seed invoice of $100 (10000 cents) for Customer One
    inv_id = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,memo) "
                         "VALUES('INV-1056',?,?,?,'sent','Edit Test')",
                         (cust1, "2026-03-01", "2026-03-31")).lastrowid
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,10000)",
                (inv_id, "Original Item"))
                
    # 3. Match a payment of $40 (making it partially paid)
    dep1 = ledger.post_entry(con, "2026-03-10", "Partial Acme 1", [(checking, 4000), (income, -4000)])
    con.execute("INSERT INTO invoice_entry_links(invoice_id, entry_id) VALUES(?,?)", (inv_id, dep1))
    con.execute("UPDATE invoices SET status='partially_paid', paid_date='2026-03-10', matched_entry_id=? WHERE id=?", (dep1, inv_id))
    # Also sync entry's customer
    con.execute("UPDATE entries SET customer_id=? WHERE id=?", (cust1, dep1))
    con.commit()
    con.close()
    
    # 4. Fetch the GET edit page to make sure it loads
    res_get = client.get(f"/invoices/{inv_id}/edit")
    ok(res_get.status_code == 200, "GET /invoices/{id}/edit returns 200")
    ok("Original Item" in res_get.text, "GET edit page renders existing items")
    
    # 5. POST edits: Change customer to cust2, due date to 2026-04-15, change item to $30 (3000 cents)
    # Total is now $30. Matched payment is $40.
    # Therefore, total_payments ($40) >= new_total ($30), so status should update to 'paid'!
    res_post = client.post(
        f"/invoices/{inv_id}/edit",
        data={
            "customer_id": str(cust2),
            "date": "2026-03-01",
            "due_date": "2026-04-15",
            "memo": "Updated Memo",
            "item_desc": ["Updated Item"],
            "item_qty": ["1"],
            "item_price": ["30.00"]
        },
        follow_redirects=False
    )
    ok(res_post.status_code == 303, "POST /invoices/{id}/edit redirects")
    
    con = db.connect()
    # Verify invoice fields are updated
    inv = con.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    ok(inv["customer_id"] == cust2, "Customer updated to cust2")
    ok(inv["due_date"] == "2026-04-15", "Due date updated to 2026-04-15")
    ok(inv["memo"] == "Updated Memo", "Memo updated")
    ok(inv["status"] == "paid", f"Invoice status updated to paid, got {inv['status']}")
    
    # Verify items are updated
    items = con.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (inv_id,)).fetchall()
    ok(len(items) == 1, "Exactly one line item remains")
    ok(items[0]["description"] == "Updated Item", "Item description updated")
    ok(items[0]["unit_cents"] == 3000, "Item unit price updated")
    
    # Verify matched entry's customer ID is synced to cust2!
    entry = con.execute("SELECT customer_id FROM entries WHERE id=?", (dep1,)).fetchone()
    ok(entry["customer_id"] == cust2, f"Matched entry customer_id synced to cust2 (got {entry['customer_id']})")
    con.close()
    
    # 6. Test Estimate Editing
    con = db.connect()
    est_id = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,memo,kind) "
                         "VALUES('EST-1056',?,?,?,'draft','Est Memo','estimate')",
                         (cust1, "2026-03-01", "2026-03-31")).lastrowid
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,5000)",
                (est_id, "Est Item"))
    con.commit()
    con.close()
    
    res_est_get = client.get(f"/estimates/{est_id}/edit")
    ok(res_est_get.status_code == 200, "GET /estimates/{id}/edit returns 200")
    
    res_est_post = client.post(
        f"/estimates/{est_id}/edit",
        data={
            "customer_id": str(cust2),
            "date": "2026-03-02",
            "due_date": "2026-04-20",
            "memo": "Updated Est Memo",
            "item_desc": ["New Est Item"],
            "item_qty": ["2"],
            "item_price": ["25.00"]
        },
        follow_redirects=False
    )
    ok(res_est_post.status_code == 303, "POST /estimates/{id}/edit redirects")
    
    con = db.connect()
    est = con.execute("SELECT * FROM invoices WHERE id=?", (est_id,)).fetchone()
    ok(est["customer_id"] == cust2, "Estimate customer updated")
    ok(est["date"] == "2026-03-02", "Estimate date updated")
    ok(est["due_date"] == "2026-04-20", "Estimate due_date updated")
    ok(est["memo"] == "Updated Est Memo", "Estimate memo updated")
    
    est_items = con.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (est_id,)).fetchall()
    ok(len(est_items) == 1, "Exactly one estimate item remains")
    ok(est_items[0]["description"] == "New Est Item", "Estimate item description updated")
    ok(est_items[0]["qty"] == 2.0, "Estimate item qty updated")
    ok(est_items[0]["unit_cents"] == 2500, "Estimate item price updated")
    con.close()

if __name__ == "__main__":
    test_invoice_and_estimate_editing()
    shutil.rmtree(os.environ["SHOPBOOKS_DATA_DIR"], ignore_errors=True)
    print("\nINVOICE AND ESTIMATE EDITING TESTS DONE")
