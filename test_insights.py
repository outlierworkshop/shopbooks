"""Tests for the read-only book-query layer (insights.py).

Follows the mandatory isolation pattern: point SHOPBOOKS_DATA_DIR at a temp dir
BEFORE importing db, so tests can never touch real books. Seeds a small known set
of entries and asserts every figure exactly (deterministic, no AI involved).
"""
import os
import tempfile
from datetime import date
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_insightstest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db        # noqa: E402
import ledger    # noqa: E402
import insights  # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)

db.init()
con = db.connect()
acct = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
CHK, SALES, MAT, UNCAT, CARD = (acct["Business Checking"], acct["Sales - Square"],
                                acct["Materials & Supplies"], acct["Uncategorized Expense"],
                                acct["Credit Card 1"])


def income(d, dollars, to=CHK):
    ledger.post_entry(con, d, "income", [(to, dollars * 100), (SALES, -dollars * 100)])


def expense(d, dollars, cat=MAT, paid=CHK):
    ledger.post_entry(con, d, "expense", [(cat, dollars * 100), (paid, -dollars * 100)])


# Prior year (2025): income 800, one $100 materials expense on the credit card.
income("2025-06-15", 800)
expense("2025-06-20", 100, paid=CARD)
# Current year (2026): income 1500; $500 materials; one $50 uncategorized in March.
income("2026-01-10", 1000)
expense("2026-01-15", 300)
income("2026-02-10", 500)
expense("2026-02-15", 200)
expense("2026-03-10", 50, cat=UNCAT)
con.commit()

EOY = date(2026, 12, 31)  # fixed "today" so this-year/last-year are deterministic

# --- parse_period ------------------------------------------------------------
ok(insights.parse_period("2026") == ("2026-01-01", "2026-12-31", "2026"), "parse YYYY")
ok(insights.parse_period("2026-Q1") == ("2026-01-01", "2026-03-31", "2026 Q1"), "parse quarter")
ok(insights.parse_period("2026-02") == ("2026-02-01", "2026-02-28", "2026-02"), "parse month (non-leap Feb)")
ok(insights.parse_period("this-year", today=EOY)[2] == "2026", "this-year resolves to current year")
ok(insights.parse_period("last-month", today=date(2026, 1, 15))[:2] == ("2025-12-01", "2025-12-31"),
   "last-month rolls over the year boundary")
try:
    insights.parse_period("garbage")
    ok(False, "bad period raises")
except ValueError:
    ok(True, "bad period raises")

# --- pnl_summary -------------------------------------------------------------
s = insights.pnl_summary(con, "2026-01-01", "2026-12-31")
ok(s["income_total"] == 150000, "2026 income = $1500")
ok(s["expense_total"] == 55000, "2026 expenses = $550 (materials 500 + uncategorized 50)")
ok(s["net"] == 95000, "2026 net = $950")
ok(s["expense_by_category"][0]["name"] == "Materials & Supplies" and s["expense_by_category"][0]["amount"] == 50000,
   "expenses broken down by category, largest first")

# --- compare (growth) --------------------------------------------------------
c = insights.compare(con, "this-year", "last-year", today=EOY)
ok(c["income"]["delta"] == 70000 and c["income"]["pct_change"] == 87.5, "income growth vs last year")
ok(c["net"]["current"] == 95000 and c["net"]["previous"] == 70000, "net compared across years")

# --- expense_changes (movers vs base period) ---------------------------------
ec = insights.expense_changes(con, "this-year", "last-year", today=EOY)
by = {r["name"]: r for r in ec["rows"]}
# 2026 materials 500 vs 2025 materials 100 -> +400; biggest absolute mover should be first
ok(ec["rows"][0]["name"] == "Materials & Supplies", "biggest mover sorted first")
ok(by["Materials & Supplies"]["delta"] == 40000 and by["Materials & Supplies"]["pct_change"] == 400.0,
   "materials change vs last year computed")
ok(by["Uncategorized Expense"]["previous"] == 0 and by["Uncategorized Expense"]["pct_change"] is None,
   "a category new this year has no prior and pct_change None")

# --- monthly_trend -----------------------------------------------------------
t = insights.monthly_trend(con, "2026-01-01", "2026-12-31")
ok(len(t) == 12, "monthly trend has 12 months")
bym = {m["month"]: m for m in t}
ok(bym["2026-01"]["net"] == 70000, "January net = income 1000 - expense 300")
ok(bym["2026-03"]["net"] == -5000, "March net is negative (only the $50 expense)")
ok(bym["2026-05"]["income"] == 0 and bym["2026-05"]["net"] == 0, "empty months are zero, not missing")

# --- cash_position -----------------------------------------------------------
cash = insights.cash_position(con, as_of="2026-12-31")
ok(cash["cash_on_hand"] == 175000, "checking balance = $1750")
ok(cash["card_debt"] == 10000, "credit card shows $100 owed (liability reads positive)")

# as_of respected: before any 2026 activity, only the 2025 deposit is in checking
early = insights.cash_position(con, as_of="2025-12-31")
ok(early["cash_on_hand"] == 80000, "as_of date limits the balance to transactions up to that date")

# --- bookkeeping_health ------------------------------------------------------
h = insights.bookkeeping_health(con, "2026-01-01", "2026-12-31")
ok(h["uncategorized"] == 1, "one entry still uncategorized in 2026")
ok(h["pending_review"] == 0 and h["unmatched_receipts"] == 0, "nothing pending / unmatched")
ok(h["tidy"] is False and any("Uncategorized" in i for i in h["issues"]),
   "not tidy: the uncategorized entry is reported as an issue")

# --- business_snapshot (the chatbot's one call) ------------------------------
snap = insights.business_snapshot(con, "2026", today=EOY)
ok(snap["period"] == "2026" and snap["pnl"]["net"] == 95000, "snapshot bundles the right P&L")
ok("monthly_trend" in snap and "cash_position" in snap and "health" in snap,
   "snapshot bundles trend, cash, and health together")

con.close()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nINSIGHTS TESTS DONE")
