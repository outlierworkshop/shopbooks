"""Shop-log time import (shoplog.py): filename revs, replace-per-day, customer matching, and the
watcher wiring. Isolation: SHOPBOOKS_DATA_DIR -> temp dir BEFORE importing db."""
import os
import tempfile
from pathlib import Path

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_shoplog_")

import db        # noqa: E402
import shoplog   # noqa: E402
import watcher   # noqa: E402
from testutil import ok  # noqa: E402

db.init()
con = db.connect()

for name in ("JP Moriarty", "Nick Lloyd Basses LLC", "Ryan L. Soltis", "Andrew Ryan", "Brian Lee"):
    con.execute("INSERT INTO customers(name) VALUES(?)", (name,))
con.commit()

# --- customer matching -----------------------------------------------------------
def cust_id(name):
    return con.execute("SELECT id FROM customers WHERE name=?", (name,)).fetchone()["id"]

ok(shoplog.match_customer(con, "Moriarty") == cust_id("JP Moriarty"),
   "a one-word client finds its unique customer")
ok(shoplog.match_customer(con, "NickLloyd") == cust_id("Nick Lloyd Basses LLC"),
   "CamelCase splits into words that all must match")
ok(shoplog.match_customer(con, "AndrewRyan") == cust_id("Andrew Ryan"),
   "'AndrewRyan' matches Andrew Ryan, not Ryan L. Soltis (ALL words required)")
ok(shoplog.match_customer(con, "Ryan") is None,
   "an ambiguous single word (two Ryans) refuses to guess")
ok(shoplog.match_customer(con, "Internal") is None, "'Internal' is the shop, never a customer")
ok(shoplog.match_customer(con, "Prismatone") is None, "an unknown client matches nobody")

# --- a day file imports ----------------------------------------------------------
# Real shop-log shape: header + comma-free notes, blank friction, an unbillable ADMIN row
# (the hours that never reach an invoice), and an RND row that IS billable (paid job time).
DAY1 = (b"date,start,end,minutes,client,job,work_type,billable,notes,friction\n"
        b"2026-08-04,08:00,08:05,5,Internal,shop,ADMIN,n,Square hold escalation filed,3\n"
        b"2026-08-04,08:10,12:10,240,Moriarty,northwoods-arches,SETUP,y,template setup - ran clean,1\n"
        b"2026-08-04,13:00,16:00,180,Moriarty,northwoods-arches,RND,y,est Fusion workflow inside the paid block,\n")
s, note = shoplog.ingest_csv(con, Path("2026-08-04.csv"), DAY1)
ok(s == "imported" and "3 time entries" in note, f"a day file imports every row ({note})")

rows = con.execute("SELECT * FROM time_entries WHERE date='2026-08-04' ORDER BY id").fetchall()
ok(len(rows) == 3, "three entries landed")
ok(rows[0]["hours"] == round(5 / 60, 4) and rows[1]["hours"] == 4.0,
   "minutes convert to hours")
ok(rows[0]["billable"] == 0 and rows[1]["billable"] == 1, "y/n maps to the billable flag")
ok(rows[0]["category"] == "ADMIN" and rows[2]["category"] == "RND",
   "work_type codes become the category")
ok(rows[2]["note"].startswith("est "), "the est marker on an unconfirmed row survives")
ok(all(r["source"] == "shoplog:2026-08-04:rev1" for r in rows), "rows carry their provenance")
ok(all(r["rate_cents"] is None for r in rows), "no per-entry rate is invented")

job = con.execute("SELECT j.*, c.name cname FROM jobs j LEFT JOIN customers c ON c.id=j.customer_id "
                  "WHERE j.name='northwoods-arches'").fetchone()
ok(job is not None, "the job was auto-created from the slug")
ok(job["cname"] == "JP Moriarty", "...and linked to the matched customer")
shop_job = con.execute("SELECT customer_id FROM jobs WHERE name='shop'").fetchone()
ok(shop_job is not None and shop_job["customer_id"] is None, "the Internal 'shop' job has no customer")
ok(rows[1]["job_id"] == job["id"] and rows[2]["job_id"] == job["id"],
   "both Moriarty rows share one job (no duplicates created)")

# re-import of the SAME file (watcher re-reads on mtime change) is idempotent
s, note = shoplog.ingest_csv(con, Path("2026-08-04.csv"), DAY1)
ok(s == "imported" and "re-imported" in note, f"same rev re-imports in place ({note})")
ok(con.execute("SELECT COUNT(*) c FROM time_entries WHERE date='2026-08-04'").fetchone()["c"] == 3,
   "...without duplicating the day")
ok(con.execute("SELECT COUNT(*) c FROM jobs WHERE lower(name)='northwoods-arches'").fetchone()["c"] == 1,
   "...or the job")

# --- rev supersession ------------------------------------------------------------
# a manual entry on the same day must survive every replace
import timetracking  # noqa: E402
timetracking.add_entry(con, "2026-08-04", 1.0, category="HAND", note="typed by hand")

REV2 = (b"date,start,end,minutes,client,job,work_type,billable,notes,friction\n"
        b"2026-08-04,08:00,12:00,240,Moriarty,northwoods-arches,SETUP,y,corrected - one block,1\n")
