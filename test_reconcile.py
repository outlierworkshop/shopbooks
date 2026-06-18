"""Tests for reconciliation (reconcile.py).

Isolation pattern: SHOPBOOKS_DATA_DIR -> temp dir BEFORE importing db. Seeds known
transactions and asserts the balance comparison, as-of behavior, card sign, duplicate
detection, and saved-checkpoint status exactly (all deterministic).
"""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_recontest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db         # noqa: E402
import ledger     # noqa: E402
import reconcile  # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)

db.init()
con = db.connect()
acct = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
CHK, SALES, MAT, CARD = (acct["Business Checking"], acct["Sales - Square"],
                         acct["Materials & Supplies"], acct["Credit Card 1"])

# Checking: +1000 (Jan 10), -300 (Jan 15), +500 (Feb 5)  -> $700 as of Jan 31, $1200 as of Feb 28
ledger.post_entry(con, "2026-01-10", "Deposit", [(CHK, 100000), (SALES, -100000)])
ledger.post_entry(con, "2026-01-15", "Lumber", [(MAT, 30000), (CHK, -30000)])
ledger.post_entry(con, "2026-02-05", "Deposit", [(CHK, 50000), (SALES, -50000)])
# Card: $200 charge -> owe $200 (liability reads positive)
ledger.post_entry(con, "2026-01-20", "Tools", [(MAT, 20000), (CARD, -20000)])
con.commit()

# --- balance comparison ------------------------------------------------------
r = reconcile.compute(con, CHK, "2026-01-31", 70000)
ok(r["book_balance"] == 70000, "book balance as of statement date = $700")
ok(r["difference"] == 0 and r["reconciled"], "matching statement balance reconciles")

off = reconcile.compute(con, CHK, "2026-01-31", 75000)
ok(off["difference"] == 5000 and not off["reconciled"], "mismatch reports the $50 difference")

# --- as-of: later statement date includes more activity ----------------------
feb = reconcile.compute(con, CHK, "2026-02-28", 120000)
ok(feb["book_balance"] == 120000 and feb["reconciled"], "as-of date includes the Feb deposit")

# --- credit card reads as amount owed (display-signed) -----------------------
card = reconcile.compute(con, CARD, "2026-01-31", 20000)
ok(card["book_balance"] == 20000 and card["reconciled"], "card statement (amount owed) reconciles")

# --- record a checkpoint and read status -------------------------------------
reconcile.record(con, CHK, "2026-01-31", 70000)
con.commit()
st = {a["name"]: a for a in reconcile.status(con)}
ok(st["Business Checking"]["reconciled"], "status shows checking reconciled after recording")
ok(st["Business Checking"]["last_date"] == "2026-01-31", "status carries the last statement date")
ok(st["Business Checking"]["activity_since"] == 1, "activity_since counts the Feb deposit after the checkpoint")
ok(st["Credit Card 1"]["never_reconciled"], "an un-reconciled account is flagged")
ok(len(reconcile.history(con, CHK)) == 1, "history records the checkpoint")

# --- duplicate detection -----------------------------------------------------
ledger.post_entry(con, "2026-03-01", "Glue", [(MAT, 4500), (CHK, -4500)])
ledger.post_entry(con, "2026-03-03", "Glue again?", [(MAT, 4500), (CHK, -4500)])
con.commit()
dups = reconcile.likely_duplicates(con, CHK, "2026-02-28", "2026-03-31")
ok(len(dups) == 1, "two same-amount transactions within a few days are flagged as a likely duplicate")
ok(dups[0][0]["amount"] == dups[0][1]["amount"] == -4500, "flagged pair shares the amount (money out)")

# a single transaction with a unique amount is not flagged
solo = reconcile.likely_duplicates(con, CHK, "2026-01-31", "2026-02-28")
ok(solo == [], "a lone transaction is not a duplicate")

con.close()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nRECONCILE TESTS DONE")
