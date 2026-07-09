"""Tests for bulk select actions on Register (delete, category) and Review (category, post, skip).

All bulk routes delegate to the existing single-entry primitives (ledger.delete_entry,
ledger.update_entry_fields, _post_staged) per selected id, so correctness follows from those —
these tests focus on: only the selected ids are touched, locked/split entries are skipped (not
aborting the batch), and the HTTP surface (checkboxes, redirects) is wired up.
Isolated via SHOPBOOKS_DATA_DIR before importing db.
"""
import os
import tempfile
from pathlib import Path
from urllib.parse import unquote

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_bulktest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db        # noqa: E402
import ledger    # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed

db.init()
con = db.connect()
acct = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
CHK, MAT, UNCAT, SALES = (acct["Business Checking"], acct["Materials & Supplies"],
                          acct["Uncategorized Expense"], acct["Sales - Square"])

e1 = ledger.post_entry(con, "2026-01-05", "Vendor A", [(MAT, 1000), (CHK, -1000)])
e2 = ledger.post_entry(con, "2026-01-06", "Vendor B", [(UNCAT, 2000), (CHK, -2000)])
e3 = ledger.post_entry(con, "2026-01-07", "Vendor C", [(UNCAT, 3000), (CHK, -3000)])
e_split = ledger.post_entry(con, "2026-01-08", "Split txn",
                            [(MAT, 500), (UNCAT, 500), (CHK, -1000)])  # 3 splits -> uncategorizable in bulk
con.commit()

from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402
c = TestClient(app.app)

# --- Register renders the bulk toolbar + one checkbox per row -------------------
page = c.get(f"/register/{CHK}")
ok(page.status_code == 200 and "Delete selected" in page.text and "Apply to selected" in page.text,
   "register page shows the bulk toolbar")
ok(page.text.count('class="sel-reg"') == 4, "one selection checkbox per register row")

# --- bulk category: only selected, 2-split entries update; the 3-split entry is skipped ---------
r = c.post(f"/register/{CHK}/bulk-category",
          data={"entry_ids": [str(e2), str(e3), str(e_split)], "category_id": str(MAT)},
          follow_redirects=False)
ok(r.status_code == 303 and "msg=" in r.headers["location"], "bulk-category redirects with a summary")

def split_cat(entry_id, exclude=CHK):
    row = con.execute(
        "SELECT s.account_id FROM splits s WHERE s.entry_id=? AND s.account_id!=?", (entry_id, exclude)
    ).fetchall()
    return {x["account_id"] for x in row}

ok(split_cat(e2) == {MAT}, "e2's category was bulk-updated to Materials")
ok(split_cat(e3) == {MAT}, "e3's category was bulk-updated to Materials")
ok(split_cat(e1) == {MAT}, "e1 (not selected) is untouched, still Materials from creation")
ok(split_cat(e_split) == {MAT, UNCAT}, "the 3-split entry is left alone (update_entry_fields no-ops on splits)")

# --- bulk category respects the period lock (locked entries skipped, not aborted) ----------------
db.set_setting(con, "books_locked_through", "2026-01-06")
con.commit()
r2 = c.post(f"/register/{CHK}/bulk-category",
           data={"entry_ids": [str(e1), str(e3)], "category_id": str(UNCAT)}, follow_redirects=False)
ok("1 skipped" in unquote(r2.headers["location"]), "a locked-period entry is reported as skipped")
ok(split_cat(e1) == {MAT}, "the locked entry (e1, dated 1/5) was NOT changed")
ok(split_cat(e3) == {UNCAT}, "the unlocked entry (e3, dated 1/7) WAS changed")
db.set_setting(con, "books_locked_through", "")
con.commit()

# --- bulk delete: only selected entries removed; balances/staged revert normally -----------------
before = con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
r3 = c.post(f"/register/{CHK}/bulk-delete", data={"entry_ids": [str(e2), str(e3)]}, follow_redirects=False)
ok(r3.status_code == 303 and "Deleted 2" in unquote(r3.headers["location"]), "bulk-delete redirects with a count")
after = con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
ok(after == before - 2, "exactly the two selected entries were deleted")
ok(con.execute("SELECT 1 FROM entries WHERE id=?", (e1,)).fetchone() is not None,
   "the unselected entry (e1) survives")
ok(con.execute("SELECT 1 FROM entries WHERE id IN (?,?)", (e2, e3)).fetchone() is None,
   "both selected entries are gone")

# --- Review: bulk category / post / skip ----------------------------------------------------------
cur = con.execute("INSERT INTO batches(filename,account_id) VALUES('bulk.csv',?)", (CHK,))
bid = cur.lastrowid
sids = []
for i, (d, amt) in enumerate([("2026-02-01", 100), ("2026-02-02", 200), ("2026-02-03", 300)]):
    cur2 = con.execute("INSERT INTO staged(batch_id,date,description,amount_cents) VALUES(?,?,?,?)",
                       (bid, d, f"Row {i}", amt))
    sids.append(cur2.lastrowid)
con.commit()

page = c.get("/review")
ok(page.status_code == 200 and "sel-review" in page.text and "Post selected" in page.text,
   "review page shows the bulk toolbar and checkboxes")

# bulk category on the first two rows only
r4 = c.post("/review", data={"sel": [str(sids[0]), str(sids[1])], "bulk_category": str(MAT),
                             "set_category_selected": "1"}, follow_redirects=False)
ok(r4.status_code == 303, "set_category_selected redirects")
cats = {s["id"]: s["category_id"] for s in con.execute("SELECT id, category_id FROM staged").fetchall()}
ok(cats[sids[0]] == MAT and cats[sids[1]] == MAT, "bulk category applied to the two selected staged rows")
ok(cats[sids[2]] is None, "the third (unselected) staged row is untouched")

# post only the first row (it now has a category from the bulk-set above)
r5 = c.post("/review", data={"sel": [str(sids[0])], "cat_" + str(sids[0]): str(MAT),
                             "cat_" + str(sids[1]): str(MAT), "post_selected": "1"}, follow_redirects=False)
ok(r5.status_code == 303, "post_selected redirects")
st0 = con.execute("SELECT status FROM staged WHERE id=?", (sids[0],)).fetchone()["status"]
st1 = con.execute("SELECT status FROM staged WHERE id=?", (sids[1],)).fetchone()["status"]
ok(st0 == "posted", "the selected row was posted")
ok(st1 == "pending", "the categorized-but-unselected row was NOT posted")

# skip the remaining pending row
r6 = c.post("/review", data={"sel": [str(sids[2])], "skip_selected": "1"}, follow_redirects=False)
ok(r6.status_code == 303, "skip_selected redirects")
st2 = con.execute("SELECT status FROM staged WHERE id=?", (sids[2],)).fetchone()["status"]
ok(st2 == "skipped", "the selected row was skipped")

# --- bulk-category with no category chosen fails gracefully (not a raw 422) ----------------------
r7 = c.post(f"/register/{CHK}/bulk-category", data={"entry_ids": [str(e1)], "category_id": ""},
           follow_redirects=False)
ok(r7.status_code == 303 and "err=" in r7.headers["location"], "an empty category selection redirects with a friendly error, not a 422")

con.close()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nBULK ACTIONS TESTS DONE")
