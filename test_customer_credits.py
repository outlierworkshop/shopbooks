import os
import shutil
import tempfile

# Setup test environment first so modules import with the correct database path
os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_credits_")

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

def test_customer_credits_workflow():
    con = db.connect()
    
    # 1. Seed accounts
    checking = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
    income = con.execute("INSERT INTO accounts(name,type,kind) VALUES('Fabrication','income','category')").lastrowid
    cust = con.execute("INSERT INTO customers(name) VALUES('Credit Test Customer')").lastrowid
    
    # 2. Seed invoice of $100 (10000 cents)
    inv_id = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,memo,kind) "
                         "VALUES('INV-100',?,'2026-03-01','2026-03-31','sent','Test Inv','invoice')",
                         (cust,)).lastrowid
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,10000)",
                (inv_id, "Main Item"))
                
    # 3. Seed credit memo of $30 (3000 cents)
    cm_id = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,memo,kind) "
                        "VALUES('CM-200',?,'2026-03-05','2026-03-05','sent','Test CM','credit_memo')",
                        (cust,)).lastrowid
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,3000)",
                (cm_id, "Credit Item"))
                
    con.commit()
    con.close()
    
    # Validate customer outstanding balance is $70 (invoice 100 - CM 30)
    con = db.connect()
    inv_bal = invoicing.invoice_outstanding_balance(con, inv_id)
    cm_bal = invoicing.invoice_outstanding_balance(con, cm_id)
    ok(inv_bal == 10000, f"Invoice balance is 10000, got {inv_bal}")
    ok(cm_bal == -3000, f"Credit memo balance is -3000, got {cm_bal}")
    
    # Check customer outstanding list balance
    cust_row = con.execute("SELECT * FROM customers WHERE id=?", (cust,)).fetchone()
    # Manual query similar to app.py
    invs = con.execute(
        "SELECT id FROM invoices WHERE customer_id=? AND kind IN ('invoice', 'credit_memo') AND status IN ('sent', 'partially_paid')",
        (cust,)
    ).fetchall()
    outstanding = sum(invoicing.invoice_outstanding_balance(con, r["id"]) for r in invs)
    ok(outstanding == 7000, f"Customer outstanding is 7000, got {outstanding}")
    con.close()
    
    # 4. Apply $30 credit memo to the $100 invoice
    r_apply = client.post(
        f"/invoices/{inv_id}/apply-credit",
        data={"credit_invoice_id": str(cm_id), "amount": "30.00", "apply_date": "2026-03-10"},
        follow_redirects=False
    )
    ok(r_apply.status_code == 303, "POST apply-credit redirected")
    
    con = db.connect()
    # Check invoice status and balance
    inv_bal = invoicing.invoice_outstanding_balance(con, inv_id)
    cm_bal = invoicing.invoice_outstanding_balance(con, cm_id)
    ok(inv_bal == 7000, f"Invoice balance is now 7000, got {inv_bal}")
    ok(cm_bal == 0, f"Credit memo balance is now 0, got {cm_bal}")
    
    # Check statuses
    inv_status = con.execute("SELECT status FROM invoices WHERE id=?", (inv_id,)).fetchone()["status"]
    cm_status = con.execute("SELECT status FROM invoices WHERE id=?", (cm_id,)).fetchone()["status"]
    ok(inv_status == "partially_paid", f"Invoice status is partially_paid, got {inv_status}")
    ok(cm_status == "paid", f"Credit memo status is paid, got {cm_status}")
    
    # Check credit applications table
    app_row = con.execute("SELECT * FROM credit_applications").fetchone()
    ok(app_row is not None, "Credit application row exists")
    ok(app_row["amount_cents"] == 3000, "Credit application amount is 3000")
    con.close()
    
    # 5. Delete credit application and verify it reverts
    r_del = client.post(
        f"/credit-applications/{app_row['id']}/delete",
        data={"back": f"/invoices/{inv_id}"},
        follow_redirects=False
    )
    ok(r_del.status_code == 303, "POST delete credit application redirected")
    
    con = db.connect()
    inv_bal = invoicing.invoice_outstanding_balance(con, inv_id)
    cm_bal = invoicing.invoice_outstanding_balance(con, cm_id)
    ok(inv_bal == 10000, f"After deletion, invoice balance reverted to 10000, got {inv_bal}")
    ok(cm_bal == -3000, f"After deletion, CM balance reverted to -3000, got {cm_bal}")
    
    inv_status = con.execute("SELECT status FROM invoices WHERE id=?", (inv_id,)).fetchone()["status"]
    cm_status = con.execute("SELECT status FROM invoices WHERE id=?", (cm_id,)).fetchone()["status"]
    ok(inv_status == "sent", f"Invoice status reverted to sent, got {inv_status}")
    ok(cm_status == "sent", f"CM status reverted to sent, got {cm_status}")
    con.close()
    
    # 6. Overpay the invoice: Post payment of $120 to the $100 invoice
    con = db.connect()
    dep_id = ledger.post_entry(con, "2026-03-15", "Overpayment Acme", [(checking, 12000), (income, -12000)])
    # Link it to the invoice
    con.execute("INSERT INTO invoice_entry_links(invoice_id, entry_id) VALUES(?,?)", (inv_id, dep_id))
    # Update status
    _update_document_status = app._update_document_status
    _update_document_status(con, inv_id)
    con.commit()
    con.close()
    
    con = db.connect()
    # Check invoice status and remaining credit
    inv_status = con.execute("SELECT status FROM invoices WHERE id=?", (inv_id,)).fetchone()["status"]
    ok(inv_status == "paid", f"Invoice status is paid, got {inv_status}")
    
    # Calculate available credit from this overpaid invoice
    pay_total = invoicing.invoice_payments_total(con, inv_id)
    total_cents = invoicing.invoice_total(con, inv_id)
    avail_credit = pay_total - total_cents
    ok(avail_credit == 2000, f"Available overpayment credit is 2000, got {avail_credit}")
    
    # Create second invoice of $50
    inv2_id = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,memo,kind) "
                          "VALUES('INV-102',?,'2026-03-20','2026-04-20','sent','Second Inv','invoice')",
                          (cust,)).lastrowid
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,5000)",
                (inv2_id, "Second Item"))
    con.commit()
    con.close()
    
    # 7. Apply the $20 overpayment credit to the second invoice
    r_apply2 = client.post(
        f"/invoices/{inv2_id}/apply-credit",
        data={"credit_invoice_id": str(inv_id), "amount": "20.00", "apply_date": "2026-03-22"},
        follow_redirects=False
    )
    ok(r_apply2.status_code == 303, "POST apply overpayment credit redirected")
    
    con = db.connect()
    inv2_bal = invoicing.invoice_outstanding_balance(con, inv2_id)
    ok(inv2_bal == 3000, f"Second invoice outstanding balance is 3000, got {inv2_bal}")
    
    # Verify both statuses are updated
    inv1_status = con.execute("SELECT status FROM invoices WHERE id=?", (inv_id,)).fetchone()["status"]
    inv2_status = con.execute("SELECT status FROM invoices WHERE id=?", (inv2_id,)).fetchone()["status"]
    ok(inv1_status == "paid", "First invoice remains paid")
    ok(inv2_status == "partially_paid", "Second invoice is partially_paid")
    con.close()

