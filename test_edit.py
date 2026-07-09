"""Test inline entry editing. Isolated via SHOPBOOKS_DATA_DIR."""
import os
import tempfile
import shutil
from urllib.parse import unquote

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_edit_")
import db  # noqa: E402
import ledger  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
loc = lambda r: unquote(r.headers.get("location", ""))
client = TestClient(appmod.app)

# Setup initial accounts and entries
con = db.connect()
checking = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
supplies = con.execute("SELECT id FROM accounts WHERE name='Office Supplies'").fetchone()["id"]
postage = con.execute("SELECT id FROM accounts WHERE name='Shipping & Postage'").fetchone()["id"]
con.execute("INSERT INTO jobs(name, status) VALUES('Job A', 'active')")
con.execute("INSERT INTO jobs(name, status) VALUES('Job B', 'active')")
job1 = con.execute("SELECT id FROM jobs WHERE name='Job A'").fetchone()["id"]
job2 = con.execute("SELECT id FROM jobs WHERE name='Job B'").fetchone()["id"]

# Create a 2-split entry (Checking -> Supplies)
entry_id = ledger.post_entry(con, "2026-06-01", "Acme Supplies",
                             [(supplies, 10000), (checking, -10000)],
                             memo="Original memo", job_id=job1)
con.commit()
con.close()

# 1. Test direct ledger helper updates
con = db.connect()
ledger.update_entry_fields(con, entry_id, "Acme Inc.", "New memo", postage, job2, "2026-06-02", checking)
con.commit()

# Assert entries table updated
entry = con.execute("SELECT date, payee, memo, job_id FROM entries WHERE id=?", (entry_id,)).fetchone()
ok(entry["date"] == "2026-06-02", "date updated")
ok(entry["payee"] == "Acme Inc.", "payee updated")
ok(entry["memo"] == "New memo", "memo updated")
ok(entry["job_id"] == job2, "job_id updated")

# Assert splits updated (checking split remains, supplies split changed to postage)
splits = con.execute("SELECT account_id, amount_cents FROM splits WHERE entry_id=?", (entry_id,)).fetchall()
ok(len(splits) == 2, "still has 2 splits")
accounts = {s["account_id"]: s["amount_cents"] for s in splits}
ok(checking in accounts and accounts[checking] == -10000, "checking split preserved")
ok(postage in accounts and accounts[postage] == 10000, "supplies split changed to postage")
ok(supplies not in accounts, "old supplies split removed")

# Check splits sum to zero invariant
sum_cents = con.execute("SELECT SUM(amount_cents) s FROM splits WHERE entry_id=?", (entry_id,)).fetchone()["s"]
ok(sum_cents == 0, "splits sum to zero invariant holds")
con.close()

# 2. Test HTTP route entry_edit endpoint
r = client.post(
    f"/entry/edit/{entry_id}",
    data={
        "date": "2026-06-03",
        "payee": "Acme Route",
        "memo": "Route memo",
        "category_id": str(supplies), # Change back to supplies
        "job_id": "", # Clear job
        "register_account_id": str(checking),
        "back": f"/register/{checking}"
    },
    follow_redirects=False
)
ok(r.status_code == 303, "route returns 303 redirect")
ok(loc(r) == f"/register/{checking}", "redirects to back URL")

con = db.connect()
entry = con.execute("SELECT date, payee, memo, job_id FROM entries WHERE id=?", (entry_id,)).fetchone()
ok(entry["date"] == "2026-06-03", "date updated via route")
ok(entry["payee"] == "Acme Route", "payee updated via route")
ok(entry["memo"] == "Route memo", "memo updated via route")
ok(entry["job_id"] is None, "job_id cleared via route")

splits = con.execute("SELECT account_id, amount_cents FROM splits WHERE entry_id=?", (entry_id,)).fetchall()
accounts = {s["account_id"]: s["amount_cents"] for s in splits}
ok(checking in accounts and accounts[checking] == -10000, "checking split still preserved")
ok(supplies in accounts and accounts[supplies] == 10000, "category changed back to supplies via route")
con.close()

# 2b. Test modifying register account (bank/card) via HTTP route
con = db.connect()
card = con.execute("SELECT id FROM accounts WHERE name='Credit Card 1'").fetchone()["id"]
con.close()

r = client.post(
    f"/entry/edit/{entry_id}",
    data={
        "date": "2026-06-03",
        "payee": "Acme Route",
        "memo": "Route memo",
        "account_id": str(card), # Move from Checking to Credit Card 1
        "category_id": str(supplies),
        "job_id": "",
        "register_account_id": str(checking),
        "back": f"/register/{checking}"
    },
    follow_redirects=False
)
ok(r.status_code == 303, "route returns 303 redirect on account change")

con = db.connect()
splits = con.execute("SELECT account_id, amount_cents FROM splits WHERE entry_id=?", (entry_id,)).fetchall()
accounts = {s["account_id"]: s["amount_cents"] for s in splits}
ok(card in accounts and accounts[card] == -10000, "checking split moved to Credit Card 1")
ok(supplies in accounts and accounts[supplies] == 10000, "category split remains supplies")
con.close()

# 3. Test HTTP route date validation error
r = client.post(
    f"/entry/edit/{entry_id}",
    data={
        "date": "invalid-date",
        "payee": "Acme Route",
        "memo": "Route memo",
        "category_id": str(supplies),
        "job_id": "",
        "register_account_id": str(checking),
        "back": f"/register/{checking}"
    },
    follow_redirects=False
)
ok(r.status_code == 303, "route returns 303 redirect on error")
ok("err=" in loc(r) and "unrecognized date" in loc(r), "redirects with error parameter")

shutil.rmtree(os.environ["SHOPBOOKS_DATA_DIR"], ignore_errors=True)
print("\nENTRY EDIT TESTS DONE")
