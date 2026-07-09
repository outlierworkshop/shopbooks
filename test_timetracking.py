"""Tests for time tracking & job costing (timetracking.py).

Isolation pattern: point SHOPBOOKS_DATA_DIR at a temp dir BEFORE importing db.
Seeds known jobs + time entries and asserts every figure exactly, plus the key
invariant: tracking time must NEVER create ledger entries/splits.
"""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_timetest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db            # noqa: E402
import timetracking as tt  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed

db.init()
con = db.connect()
db.set_setting(con, "default_hourly_rate", "50")   # $50/hr default
con.commit()

ok(tt.default_rate_cents(con) == 5000, "default rate parsed to cents")

job_a = tt.add_job(con, "Uke #1", notes="tenor")
job_b = tt.add_job(con, "Bench repair")
con.commit()

# Entries: (date, hours, job, category, billable, rate$)
tt.add_entry(con, "2026-01-05", 2.0, job_id=job_a, category="carving", billable=True)            # 2*50 = $100
tt.add_entry(con, "2026-01-06", 1.5, job_id=job_a, category="finishing", billable=True, rate_cents=8000)  # 1.5*80 = $120
tt.add_entry(con, "2026-01-07", 3.0, job_id=job_b, category="carving", billable=False)            # $0
tt.add_entry(con, "2026-01-08", 1.0, category="admin", billable=False)                            # no job, $0
tt.add_entry(con, "2026-02-01", 0.5, job_id=job_a, category="admin", billable=True)               # 0.5*50 = $25
con.commit()

# --- summary totals (all 2026) -----------------------------------------------
s = tt.summary(con, "2026-01-01", "2026-12-31")
ok(s["total_hours"] == 8.0, "total hours = 8.0")
ok(s["billable_hours"] == 4.0, "billable hours = 4.0 (non-billable excluded)")
ok(s["billable_value"] == 24500, "billable value = $245.00 (default rate + per-entry override)")

# --- per-entry rate override and non-billable = 0 ----------------------------
bycat = {c["category"]: c for c in s["by_category"]}
ok(bycat["finishing"]["billable_value"] == 12000, "per-entry $80 rate overrode the default")
ok(bycat["carving"]["hours"] == 5.0 and bycat["carving"]["billable_value"] == 10000,
   "carving rolls billable + non-billable hours, values only the billable one")
ok(s["by_category"][0]["category"] == "carving", "categories sorted by hours, busiest first")

# --- per-job rollup ----------------------------------------------------------
byjob = {j["job"]: j for j in s["by_job"]}
ok(byjob["Uke #1"]["hours"] == 4.0 and byjob["Uke #1"]["billable_value"] == 24500,
   "job A rolls up its three entries")
ok(byjob["Bench repair"]["billable_value"] == 0, "non-billable job has zero value")
ok(any(j["job"] == "(no job)" and j["hours"] == 1.0 for j in s["by_job"]),
   "entries without a job group under '(no job)'")

# --- period filter -----------------------------------------------------------
jan = tt.summary(con, "2026-01-01", "2026-01-31")
ok(jan["total_hours"] == 7.5 and jan["billable_value"] == 22000,
   "period filter excludes the February entry")

# --- job_report --------------------------------------------------------------
rep = tt.job_report(con, job_a)
ok(rep["total_hours"] == 4.0 and rep["billable_value"] == 24500 and len(rep["entries"]) == 3,
   "job_report totals one job and lists its entries")
ok(tt.job_report(con, 99999) is None, "job_report returns None for an unknown job")

# --- jobs_overview & categories ----------------------------------------------
ov = {j["name"]: j for j in tt.jobs_overview(con)}
ok(ov["Uke #1"]["hours"] == 4.0 and ov["Uke #1"]["billable_value"] == 24500, "jobs_overview rolls up job A")
ok(ov["Bench repair"]["hours"] == 3.0, "jobs_overview includes a job with only non-billable time")
ok(tt.categories(con) == ["admin", "carving", "finishing"], "distinct categories for autocomplete")

# --- status toggle -----------------------------------------------------------
tt.set_job_status(con, job_b, "done"); con.commit()
ok(con.execute("SELECT status FROM jobs WHERE id=?", (job_b,)).fetchone()["status"] == "done",
   "job status can be marked done")

# --- INVARIANT: time tracking never touches the double-entry ledger ----------
ok(con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0, "no ledger entries created")
ok(con.execute("SELECT COUNT(*) c FROM splits").fetchone()["c"] == 0, "no ledger splits created")

# --- Phase 2: job costing (tag ledger transactions to a job) -----------------
import ledger  # noqa: E402

acct = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
CHK, SALES, MAT = acct["Business Checking"], acct["Sales - Square"], acct["Materials & Supplies"]

# $400 sale tagged to job A at post time; $150 materials tagged retroactively.
ledger.post_entry(con, "2026-01-20", "Customer pmt", [(CHK, 40000), (SALES, -40000)], job_id=job_a)
mat_entry = ledger.post_entry(con, "2026-01-12", "Lumber", [(MAT, 15000), (CHK, -15000)])
ledger.set_entry_job(con, mat_entry, job_a)
# An untagged expense must NOT affect any job.
ledger.post_entry(con, "2026-01-13", "Untagged glue", [(MAT, 2000), (CHK, -2000)])
con.commit()

fin = tt.job_financials(con, job_a)
ok(fin["income"] == 40000, "job income = $400 (tagged sale)")
ok(fin["expenses"] == 15000, "job expenses = $150 (retroactively-tagged materials only)")
ok(fin["net_cash"] == 25000, "job net cash profit = $250 (untagged glue excluded)")

rep = tt.job_report(con, job_a)
ok(rep["financials"]["net_cash"] == 25000, "job_report carries financials")
# job A has 4.0 logged hours; $250 / 4h = $62.50/hr
ok(rep["effective_hourly"] == 6250, "effective profit/hour = net cash / hours")
ok(len(rep["transactions"]) == 2 and rep["transactions"][0]["pnl"] in (40000, -15000),
   "job_report lists the two tagged transactions with profit impact")

ov = {j["name"]: j for j in tt.jobs_overview(con)}
ok(ov["Uke #1"]["net_cash"] == 25000, "jobs_overview shows net cash profit")
ok(ov["Bench repair"]["net_cash"] == 0, "untagged job shows zero profit")

# retag: move the materials entry off the job -> profit rises to pure income
ledger.set_entry_job(con, mat_entry, None)
con.commit()
ok(tt.job_financials(con, job_a)["net_cash"] == 40000, "untagging a transaction updates job profit")

# --- ledger invariant still holds after all this posting ---------------------
bad = con.execute("SELECT entry_id FROM splits GROUP BY entry_id HAVING SUM(amount_cents)!=0").fetchall()
ok(not bad, "every journal entry still balances to zero (splits sum to 0)")

con.close()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nTIME TRACKING TESTS DONE")
