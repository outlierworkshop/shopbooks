"""Hide/reactivate accounts. Isolated via SHOPBOOKS_DATA_DIR."""
import os
import tempfile
from urllib.parse import unquote

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_deact_")
import db  # noqa: E402
import ledger  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
loc = lambda r: unquote(r.headers.get("location", ""))
client = TestClient(appmod.app)

con = db.connect()
unused = con.execute("SELECT id FROM accounts WHERE name='Contract Labor'").fetchone()["id"]   # seed, no history
card = con.execute("SELECT id FROM accounts WHERE name='Credit Card 1'").fetchone()["id"]
mats = con.execute("SELECT id FROM accounts WHERE name='Materials & Supplies'").fetchone()["id"]
# give Materials & Supplies a transaction so it can't be hidden
ledger.post_entry(con, "2026-01-01", "x", [(mats, 1000), (card, -1000)])
# a parent with an active child
veh = con.execute("SELECT id FROM accounts WHERE name='Vehicle Expenses'").fetchone()["id"]
con.execute("INSERT INTO accounts(name,type,kind,parent_id) VALUES('Fuel','expense','category',?)", (veh,))
con.commit(); con.close()

before = {o["name"] for o in appmod.categories(db.connect())}
ok("Contract Labor" in before, "unused account starts visible in the category picker")

# hide an unused account -> gone from picker, still in DB (inactive)
r = client.post("/accounts/active", data={"account_id": str(unused), "active": "0"}, follow_redirects=False)
ok(r.status_code == 303 and "err" not in loc(r), "hide unused account succeeds")
after = {o["name"] for o in appmod.categories(db.connect())}
ok("Contract Labor" not in after, "hidden account no longer offered as a category")
con = db.connect()
ok(con.execute("SELECT active FROM accounts WHERE id=?", (unused,)).fetchone()["active"] == 0, "row kept, just active=0")
con.close()

# can't hide an account with transactions
r = client.post("/accounts/active", data={"account_id": str(mats), "active": "0"}, follow_redirects=False)
ok("transactions" in loc(r), "refuses to hide an account that has history")
con = db.connect()
ok(con.execute("SELECT active FROM accounts WHERE id=?", (mats,)).fetchone()["active"] == 1, "account with history stays active")
con.close()

# can't hide a parent that still has an active child
r = client.post("/accounts/active", data={"account_id": str(veh), "active": "0"}, follow_redirects=False)
ok("sub-account" in loc(r), "refuses to hide a parent with active children")

# reactivate brings it back to the picker
r = client.post("/accounts/active", data={"account_id": str(unused), "active": "1"}, follow_redirects=False)
ok("Contract Labor" in {o["name"] for o in appmod.categories(db.connect())}, "reactivate restores it to the picker")

# include_inactive lists hidden ones with flags
con = db.connect()
client.post("/accounts/active", data={"account_id": str(unused), "active": "0"})
rows = {a["name"]: a for a in ledger.accounts_with_balances(con, include_inactive=True)}
ok("Contract Labor" in rows and rows["Contract Labor"]["active"] == 0, "include_inactive shows hidden accounts")
ok(rows["Materials & Supplies"]["has_history"] is True, "has_history flag set for accounts with splits")
con.close()

import shutil  # noqa: E402
shutil.rmtree(os.environ["SHOPBOOKS_DATA_DIR"], ignore_errors=True)
print("\nDEACTIVATE TESTS DONE")
