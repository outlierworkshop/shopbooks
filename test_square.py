"""Square online payments: config gating, the create+publish payload (ACH + card, totals match),
and payment sync into the Square clearing account (income gross, tax split, fee booked, idempotent).
No network — square._get/_post are monkeypatched. Isolation: SHOPBOOKS_DATA_DIR before importing db."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_square_")

import db          # noqa: E402
import invoicing   # noqa: E402
import square       # noqa: E402
from testutil import ok  # noqa: E402

db.init()
con = db.connect()

# --- seed accounts, a customer, a taxable item, and a sent invoice ----------------------------
income = con.execute("INSERT INTO accounts(name,kind,type,active) VALUES('Square Test Sales','category','income',1)").lastrowid
tax_acct = invoicing.sales_tax_account_id(con)
if not tax_acct:
    tax_acct = con.execute("INSERT INTO accounts(name,kind,type,active) "
                           "VALUES('Sales Tax Payable','category','liability',1)").lastrowid
db.set_setting(con, "sales_tax_rate", "10")   # 10% so the payment exercises the tax split

cust = con.execute("INSERT INTO customers(name,email) VALUES('Test Buyer','buyer@example.com')").lastrowid
item = con.execute("INSERT INTO items(name,unit_cents,income_account_id,taxable,active) "
                   "VALUES('Widget',10000,?,1,1)", (income,)).lastrowid
inv_id = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,kind) "
                     "VALUES('INV-9001',?,?,?,'sent','invoice')", (cust, '2026-07-14', '2026-08-14')).lastrowid
con.execute("INSERT INTO invoice_items(invoice_id,item_id,description,qty,unit_cents,taxable) "
            "VALUES(?,?,'Widget',1,10000,1)", (inv_id, item))
con.commit()

total = invoicing.invoice_total(con, inv_id)
ok(total == 11000, "invoice total = 11000 (10000 + 10% tax)")

# --- gating: nothing works until Square is configured ------------------------------------------
try:
    square.create_and_publish_invoice(con, inv_id)
    ok(False, "create should require Square configured")
except ValueError:
    ok(True, "create_and_publish_invoice requires configuration")
try:
    square.sync_payments(con)
    ok(False, "sync should require Square configured")
except ValueError:
    ok(True, "sync_payments requires configuration")

db.set_setting(con, "square_access_token", "sq0test-token")
db.set_setting(con, "square_location_id", "LOC1")
db.set_setting(con, "square_environment", "sandbox")
db.set_setting(con, "square_enable_card", "1")
ok(square.configured(con), "configured() true once token + location are set")

# --- mock the Square REST calls (no network) ---------------------------------------------------
seen = {}

def fake_post(con, path, body):
    if path == "/v2/customers":
        return {"customer": {"id": "CUST1"}}
    if path == "/v2/orders":
        seen["order"] = body
        return {"order": {"id": "ORD1"}}
    if path == "/v2/invoices":
        seen["invoice"] = body
        return {"invoice": {"id": "SQINV1", "version": 0, "status": "DRAFT"}}
    if path == "/v2/invoices/SQINV1/publish":
        return {"invoice": {"id": "SQINV1", "version": 1, "status": "UNPAID",
                            "public_url": "https://squareup.com/pay/abc"}}
    raise AssertionError("unexpected POST " + path)

square._post = fake_post

res = square.create_and_publish_invoice(con, inv_id)
con.commit()
ok(res["public_url"] == "https://squareup.com/pay/abc", "publish returns the hosted pay URL")
methods = seen["invoice"]["invoice"]["accepted_payment_methods"]
ok(methods["bank_account"] is True and methods["card"] is True, "both ACH and card are enabled")
line_sum = sum(li["base_price_money"]["amount"] for li in seen["order"]["order"]["line_items"])
ok(line_sum == total, "Square order line totals equal the invoice total (incl. the tax line)")
m = square.get_mapping(con, inv_id)
ok(m["square_invoice_id"] == "SQINV1" and m["public_url"], "mapping row stored with the pay URL")
ok(con.execute("SELECT square_customer_id FROM customers WHERE id=?", (cust,)).fetchone()[0] == "CUST1",
   "the Square customer id is cached on the customer")

# --- sync: the customer paid (PAID) + Square reports a 1% fee -----------------------------------
def fake_get(con, path):
    if path == "/v2/invoices/SQINV1":
        # Square reports the completed amount inside payment_requests, not top-level.
        return {"invoice": {"id": "SQINV1", "status": "PAID", "version": 3, "order_id": "ORD1",
                            "payment_requests": [{"total_completed_amount_money":
                                                  {"amount": 11000, "currency": "USD"}}]}}
    if path == "/v2/orders/ORD1":
        return {"order": {"tenders": [{"payment_id": "PAY1"}]}}
    if path == "/v2/payments/PAY1":
        return {"payment": {"processing_fee": [{"amount_money": {"amount": 110, "currency": "USD"}}]}}
    raise AssertionError("unexpected GET " + path)

square._get = fake_get

out = square.sync_payments(con)
ok(out["recorded"] == 1 and out["fees"] == 1, "sync records the payment and books the fee")

inv_row = con.execute("SELECT status, paid_entry_id FROM invoices WHERE id=?", (inv_id,)).fetchone()
ok(inv_row["status"] == "paid" and inv_row["paid_entry_id"], "invoice marked paid with a posted entry")

clearing = square.clearing_account_id(con, create=False)
ok(clearing is not None, "the Square clearing account was created")
legs = {r["account_id"]: r["amount_cents"] for r in con.execute(
    "SELECT account_id, amount_cents FROM splits WHERE entry_id=?", (inv_row["paid_entry_id"],)).fetchall()}
ok(legs[clearing] == 11000, "full gross deposited into the Square clearing account")
ok(legs[income] == -10000, "income booked gross (pre-tax)")
ok(legs[tax_acct] == -1000, "collected sales tax split to the liability")

bad = con.execute("SELECT COUNT(*) FROM (SELECT entry_id FROM splits GROUP BY entry_id "
                  "HAVING SUM(amount_cents)!=0)").fetchone()[0]
ok(bad == 0, "ledger stays balanced")

fees_acct = con.execute("SELECT id FROM accounts WHERE name=?", (square.SQUARE_FEES_ACCOUNT,)).fetchone()["id"]
fee_leg = con.execute("SELECT amount_cents FROM splits WHERE account_id=?", (fees_acct,)).fetchone()
ok(fee_leg and fee_leg["amount_cents"] == 110, "the 110-cent fee booked to Square Fees expense")
net = con.execute("SELECT COALESCE(SUM(amount_cents),0) FROM splits WHERE account_id=?", (clearing,)).fetchone()[0]
ok(net == 10890, "clearing balance = net payout (gross 11000 minus 110 fee)")

# --- idempotent: syncing again posts nothing new -----------------------------------------------
out2 = square.sync_payments(con)
ok(out2["recorded"] == 0 and out2["fees"] == 0, "re-syncing the same paid invoice posts nothing new")
ok(con.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2,
   "still exactly two entries (payment + fee) after re-sync")

con.close()
print("\nSQUARE TESTS DONE")
