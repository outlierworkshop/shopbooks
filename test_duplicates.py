"""Tests for posted-entry duplicate detection (duplicates.py + /duplicates routes) and the widened
feeds cross-source guard. Isolated via SHOPBOOKS_DATA_DIR before importing db.
"""
import os
import tempfile
from pathlib import Path
from urllib.parse import unquote

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_duptest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db          # noqa: E402
import ledger      # noqa: E402
import duplicates  # noqa: E402
import feeds       # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)

db.init()
con = db.connect()
acct = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
CHK, CARD, MAT, UNCAT = (acct["Business Checking"], acct["Credit Card 1"],
                         acct["Materials & Supplies"], acct["Uncategorized Expense"])

# A genuine double-post: same account, same amount, one day apart (classic feed-vs-statement twin).
dup_a = ledger.post_entry(con, "2026-04-10", "TOOL VENDOR", [(MAT, 9999), (CARD, -9999)])
dup_b = ledger.post_entry(con, "2026-04-11", "TOOL VENDOR", [(MAT, 9999), (CARD, -9999)])
# A same-amount charge on the SAME account but far apart in time -> NOT a duplicate (window).
far = ledger.post_entry(con, "2026-06-01", "TOOL VENDOR", [(MAT, 9999), (CARD, -9999)])
# Same amount, same date, but a DIFFERENT account -> NOT grouped together.
other_acct = ledger.post_entry(con, "2026-04-10", "COINCIDENCE", [(MAT, 9999), (CHK, -9999)])
# A unique amount -> never a duplicate.
uniq = ledger.post_entry(con, "2026-04-10", "ONE OFF", [(MAT, 1234), (CHK, -1234)])
# A three-in-a-row cluster (chained within the window) on checking.
c1 = ledger.post_entry(con, "2026-05-01", "RENT", [(UNCAT, 200000), (CHK, -200000)])
c2 = ledger.post_entry(con, "2026-05-03", "RENT", [(UNCAT, 200000), (CHK, -200000)])
c3 = ledger.post_entry(con, "2026-05-05", "RENT", [(UNCAT, 200000), (CHK, -200000)])
con.commit()

groups = duplicates.find_duplicate_groups(con)

def group_ids(g):
    return sorted(e["entry_id"] for e in g["entries"])

all_ids = [group_ids(g) for g in groups]
ok(any(set(ids) == {dup_a, dup_b} for ids in all_ids), "the one-day-apart same-account twin is flagged as a group")
ok(any(set(ids) == {c1, c2, c3} for ids in all_ids), "a chained run of near-date repeats is one group of three")
ok(not any(far in ids for ids in all_ids), "a same-amount charge outside the window is NOT grouped")
ok(not any(uniq in ids for ids in all_ids), "a unique-amount entry is never a duplicate")
ok(not any(other_acct in ids and dup_a in ids for ids in all_ids),
   "same amount on a different account is not grouped with the card twin")
ok(all(len(g["entries"]) >= 2 for g in groups), "every reported group has at least two entries")

# each group carries the account + a display amount + counter-account for judging
twin = next(g for g in groups if set(group_ids(g)) == {dup_a, dup_b})
ok(twin["account_name"] == "Credit Card 1", "the group is anchored on the bank/card account, not the category")
ok(all(e["counter"] == "Materials & Supplies" for e in twin["entries"]),
   "each entry shows its counter (category) account")
ok(twin["amount_cents"] == -9999, "the group amount is the signed card-leg amount")

# --- the pair is reported ONCE (anchored on the card leg, not also on the shared category) ---------
card_matches = [g for g in groups if set(group_ids(g)) == {dup_a, dup_b}]
ok(len(card_matches) == 1, "the duplicate pair is reported exactly once, not once per shared split")

# --- window boundary ------------------------------------------------------------------------------
ok(duplicates.WINDOW_DAYS == 4, "default window is 4 days")
tight = duplicates.find_duplicate_groups(con, window_days=1)
ok(any(set(group_ids(g)) == {dup_a, dup_b} for g in tight),
   "with a 1-day window the twin still groups (they ARE 1 day apart)")
# c1..c3 are 2 days apart each; a 1-day window should break them apart
ok(not any(set(group_ids(g)) == {c1, c2, c3} for g in tight),
   "with a 1-day window the 2-day-spaced run no longer chains into one group")

# --- feeds cross-source guard now uses a date window ----------------------------------------------
# A posted card entry on 4/10; a feed txn 'the same charge' dated 4/12 (2 days off) must be caught.
ok(feeds._already_on_books(con, CARD, "2026-04-12", 9999),
   "a feed txn within CROSS_SOURCE_DAYS of a posted same-amount entry is treated as already on books")
ok(not feeds._already_on_books(con, CARD, "2026-04-30", 9999),
   "a same-amount entry well outside the window is NOT treated as already on books")

con.close()

# --- HTTP surface ---------------------------------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402
c = TestClient(app.app)

page = c.get("/duplicates")
ok(page.status_code == 200 and "Possible duplicates" in page.text, "the duplicates page renders")
ok("Delete checked" in page.text and "Credit Card 1" in page.text, "it lists the flagged groups")
ok("Find Duplicates" in c.get("/").text, "the nav links to Find Duplicates")

# delete one side of the twin -> it disappears from the report, the other survives
r = c.post("/duplicates/delete", data={"entry_ids": [str(dup_b)]}, follow_redirects=False)
ok(r.status_code == 303 and "Deleted 1" in unquote(r.headers["location"]), "delete redirects with a count")
con = db.connect()
ok(con.execute("SELECT 1 FROM entries WHERE id=?", (dup_a,)).fetchone() is not None, "the kept entry survives")
ok(con.execute("SELECT 1 FROM entries WHERE id=?", (dup_b,)).fetchone() is None, "the checked entry is deleted")
after = duplicates.find_duplicate_groups(con)
ok(not any(dup_a in group_ids(g) and dup_b in group_ids(g) for g in after),
   "the twin no longer appears as a duplicate group after the delete")

# locked-period entries are skipped, not aborted
db.set_setting(con, "books_locked_through", "2026-05-31")
con.commit()
con.close()
r2 = c.post("/duplicates/delete", data={"entry_ids": [str(c1), str(c2)]}, follow_redirects=False)
ok("2 skipped" in unquote(r2.headers["location"]), "locked-period entries are skipped and reported")
con = db.connect()
ok(con.execute("SELECT 1 FROM entries WHERE id=?", (c1,)).fetchone() is not None,
   "a locked entry was not deleted")
con.close()

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nDUPLICATES TESTS DONE")
