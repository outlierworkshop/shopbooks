"""Reconciliation adjustment test. Isolated via SHOPBOOKS_DATA_DIR."""
import io
import os
import tempfile
from pathlib import Path

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_rec_adj_")
import db  # noqa: E402
import app as appmod  # noqa: E402
import ledger  # noqa: E402
import reconcile  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ok = lambda c, w: print(("PASS" if c else "FAIL"), w)
client = TestClient(appmod.app)

con = db.connect()
try:
    # 1. Setup checking and card names
    con.execute("UPDATE accounts SET name='EasternBankBusinessChecking' WHERE id=1")
    con.execute("UPDATE accounts SET name='ChaseInk' WHERE id=2")
    con.commit()
    
    # 2. Get account IDs
    checking_id = con.execute("SELECT id FROM accounts WHERE name='EasternBankBusinessChecking'").fetchone()["id"]
    card_id = con.execute("SELECT id FROM accounts WHERE name='ChaseInk'").fetchone()["id"]
    uncat_id = con.execute("SELECT id FROM accounts WHERE name='Uncategorized Expense'").fetchone()["id"]
    
    # Check initial balances
    bal_checking = ledger.raw_balance(con, checking_id)
    bal_card = ledger.raw_balance(con, card_id)
finally:
    con.close()

# TEST 1: Reconcile Asset Account (Checking)
# Statement shows 120.00, book balance is 0.00 (difference of +120.00)
response1 = client.post("/reconcile/adjust", data={
    "account_id": str(checking_id),
    "statement_date": "2026-01-31",
    "statement_balance": "120.00",
    "difference": "12000",  # 120.00 in cents
    "offset_account_id": str(uncat_id),
    "payee": "Adjustment Checking",
    "memo": "Checking discrepancy",
}, follow_redirects=False)

ok(response1.status_code == 303, f"Checking adjustment redirects (got {response1.status_code})")

# Verify checking balance is now 120.00
con = db.connect()
try:
    new_bal_checking = ledger.raw_balance(con, checking_id)
    ok(new_bal_checking == 12000, f"Checking raw balance adjusted to 120.00 (got {new_bal_checking})")
    
    # Verify reconciliation checkpoint recorded
    rec = reconcile.last_reconciliation(con, checking_id)
    ok(rec is not None, "Checking reconciliation checkpoint exists")
    ok(rec["statement_balance_cents"] == 12000, f"Statement balance recorded (got {rec['statement_balance_cents']})")
    ok(rec["difference_cents"] == 0, f"Recorded difference is 0 (got {rec['difference_cents']})")
finally:
    con.close()


# TEST 2: Reconcile Liability Account (Credit Card)
# Statement shows 50.00 owed (which is credit raw balance -50.00, display balance 50.00).
# Book balance is 0.00 (difference of +50.00 display cents).
response2 = client.post("/reconcile/adjust", data={
    "account_id": str(card_id),
    "statement_date": "2026-01-31",
    "statement_balance": "50.00",
    "difference": "5000",  # 50.00 in cents
    "offset_account_id": str(uncat_id),
    "payee": "Adjustment Card",
    "memo": "Card discrepancy",
}, follow_redirects=False)

ok(response2.status_code == 303, f"Card adjustment redirects (got {response2.status_code})")

# Verify card balance is now -50.00 (raw) which displays as 50.00 owed
con = db.connect()
try:
    new_bal_card = ledger.raw_balance(con, card_id)
    ok(new_bal_card == -5000, f"Card raw balance adjusted to -50.00 (got {new_bal_card})")
    
    # Verify reconciliation checkpoint recorded
    rec = reconcile.last_reconciliation(con, card_id)
    ok(rec is not None, "Card reconciliation checkpoint exists")
    ok(rec["statement_balance_cents"] == 5000, f"Statement balance recorded (got {rec['statement_balance_cents']})")
    ok(rec["difference_cents"] == 0, f"Recorded difference is 0 (got {rec['difference_cents']})")
finally:
    con.close()

print("\nRECONCILE ADJUSTMENT TESTS DONE")