def test_overapplication_is_capped():
    """A credit larger than the invoice's remaining balance is capped to that balance — the rest of
    the credit stays available for other invoices (no wasted credit)."""
    con = db.connect()
    cust = con.execute("INSERT INTO customers(name) VALUES('Cap Test')").lastrowid
    # $70 invoice
    inv = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,kind) "
                      "VALUES('INV-CAP',?,'2026-05-01','2026-05-31','sent','invoice')", (cust,)).lastrowid
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,7000)", (inv, "job"))
    # $100 credit memo
    cm = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,kind) "
                     "VALUES('CM-CAP',?,'2026-05-02','2026-05-02','sent','credit_memo')", (cust,)).lastrowid
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,10000)", (cm, "credit"))
    con.commit(); con.close()

    # Try to over-apply $100 of credit to the $70 invoice
    r = client.post(f"/invoices/{inv}/apply-credit",
                    data={"credit_invoice_id": str(cm), "amount": "100.00", "apply_date": "2026-05-10"},
                    follow_redirects=False)
    ok(r.status_code == 303, "over-application redirected")

    con = db.connect()
    applied = con.execute("SELECT amount_cents FROM credit_applications WHERE invoice_id=?", (inv,)).fetchone()["amount_cents"]
    ok(applied == 7000, f"credit was capped to the invoice's $70 balance, got {applied}")
    ok(invoicing.invoice_outstanding_balance(con, inv) == 0, "invoice is now fully covered ($0 outstanding)")
    # the remaining $30 of the credit memo is still available
    ok(invoicing.invoice_outstanding_balance(con, cm) == -3000, "the unused $30 of credit stays available")
    ok(con.execute("SELECT status FROM invoices WHERE id=?", (inv,)).fetchone()["status"] == "paid",
       "invoice marked paid; credit memo only partially applied")
    con.close()


