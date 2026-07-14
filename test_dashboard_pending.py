"""Dashboard 'Waiting for review' section lists UNPOSTED (pending) transactions and links to
/review, replacing the old 'Recent activity' log of posted entries (issue #81).
Isolated via SHOPBOOKS_DATA_DIR."""
import io
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_dash81_")
import db  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
client = TestClient(appmod.app)

chk = db.connect().execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]

# ---- empty state: nothing pending ----
page = client.get("/").text
ok("Waiting for review" in page, "dashboard has the 'Waiting for review' section")
ok("Nothing waiting" in page, "empty state shows the 'nothing waiting' message")
ok("Recent activity" not in page, "the old posted-entries 'Recent activity' section is gone")

# ---- account tiles are ALWAYS the top row, linking to each register (regressed once in 62d505b) ----
ok('class="kpis account-tiles"' in page, "the account-tiles top row is present")
ok(f'href="/register/{chk}"' in page, "each account tile links to its register")
ok("Business Checking" in page, "the checking-account tile shows on the dashboard")
ok(page.index('account-tiles') < page.index('Today at a glance'),
   "account tiles come before the day-brief hero (top row)")
ok("Registers ▾" in page, "the nav has a Registers dropdown to reach each account's register")

# ---- seed pending transactions via a CSV import ----
csv = ("Date,Description,Amount\n"
       "02/03/2026,BLUE BOTTLE COFFEE,-6.75\n"
       "02/04/2026,MCMASTER-CARR SUPPLY,-212.40\n"
       "02/05/2026,SQUARE PAYOUT,845.00\n")
client.post("/import", files={"file": ("s.csv", io.BytesIO(csv.encode()), "text/csv")},
            data={"account_id": str(chk)})

page = client.get("/").text
ok("MCMASTER-CARR SUPPLY" in page and "BLUE BOTTLE COFFEE" in page,
   "pending transaction descriptions appear on the dashboard")
ok("Review all" in page, "a link to Review all pending is shown when there are pending rows")
ok('href="/review"' in page, "the section links to /review")
# these are NOT posted — no ledger entries should exist yet
n_entries = db.connect().execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
ok(n_entries == 0, "listed transactions are genuinely unposted (no ledger entries)")

# ---- posting the pending rows empties the section again ----
con = db.connect()
ship = con.execute("SELECT id FROM accounts WHERE name='Shipping & Postage'").fetchone()["id"]
sids = [r["id"] for r in con.execute("SELECT id FROM staged WHERE status='pending'").fetchall()]
con.close()
form = {"post_all": "1"}
for sid in sids:
    form[f"cat_{sid}"] = str(ship)
client.post("/review", data=form)

page = client.get("/").text
ok("Nothing waiting" in page, "after posting everything, the section returns to the empty state")
ok("MCMASTER-CARR SUPPLY" not in page, "posted transactions no longer show in 'Waiting for review'")

print("\nDASHBOARD PENDING TESTS DONE")
