"""Tests for reconciliation Phase 2 — per-transaction clearing (reconcile.cleared_balance /
unreconciled_transactions / finish). Isolated via SHOPBOOKS_DATA_DIR before importing db.

Clearing carries forward via splits.reconciled_id (not dates): cleared items become the next
statement's beginning balance and drop off the checklist. All figures are deterministic.
"""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_clrtest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db        # noqa: E402
import ledger    # noqa: E402
import reconcile  # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)

db.init()
con = db.connect()
acct = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
CHK, SALES, MAT, CARD = (acct["Business Checking"], acct["Sales - Square"],
                         acct["Materials & Supplies"], acct["Credit Card 1"])

# Checking: +1000 (Jan 10), -300 (Jan 15), +500 (Feb 10). Statement #1 closes Jan 31 at $700.
ledger.post_entry(con, "2026-01-10", "deposit", [(CHK, 100000), (SALES, -100000)])
ledger.post_entry(con, "2026-01-15", "supplies", [(MAT, 30000), (CHK, -30000)])
ledger.post_entry(con, "2026-02-10", "deposit", [(CHK, 50000), (SALES, -50000)])
con.commit()

# --- starting state -----------------------------------------------------------
ok(reconcile.cleared_balance(con, CHK) == 0, "nothing cleared yet -> beginning balance is 0")
un = reconcile.unreconciled_transactions(con, CHK, "2026-01-31")
ok(len(un) == 2, "only the two January legs are uncleared up to the statement date (Feb excluded)")
ok({u["amount"] for u in un} == {100000, -30000}, "checklist shows display-signed amounts")
ok(all("split_id" in u for u in un), "each checklist row carries the split_id to clear")

# --- clear the two January items against the $700 statement -------------------
jan_ids = [u["split_id"] for u in un]
r = reconcile.finish(con, CHK, "2026-01-31", 70000, jan_ids)
con.commit()
ok(r["reconciled"] and r["difference"] == 0, "clearing both Jan items reconciles to the $700 statement")
ok(r["cleared_count"] == 2, "two transactions reported cleared")

# --- carry-forward: cleared items become the beginning balance and drop off ---
ok(reconcile.cleared_balance(con, CHK) == 70000, "beginning balance now $700 (the cleared total)")
un2 = reconcile.unreconciled_transactions(con, CHK, "2026-02-28")
ok(len(un2) == 1 and un2[0]["amount"] == 50000, "only the uncleared Feb deposit remains on the checklist")
ok(con.execute("SELECT COUNT(*) c FROM splits WHERE account_id=? AND reconciled_id IS NOT NULL",
               (CHK,)).fetchone()["c"] == 2, "exactly the two cleared legs are stamped reconciled")

# --- statement #2: clear the Feb deposit -> $1200 ----------------------------
r2 = reconcile.finish(con, CHK, "2026-02-28", 120000, [un2[0]["split_id"]])
con.commit()
ok(r2["reconciled"] and reconcile.cleared_balance(con, CHK) == 120000,
   "second statement reconciles; beginning carries to $1200")

# --- an out-of-balance finish: wrong statement total leaves a difference ------
ledger.post_entry(con, "2026-03-05", "deposit", [(CHK, 10000), (SALES, -10000)])
con.commit()
un3 = reconcile.unreconciled_transactions(con, CHK, "2026-03-31")
r3 = reconcile.finish(con, CHK, "2026-03-31", 999999, [un3[0]["split_id"]])  # deliberately wrong target
con.commit()
ok(not r3["reconciled"] and r3["difference"] != 0, "a wrong statement total reports a nonzero difference")
ok(reconcile.last_reconciliation(con, CHK)["difference_cents"] == r3["difference"],
   "the out-of-balance checkpoint is recorded with its difference")

# --- card (liability) reads display-signed correctly --------------------------
ledger.post_entry(con, "2026-01-20", "tool", [(MAT, 8000), (CARD, -8000)])  # owe $80 on the card
con.commit()
cun = reconcile.unreconciled_transactions(con, CARD, "2026-01-31")
ok(len(cun) == 1 and cun[0]["amount"] == 8000, "card charge reads +$80 owed (display-signed)")
rc = reconcile.finish(con, CARD, "2026-01-31", 8000, [cun[0]["split_id"]])
con.commit()
ok(rc["reconciled"] and reconcile.cleared_balance(con, CARD) == 8000, "card reconciles to $80 owed")

con.close()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nRECONCILE CLEARING TESTS DONE")
