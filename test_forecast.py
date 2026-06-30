"""Tests for the cash-flow forecast (insights.cash_forecast). Deterministic, isolated.

Combines: starting bank cash, expected invoice collections (by due month), recurring income/expense
occurrences, and a trailing-average burn for everything non-recurring — projecting the month-end
balance and flagging the low point / any dip below zero.
"""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_fcasttest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db        # noqa: E402
import ledger    # noqa: E402
import insights  # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)
TODAY = "2026-06-15"

db.init()
con = db.connect()
acct = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
CHK, SALES, MAT = acct["Business Checking"], acct["Sales - Square"], acct["Materials & Supplies"]

# Starting cash: $5,000 in, $3,000 expense already spent -> $2,000 on hand.
ledger.post_entry(con, "2026-01-10", "seed deposit", [(CHK, 500000), (SALES, -500000)])
ledger.post_entry(con, "2026-03-01", "materials", [(MAT, 300000), (CHK, -300000)])   # historical burn
# One open invoice, $1,500, due in July (lands in the July bucket).
cust = con.execute("INSERT INTO customers(name) VALUES('Acme')").lastrowid
inv = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,kind) "
                  "VALUES('INV-1',?,'2026-07-01','2026-07-31','sent','invoice')", (cust,)).lastrowid
con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,150000)", (inv, "job"))
# Recurring: $200/mo rent (expense) from Jul 1; $300/mo retainer (income) from Jul 5.
con.execute("INSERT INTO recurring(name,amount_cents,flow,account_id,category_id,frequency,next_date) "
            "VALUES('Rent',20000,'expense',?,?,'monthly','2026-07-01')", (CHK, MAT))
con.execute("INSERT INTO recurring(name,amount_cents,flow,account_id,category_id,frequency,next_date) "
            "VALUES('Retainer',30000,'income',?,?,'monthly','2026-07-05')", (CHK, SALES))
con.commit()

f = insights.cash_forecast(con, horizon_days=90, today=TODAY)

# --- base figures -------------------------------------------------------------
ok(f["starting_cash"] == 200000, "starting cash = $5000 in - $3000 spent = $2,000")
ok(f["avg_monthly_expense"] == 50000, "trailing burn = $3000 / 6 months = $500/mo")
ok(f["recurring_monthly_expense"] == 20000, "recurring expense normalizes to $200/mo")
ok(f["variable_burn"] == 30000, "variable burn = $500 - $200 recurring = $300/mo (no double-count)")
ok(f["expected_inflow_total"] == 150000, "expected invoice collections over horizon = $1,500")
ok(f["recurring_income_total"] == 90000, "recurring income = 3 x $300 (Jul/Aug/Sep) = $900")
ok(f["recurring_expense_total"] == 60000, "recurring expense occurrences = 3 x $200 = $600")

# --- monthly projection -------------------------------------------------------
bym = {m["month"]: m for m in f["months"]}
ok(set(bym) == {"2026-06", "2026-07", "2026-08", "2026-09"}, "horizon spans Jun–Sep")
ok(bym["2026-06"]["end_balance"] == 170000, "Jun: 2000 - 300 burn = $1,700")
ok(bym["2026-07"]["inflow"] == 180000 and bym["2026-07"]["outflow"] == 50000,
   "Jul: in = $1500 invoice + $300 retainer; out = $300 burn + $200 rent")
ok(bym["2026-07"]["end_balance"] == 300000, "Jul: 1700 + 1800 - 500 = $3,000")
ok(bym["2026-09"]["end_balance"] == 260000, "Sep end balance = $2,600")
ok(f["projected_end"] == 260000, "projected end matches the last month")
ok(f["low_point"]["balance"] == 170000 and f["low_point"]["label"] == "Jun 2026", "low point is June at $1,700")
ok(f["goes_negative"] is False, "this scenario never dips below zero")

# --- a big recurring bill pushes it negative ----------------------------------
con.execute("INSERT INTO recurring(name,amount_cents,flow,account_id,category_id,frequency,next_date) "
            "VALUES('Huge',500000,'expense',?,?,'monthly','2026-07-01')", (CHK, MAT))
con.commit()
f2 = insights.cash_forecast(con, horizon_days=90, today=TODAY)
ok(f2["goes_negative"] is True, "a $5,000/mo bill drives the projection below zero")
ok(f2["low_point"]["balance"] < 0, "the low point is now negative")

con.close()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nFORECAST TESTS DONE")
