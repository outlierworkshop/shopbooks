"""Tests for year-end close / period lock (ledger.assert_unlocked + post/delete/edit guards).

Isolation pattern: point SHOPBOOKS_DATA_DIR at a temp dir BEFORE importing db. The lock is the
`books_locked_through` setting; once set, no write path may touch a transaction on or before it.
"""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_locktest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db        # noqa: E402
import ledger    # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)


def raises_locked(fn):
    try:
        fn()
        return False
    except ledger.LockedPeriodError:
        return True
    except Exception:
        return False  # any OTHER error is a failure of this test's intent


db.init()
con = db.connect()
acct = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
CHK, SALES, MAT = acct["Business Checking"], acct["Sales - Square"], acct["Materials & Supplies"]


def income(d):
    return ledger.post_entry(con, d, "income", [(CHK, 100000), (SALES, -100000)])


def expense(d):
    return ledger.post_entry(con, d, "expense", [(MAT, 5000), (CHK, -5000)])


# --- with nothing locked, everything works --------------------------------------
e2025 = expense("2025-06-15")
e2026 = expense("2026-06-15")
con.commit()
ok(ledger.locked_through(con) == "", "default: nothing is locked")
ok(isinstance(e2025, int) and isinstance(e2026, int), "posting works when unlocked")

# --- close the books through 2025-12-31 -----------------------------------------
db.set_setting(con, "books_locked_through", "2025-12-31")
con.commit()
ok(ledger.locked_through(con) == "2025-12-31", "lock date is read back")

ok(raises_locked(lambda: expense("2025-03-01")), "posting INTO the locked period is blocked")
ok(raises_locked(lambda: expense("2025-12-31")), "the close date itself is locked (on-or-before)")
e_new = None
try:
    e_new = expense("2026-02-01")
    con.commit()
    ok(True, "posting AFTER the locked period still works")
except Exception:
    ok(False, "posting AFTER the locked period still works")

ok(raises_locked(lambda: ledger.delete_entry(con, e2025)), "deleting a locked-period entry is blocked")
try:
    ledger.delete_entry(con, e_new); con.commit()
    ok(True, "deleting an entry after the lock works")
except Exception:
    ok(False, "deleting an entry after the lock works")

# editing a locked entry is blocked
ok(raises_locked(lambda: ledger.update_entry_fields(
    con, e2025, "x", "", None, None, "2025-06-15", CHK)), "editing a locked-period entry is blocked")
# moving an unlocked entry BACK into the locked period is blocked
ok(raises_locked(lambda: ledger.update_entry_fields(
    con, e2026, "x", "", None, None, "2025-06-15", CHK)), "moving an entry into the locked period is blocked")
# editing an unlocked entry, keeping it unlocked, works
try:
    ledger.update_entry_fields(con, e2026, "renamed", "", None, None, "2026-07-01", CHK); con.commit()
    moved = con.execute("SELECT date, payee FROM entries WHERE id=?", (e2026,)).fetchone()
    ok(moved["date"] == "2026-07-01" and moved["payee"] == "renamed", "editing an unlocked entry works")
except Exception:
    ok(False, "editing an unlocked entry works")

# --- reopen -> the period is editable again -------------------------------------
db.set_setting(con, "books_locked_through", "")
con.commit()
try:
    r = expense("2025-01-15"); con.commit()
    ok(isinstance(r, int), "after reopening, the previously-locked period accepts writes again")
except Exception:
    ok(False, "after reopening, the previously-locked period accepts writes again")

# assert_unlocked is a no-op with no lock and ignores empty dates
ledger.assert_unlocked(con, None, "", "2025-01-01")
ok(True, "assert_unlocked is a no-op when nothing is locked")

con.close()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nPERIOD LOCK TESTS DONE")
