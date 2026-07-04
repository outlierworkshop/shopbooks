"""Split transactions: multiple categories on one entry, from both the manual-entry form and the
Review queue. Isolated via SHOPBOOKS_DATA_DIR (set BEFORE importing db/app)."""
import io
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_split_")
import db  # noqa: E402
import ledger  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ok = lambda c, w: print(("PASS" if c else "FAIL"), w)
client = TestClient(appmod.app)

con = db.connect()
chk = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
card = con.execute("SELECT id FROM accounts WHERE name='Credit Card 1'").fetchone()["id"]
exps = con.execute("SELECT id, name FROM accounts WHERE type='expense' ORDER BY name LIMIT 2").fetchall()
e1, e2 = exps[0]["id"], exps[1]["id"]
inc = con.execute("SELECT id FROM accounts WHERE type='income' LIMIT 1").fetchone()["id"]
con.close()


def legs(entry_id):
    con = db.connect()
    rows = con.execute("SELECT account_id, amount_cents FROM splits WHERE entry_id=?", (entry_id,)).fetchall()
    con.close()
    return {r["account_id"]: r["amount_cents"] for r in rows}


def zero_sum_ok():
    con = db.connect()
    bad = con.execute("SELECT entry_id, SUM(amount_cents) t FROM splits GROUP BY entry_id HAVING t!=0").fetchall()
    con.close()
    return not bad


# ---- 1. Manual entry, money OUT, split across two expense categories ----
client.post("/entry/new", data={
    "date": "2026-03-10", "payee": "Costco run", "direction": "out",
    "source_account": str(chk), "scat": [str(e1), str(e2)], "samt": ["60.00", "40.00"], "memo": "",
})
con = db.connect()
eid = con.execute("SELECT id FROM entries WHERE payee='Costco run'").fetchone()["id"]
con.close()
L = legs(eid)
ok(len(L) == 3, f"manual split booked one entry with 3 legs (got {len(L)})")
ok(L.get(e1) == 6000 and L.get(e2) == 4000, "both expense categories debited by their share")
ok(L.get(chk) == -10000, "source (checking) credited the $100 total")

# ---- 2. Manual entry, money IN, income to checking ----
client.post("/entry/new", data={
    "date": "2026-03-11", "payee": "Client deposit", "direction": "in",
    "source_account": str(chk), "scat": [str(inc)], "samt": ["250.00"], "memo": "",
})
con = db.connect()
eid2 = con.execute("SELECT id FROM entries WHERE payee='Client deposit'").fetchone()["id"]
con.close()
L2 = legs(eid2)
ok(L2.get(inc) == -25000 and L2.get(chk) == 25000, "money-in: income credited, checking debited")

# ---- 3. Reject a category that equals the source account ----
r = client.post("/entry/new", data={
    "date": "2026-03-12", "payee": "Bad self-ref", "direction": "out",
    "source_account": str(chk), "scat": [str(chk)], "samt": ["5.00"],
})
con = db.connect()
exists = con.execute("SELECT COUNT(*) c FROM entries WHERE payee='Bad self-ref'").fetchone()["c"]
con.close()
ok(exists == 0, "manual entry with category == source is rejected, nothing posted")


# ---- 4. Review: split a staged card charge across two categories ----
def import_csv(name, data, acct):
    client.post("/import", files={"file": (name, io.BytesIO(data.encode()), "text/csv")}, data={"account_id": str(acct)})


import_csv("card.csv", "Date,Description,Amount\n03/15/2026,BIG BOX STORE,-100.00\n", card)
con = db.connect()
sid = con.execute("SELECT id FROM staged WHERE description='BIG BOX STORE' AND status='pending'").fetchone()["id"]
con.close()

# first try an UNBALANCED split (60 + 30 != 100) -> must NOT post
r = client.post("/review", data={
    "post_one": str(sid), f"splitmode_{sid}": "1",
    f"scat_{sid}": [str(e1), str(e2)], f"samt_{sid}": ["60.00", "30.00"],
})
con = db.connect()
st = con.execute("SELECT status FROM staged WHERE id=?", (sid,)).fetchone()["status"]
con.close()
ok(st == "pending", "unbalanced split does NOT post (row stays pending)")
ok("err=" in str(r.url) or r.status_code == 200, "unbalanced split reports an error back to Review")

# now a BALANCED split (60 + 40 = 100) -> posts one entry, 3 legs
client.post("/review", data={
    "post_one": str(sid), f"splitmode_{sid}": "1",
    f"scat_{sid}": [str(e1), str(e2)], f"samt_{sid}": ["60.00", "40.00"],
})
con = db.connect()
row = con.execute("SELECT status, entry_id, category_id FROM staged WHERE id=?", (sid,)).fetchone()
con.close()
ok(row["status"] == "posted", "balanced split posts the staged row")
ok(row["category_id"] is None, "a split leaves staged.category_id NULL (no single category)")
L3 = legs(row["entry_id"])
ok(L3.get(e1) == 6000 and L3.get(e2) == 4000, "staged split: each expense leg carries its share (money out = +)")
ok(L3.get(card) == -10000, "staged split: the card (source) balances the total")

# ---- 6. Turn a simple posted entry into a split, then re-allocate it (register editor) ----
# a plain single-category card charge ($100 -> Advertising), booked via manual entry
client.post("/entry/new", data={
    "date": "2026-04-01", "payee": "Split me later", "direction": "out",
    "source_account": str(card), "scat": [str(e1)], "samt": ["100.00"],
})
con = db.connect()
seid = con.execute("SELECT id FROM entries WHERE payee='Split me later'").fetchone()["id"]
con.close()
ok(len(legs(seid)) == 2, "starts as a simple 2-leg entry")

# split it across two categories, anchored to the card (the register account)
client.post(f"/entry/{seid}/splits", data={
    "back": f"/register/{card}", "register_account_id": str(card), "direction": "out",
    "scat": [str(e1), str(e2)], "samt": ["70.00", "30.00"],
})
LS = legs(seid)
ok(len(LS) == 3, f"editing turns it into a 3-leg split (got {len(LS)})")
ok(LS.get(e1) == 7000 and LS.get(e2) == 3000, "the two category legs carry the new allocation")
ok(LS.get(card) == -10000, "the anchor (card) still balances the $100 total")

# re-allocate again (80/20) — an existing split can be edited
client.post(f"/entry/{seid}/splits", data={
    "back": f"/register/{card}", "register_account_id": str(card), "direction": "out",
    "scat": [str(e1), str(e2)], "samt": ["80.00", "20.00"],
})
LS2 = legs(seid)
ok(LS2.get(e1) == 8000 and LS2.get(e2) == 2000 and LS2.get(card) == -10000, "an existing split can be re-allocated")

# an empty submission is rejected and leaves the entry untouched
before = legs(seid)
client.post(f"/entry/{seid}/splits", data={
    "back": f"/register/{card}", "register_account_id": str(card), "direction": "out",
    "scat": [""], "samt": [""],
})
ok(legs(seid) == before, "an empty split submission changes nothing")

# ---- 7. Ledger invariant holds across everything we posted ----
ok(zero_sum_ok(), "every entry's splits sum to zero")

print("\nSPLIT TESTS DONE")