s, note = shoplog.ingest_csv(con, Path("2026-08-04-rev2.csv"), REV2)
ok(s == "imported" and "rev2 replaced rev1" in note, f"a rev2 file replaces the day ({note})")
imported = con.execute("SELECT * FROM time_entries WHERE date='2026-08-04' AND source != '' ").fetchall()
ok(len(imported) == 1 and imported[0]["note"] == "corrected - one block",
   "the day's imported rows are the rev2 rows only")
manual = con.execute("SELECT COUNT(*) c FROM time_entries WHERE date='2026-08-04' AND source=''").fetchone()
ok(manual["c"] == 1, "the manually typed entry survived the replace")

# the superseded plain file turning up again (alphabetical scan order) must NOT undo rev2
s, note = shoplog.ingest_csv(con, Path("2026-08-04.csv"), DAY1)
ok(s == "duplicate" and "superseded" in note, f"an older rev is skipped ({note})")
ok(con.execute("SELECT COUNT(*) c FROM time_entries WHERE date='2026-08-04' AND source != ''").fetchone()["c"] == 1,
   "...and the rev2 import is untouched")

# --- malformed input -------------------------------------------------------------
s, _ = shoplog.ingest_csv(con, Path("notes.csv"), DAY1)
ok(s == "error", "a non-day filename is rejected")
s, _ = shoplog.ingest_csv(con, Path("2026-08-05.csv"), b"date,hours\n2026-08-05,2\n")
ok(s == "error", "a file missing the shop-log columns is rejected")
MIXED = (b"date,start,end,minutes,client,job,work_type,billable,notes,friction\n"
         b"2026-08-05,08:00,09:00,60,Internal,shop,MAINT,n,cleanup,\n"
         b"2026-08-06,09:00,10:00,60,Internal,shop,MAINT,n,wrong day for this file,\n"
         b"2026-08-05,10:00,10:30,,Internal,shop,MAINT,n,blank minutes,\n")
s, note = shoplog.ingest_csv(con, Path("2026-08-05.csv"), MIXED)
ok(s == "imported" and "1 time entry" in note and "2 row(s) skipped" in note,
   f"wrong-day and unusable rows are skipped, not imported ({note})")
s, _ = shoplog.ingest_csv(con, Path("2026-08-07.csv"),
                          b"date,start,end,minutes,client,job,work_type,billable,notes,friction\n")
ok(s == "error", "a header-only file reports an error (so the watcher retries it)")

# a blank job slug (client with no job decided yet) imports with no job link
BLANK_JOB = (b"date,start,end,minutes,client,job,work_type,billable,notes,friction\n"
             b"2026-08-09,10:00,10:45,45,Buscarino,,COMMS,n,first-contact call,\n")
s, _ = shoplog.ingest_csv(con, Path("2026-08-09.csv"), BLANK_JOB)
r = con.execute("SELECT job_id FROM time_entries WHERE date='2026-08-09'").fetchone()
ok(s == "imported" and r["job_id"] is None, "a blank job slug imports without a job")

con.commit()

# --- watcher wiring --------------------------------------------------------------
inbox = Path(tempfile.mkdtemp(prefix="shoplog_inbox_"))
(inbox / "2026-08-10.csv").write_bytes(
    b"date,start,end,minutes,client,job,work_type,billable,notes,friction\n"
    b"2026-08-10,08:00,09:00,60,NickLloyd,lloyd-bass,MODEL,y,bass modeling,\n")
(inbox / "notes.md").write_bytes(b"not a csv")   # narrative files are never picked up
db.set_setting(con, "time_watch_folder", str(inbox))
con.commit()
r = watcher.run_once(con, lambda *a: ("skipped", ""), lambda *a: ("skipped", ""),
                     lambda *a: ("skipped", ""), shoplog.ingest_csv)
ok(r["time"]["enabled"] and r["time"]["scanned"] == 1 and r["time"]["counts"].get("imported") == 1,
   "the watcher scans the shop-log folder (csv only)")
ok(con.execute("SELECT COUNT(*) c FROM time_entries WHERE source='shoplog:2026-08-10:rev1'").fetchone()["c"] == 1,
   "the watched file's rows landed")
job = con.execute("SELECT j.name, c.name cname FROM jobs j LEFT JOIN customers c ON c.id=j.customer_id "
                  "WHERE j.name='lloyd-bass'").fetchone()
ok(job and job["cname"] == "Nick Lloyd Basses LLC", "the watched import linked lloyd-bass to Nick Lloyd")
r2 = watcher.run_once(con, lambda *a: ("skipped", ""), lambda *a: ("skipped", ""))
ok("time" not in r2, "run_once without time_fn keeps the old shape (back-compat)")
con.commit()

# --- the whole day shows up in the time summary ------------------------------------
# rev2 Moriarty block 4.0 + manual 1.0 + Aug-5 1.0 + Buscarino 0.75 + lloyd-bass 1.0
s = timetracking.summary(con, "2026-08-01", "2026-08-31")
ok(s["total_hours"] == 7.75, f"imported hours flow into the time reports ({s['total_hours']:.2f} h)")

con.close()
print("\nSHOP LOG IMPORT TESTS DONE")
