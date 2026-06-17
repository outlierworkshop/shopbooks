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

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)

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

con.close()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nTIME TRACKING TESTS DONE")
