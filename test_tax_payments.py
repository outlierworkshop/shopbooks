"""Tests for estimated-tax payment tracking (#40): tax_payments table, paid/remaining in
insights.estimated_taxes, the briefing reminder using what's still due, and the Taxes-page routes.
Isolated via SHOPBOOKS_DATA_DIR before importing db. Deterministic — no AI, no network.
"""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_taxpaytest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db        # noqa: E402
import ledger    # noqa: E402
import insights  # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)

db.init()
con = db.connect()
acct = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
CHK, SALES = acct["Business Checking"], acct["Sales - Square"]

# Q1 2026 profit: $10,000 -> SE tax = 10000*0.9235*0.153 = $1,412.96; income tax @15% = $1,500
ledger.post_entry(con, "2026-02-01", "big job", [(CHK, 1000000), (SALES, -1000000)])
con.commit()

# --- baseline: due computed, nothing paid --------------------------------------
est = insights.estimated_taxes(con, 2026, 15.0)
q1 = next(q for q in est["quarters"] if q["quarter"] == "Q1")
ok(q1["total_due"] == 291296, "Q1 due = $2,912.96 (SE + income tax)")
ok(q1["paid"] == 0 and q1["remaining"] == 291296, "nothing paid -> remaining = full amount")
ok(est["total_paid"] == 0 and est["total_remaining"] == est["total_due"], "year totals start unpaid")

# --- record a partial payment, then the rest -----------------------------------
con.execute("INSERT INTO tax_payments(year,quarter,date,amount_cents,memo) "
            "VALUES(2026,'Q1','2026-04-10',200000,'IRS Direct Pay')")
con.commit()
q1 = next(q for q in insights.estimated_taxes(con, 2026, 15.0)["quarters"] if q["quarter"] == "Q1")
ok(q1["paid"] == 200000 and q1["remaining"] == 91296, "partial payment: $2,000 paid, $912.96 remaining")

con.execute("INSERT INTO tax_payments(year,quarter,date,amount_cents) VALUES(2026,'Q1','2026-04-14',100000)")
con.commit()
est = insights.estimated_taxes(con, 2026, 15.0)
q1 = next(q for q in est["quarters"] if q["quarter"] == "Q1")
ok(q1["paid"] == 300000 and q1["remaining"] == -8704, "second payment: overpaid by $87.04 (negative remaining)")
ok(est["total_paid"] == 300000 and est["total_remaining"] == est["total_due"] - 300000,
   "year totals reflect all recorded payments")

# --- payments belong to the TAX year, not the calendar year of payment ----------
con.execute("INSERT INTO tax_payments(year,quarter,date,amount_cents) VALUES(2025,'Q4','2026-01-14',50000)")
con.commit()
est26 = insights.estimated_taxes(con, 2026, 15.0)
ok(est26["total_paid"] == 300000, "a 2025-Q4 payment made in Jan 2026 does NOT count toward 2026")

# --- briefing: reminds on what's STILL due, skips paid quarters ------------------
# Give Q2 some profit so it owes something: $5,000 in April -> due $1,456.48.
ledger.post_entry(con, "2026-04-20", "spring job", [(CHK, 500000), (SALES, -500000)])
con.commit()
# On 2026-04-01, Q1 (due 4/15) is overpaid -> the next unpaid quarter (Q2, due 6/15) is the reminder,
# and at 75 days out it's beyond the 45-day window, so no attention item at all.
b = insights.briefing(con, "2026-04-01")
ok(b["next_tax"] and b["next_tax"]["quarter"] == "Q2",
   "an overpaid Q1 is skipped; the next reminder is Q2")
ok(not any("Estimated tax" in a["text"] for a in b["attention"]),
   "Q2 at 75 days out is not yet in the attention list")
# On 2026-06-10 (5 days before Q2's due date) the item appears, escalated to a warning.
b2 = insights.briefing(con, "2026-06-10")
tax_items = [a for a in b2["attention"] if "Estimated tax" in a["text"]]
ok(len(tax_items) == 1 and tax_items[0]["level"] == "warn",
   "within 7 days of the due date the reminder escalates to a warning")
ok("Q2" in tax_items[0]["text"] and "still due" in tax_items[0]["text"],
   "the reminder names the quarter and the amount still due")
ok(b2["next_tax"]["amount"] == 145648 and b2["next_tax"]["paid"] == 0,
   "next_tax carries the remaining amount ($1,456.48) + paid for the dashboard")

# --- January edge: LAST year's Q4 (due Jan 15) is caught -------------------------
con.execute("DELETE FROM tax_payments WHERE year=2025")
con.commit()
# Give 2025 Q4 some profit so it has an amount due
ledger.post_entry(con, "2025-10-05", "fall job", [(CHK, 500000), (SALES, -500000)])
con.commit()
bj = insights.briefing(con, "2026-01-05")
ok(bj["next_tax"] and bj["next_tax"]["quarter"] == "Q4" and bj["next_tax"]["due_date"] == "2026-01-15",
   "in early January, last year's Q4 (due Jan 15) is the next reminder")

con.close()

# --- HTTP: record + delete via the Taxes page ------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402
c = TestClient(app.app)
page = c.get("/taxes?year=2026")
ok(page.status_code == 200 and "Estimated Payments Made (2026)" in page.text, "taxes page shows the payments section")
ok("Remaining" in page.text and "overpaid" in page.text, "the quarterly table shows Paid/Remaining (incl. the overpay)")

r = c.post("/taxes/payment", data={"year": 2026, "quarter": "Q2", "date": "2026-06-12",
                                   "amount": "1,456.48", "memo": "EFTPS"}, follow_redirects=False)
ok(r.status_code == 303 and "Recorded" in r.headers["location"], "recording a payment redirects with a note")
con = db.connect()
row = con.execute("SELECT * FROM tax_payments WHERE year=2026 AND quarter='Q2'").fetchone()
ok(row and row["amount_cents"] == 145648, "the payment row landed ($1,456.48, comma parsed)")
pid = row["id"]
con.close()

bad = c.post("/taxes/payment", data={"year": 2026, "quarter": "Q9", "date": "2026-06-12",
                                     "amount": "5"}, follow_redirects=False)
ok(bad.status_code == 303 and "err=" in bad.headers["location"], "a bogus quarter is rejected")

r2 = c.post(f"/taxes/payment/{pid}/delete", data={"year": 2026}, follow_redirects=False)
ok(r2.status_code == 303, "deleting a recorded payment redirects")
con = db.connect()
ok(con.execute("SELECT COUNT(*) c FROM tax_payments WHERE id=?", (pid,)).fetchone()["c"] == 0,
   "the payment row is gone after delete")
con.close()

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nTAX PAYMENT TESTS DONE")