def test_apply_from_credit_memo_side():
    """Feature #2: apply a credit memo to a chosen invoice from the CREDIT MEMO's side."""
    con = db.connect()
    cust = con.execute("INSERT INTO customers(name) VALUES('Memo Side')").lastrowid
    inv = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,kind) "
                      "VALUES('INV-MS',?,'2026-06-01','2026-06-30','sent','invoice')", (cust,)).lastrowid
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,5000)", (inv, "job"))
    cm = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,kind) "
                     "VALUES('CM-MS',?,'2026-06-02','2026-06-02','sent','credit_memo')", (cust,)).lastrowid
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,8000)", (cm, "credit"))
    con.commit(); con.close()

    r = client.post(f"/credit-memos/{cm}/apply",
                    data={"invoice_id": str(inv), "amount": "50.00", "apply_date": "2026-06-10"},
                    follow_redirects=False)
    ok(r.status_code == 303 and r.headers["location"].startswith(f"/invoices/{cm}"),
       "apply-from-memo redirects back to the credit memo")
    con = db.connect()
    ok(invoicing.invoice_outstanding_balance(con, inv) == 0, "target invoice fully covered from the memo side")
    ok(con.execute("SELECT status FROM invoices WHERE id=?", (inv,)).fetchone()["status"] == "paid", "invoice marked paid")
    ok(invoicing.invoice_outstanding_balance(con, cm) == -3000, "credit memo has $30 left after applying $50 of $80")
    con.close()


def test_overpayment_to_credit_memo():
    """Feature #4: turn an invoice's overpayment into a standalone credit memo (no double-counting)."""
    con = db.connect()
    chk = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
    inc = con.execute("SELECT id FROM accounts WHERE name='Fabrication'").fetchone()["id"]
    cust = con.execute("INSERT INTO customers(name) VALUES('Overpay')").lastrowid
    inv = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,kind) "
                      "VALUES('INV-OP',?,'2026-07-01','2026-07-31','sent','invoice')", (cust,)).lastrowid
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,10000)", (inv, "job"))
    dep = ledger.post_entry(con, "2026-07-05", "Overpay dep", [(chk, 13000), (inc, -13000)])  # paid $130 on a $100 invoice
    con.execute("INSERT INTO invoice_entry_links(invoice_id, entry_id) VALUES(?,?)", (inv, dep))
    app._update_document_status(con, inv)
    con.commit()
    before = invoicing.customer_available_credit(con, cust)
    con.close()
    ok(before == 3000, f"the $30 overpayment is available as credit before converting (got {before})")

    r = client.post(f"/invoices/{inv}/to-credit-memo", follow_redirects=False)
    ok(r.status_code == 303 and "/invoices/" in r.headers["location"], "overpayment->credit redirects to the new memo")
    cm_id = int(r.headers["location"].split("/invoices/")[1].split("?")[0])

    con = db.connect()
    cm = con.execute("SELECT number, kind FROM invoices WHERE id=?", (cm_id,)).fetchone()
    ok(cm["kind"] == "credit_memo" and cm["number"].startswith("CM-"), "a CM- credit memo was created")
    ok(abs(invoicing.invoice_total(con, cm_id)) == 3000, "the credit memo is for the $30 excess")
    # no double-counting: total available credit for the customer is still exactly $30
    ok(invoicing.customer_available_credit(con, cust) == 3000, "still exactly $30 available (excess moved, not duplicated)")
    ok(con.execute("SELECT status FROM invoices WHERE id=?", (inv,)).fetchone()["status"] == "paid",
       "the source invoice stays paid")
    con.close()


def test_available_credit_surfaces_in_briefing():
    """Feature #1: unused customer credit shows up in the dashboard briefing."""
    import insights
    con = db.connect()
    b = insights.briefing(con, "2026-08-01")
    ok(b["customer_credit"] > 0, "briefing reports a nonzero customer-credit figure")
    ok(any("customer credit" in a["text"] for a in b["attention"]),
       "briefing surfaces an 'unused customer credit to apply' item")
    con.close()


if __name__ == "__main__":
    test_customer_credits_workflow()
    test_overapplication_is_capped()
    test_apply_from_credit_memo_side()
    test_overpayment_to_credit_memo()
    test_available_credit_surfaces_in_briefing()
    print("\nCUSTOMER CREDITS TESTS DONE")
