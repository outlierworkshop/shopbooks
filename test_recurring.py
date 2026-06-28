"""Tests for recurring transactions (recurring.py). Isolated via SHOPBOOKS_DATA_DIR.

Nothing posts automatically: post_occurrence posts ONE occurrence (human-confirmed) and advances
the schedule; skip advances without posting; upcoming() projects future occurrences for the forecast.
"""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_rectest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db          # noqa: E402
import ledger      # noqa: E402
import recurring   # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)

# --- advance(): frequency math + month/year clamping --------------------------
ok(recurring.advance("2026-01-15", "weekly") == "2026-01-22", "weekly = +7 days")
ok(recurring.advance("2026-01-31", "monthly") == "2026-02-28", "monthly clamps Jan 31 -> Feb 28")
ok(recurring.advance("2026-02-28", "monthly") == "2026-03-28", "monthly keeps the day where it fits")
ok(recurring.advance("2026-01-15", "monthly", n=3) == "2026-04-15", "monthly n=3 steps three months")
ok(recurring.advance("2028-02-29", "yearly") == "2029-02-28", "yearly clamps a leap day")
ok(recurring.advance("2026-01-15", "yearly") == "2027-01-15", "yearly = +1 year")

db.init()
con = db.connect()
acct = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
CHK, RENT_CAT, SALES = acct["Business Checking"], acct["Materials & Supplies"], acct["Sales - Square"]


def mk(name, cents, cat, acc=CHK, flow="expense", freq="monthly", nd="2026-03-01"):
    con.execute("INSERT INTO recurring(name,amount_cents,flow,account_id,category_id,frequency,next_date) "
                "VALUES(?,?,?,?,?,?,?)", (name, cents, flow, acc, cat, freq, nd))
    return con.execute("SELECT id FROM recurring WHERE name=?", (name,)).fetchone()["id"]


rent = mk("Shop rent", 120000, RENT_CAT, nd="2026-03-01")     # $1,200/mo expense
sub = mk("Subscription", 5000, RENT_CAT, nd="2026-04-10")     # $50/mo, not yet due at 3/15
retainer = mk("Retainer", 80000, RENT_CAT, acc=CHK, flow="income", nd="2026-03-05")  # $800/mo income
con.commit()

TODAY = "2026-03-15"

# --- due detection ------------------------------------------------------------
due_ids = {r["id"] for r in recurring.due(con, TODAY)}
ok(rent in due_ids and retainer in due_ids and sub not in due_ids,
   "due() returns only active items whose next_date has arrived (subscription due 4/10 is not yet)")
allrows = {r["id"]: r for r in recurring.list_all(con, TODAY)}
ok(allrows[rent]["due"] and allrows[rent]["days_overdue"] == 14, "list_all flags due + days overdue (3/15 - 3/01)")
ok(not allrows[sub]["due"], "a future item is not marked due")

# --- post_occurrence: posts the right splits + advances -----------------------
eid = recurring.post_occurrence(con, rent)
con.commit()
splits = {r["account_id"]: r["amount_cents"] for r in
          con.execute("SELECT account_id, amount_cents FROM splits WHERE entry_id=?", (eid,)).fetchall()}
ok(splits.get(RENT_CAT) == 120000 and splits.get(CHK) == -120000, "expense posts category +$1200, bank -$1200")
r = con.execute("SELECT next_date, last_posted_date FROM recurring WHERE id=?", (rent,)).fetchone()
ok(r["next_date"] == "2026-04-01" and r["last_posted_date"] == "2026-03-01",
   "posting advances next_date one month and records last_posted_date")
ok(rent not in {x["id"] for x in recurring.due(con, TODAY)}, "after posting, the item is no longer due")

# --- income flow posts the other direction ------------------------------------
eid2 = recurring.post_occurrence(con, retainer)
con.commit()
s2 = {r["account_id"]: r["amount_cents"] for r in
      con.execute("SELECT account_id, amount_cents FROM splits WHERE entry_id=?", (eid2,)).fetchall()}
ok(s2.get(CHK) == 80000 and s2.get(RENT_CAT) == -80000, "income posts bank +$800, category -$800")

# --- skip advances without posting --------------------------------------------
before = con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
recurring.skip_occurrence(con, sub)
con.commit()
after = con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
ok(after == before and con.execute("SELECT next_date FROM recurring WHERE id=?", (sub,)).fetchone()[0] == "2026-05-10",
   "skip advances the date (4/10 -> 5/10) and posts nothing")

# --- period lock is respected -------------------------------------------------
db.set_setting(con, "books_locked_through", "2026-12-31")
con.commit()
locked = False
try:
    recurring.post_occurrence(con, rent)  # rent next_date is now 2026-04-01, inside the lock
except ledger.LockedPeriodError:
    locked = True
ok(locked, "posting a recurring occurrence into a closed period is blocked by the ledger guard")
db.set_setting(con, "books_locked_through", "")
con.commit()

# --- upcoming(): projection for the forecast ----------------------------------
up = recurring.upcoming(con, "2026-04-01", "2026-06-30")
rent_occ = [u for u in up if u["recurring_id"] == rent]
ok([u["date"] for u in rent_occ] == ["2026-04-01", "2026-05-01", "2026-06-01"], "monthly item projects 3 occurrences in Q2")
ok(all(u["amount"] == -120000 for u in rent_occ), "expense occurrences are signed negative")
ok(any(u["recurring_id"] == retainer and u["amount"] == 80000 for u in up), "income occurrences are signed positive")

con.close()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nRECURRING TESTS DONE")
