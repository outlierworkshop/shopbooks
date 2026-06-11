"""AI-review-pending button test. Isolated via SHOPBOOKS_DATA_DIR; AI monkeypatched (no network)."""
import io
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_aireview_"))
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import ai  # noqa: E402
import db  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from urllib.parse import unquote  # noqa: E402
ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)
loc = lambda r: unquote(r.headers["location"])
client = TestClient(appmod.app)

con = db.connect()
card = con.execute("SELECT id FROM accounts WHERE name='Credit Card 1'").fetchone()["id"]
supplies = con.execute("SELECT name FROM accounts WHERE name='Materials & Supplies'").fetchone()["name"]
software = con.execute("SELECT name FROM accounts WHERE name='Software & Subscriptions'").fetchone()["name"]
# remove the seeded ADOBE rule so the AI path (not the rule) handles that row
con.execute("DELETE FROM rules WHERE pattern='ADOBE'")
con.commit()
con.close()

# import a CSV with one rule-matchable row (HOME DEPOT) and one that needs AI (MysteryVendor)
csv_data = ("Date,Description,Amount\n"
            "02/01/2026,HOME DEPOT #5,-50.00\n"
            "02/02/2026,MYSTERYVENDOR LLC,-20.00\n"
            "02/03/2026,ADOBE SUBSCR,-15.00\n")
client.post("/import", files={"file": ("c.csv", io.BytesIO(csv_data.encode()), "text/csv")}, data={"account_id": str(card)})

con = db.connect()
rows = {r["description"]: r for r in con.execute("SELECT * FROM staged WHERE status='pending'")}
con.close()
# at import (no AI key) HOME DEPOT got a rule; the other two are uncategorized
ok(rows["HOME DEPOT #5"]["category_id"] is not None, "rule categorized HOME DEPOT at import")
ok(rows["MYSTERYVENDOR LLC"]["category_id"] is None, "MysteryVendor uncategorized before AI review")
ok(rows["ADOBE SUBSCR"]["category_id"] is None, "Adobe uncategorized before AI review (rule removed)")

# AI off -> button path returns a helpful note, no changes
r = client.post("/review", data={"ai_review": "1"}, follow_redirects=False)
ok(r.status_code == 303 and "AI is off" in loc(r), "AI-off path gives a clear note")

# simulate AI on: monkeypatch availability + categorize
ai.available = lambda con: True
def fake_categorize(con, txns, names):
    out = []
    for t in txns:
        d = t["description"].upper()
        out.append(software if "ADOBE" in d else (supplies if "MYSTERY" in d else "Office Supplies"))
    return out
ai.categorize = lambda con, txns, names: fake_categorize(con, txns, names)

r = client.post("/review", data={"ai_review": "1"}, follow_redirects=False)
ok(r.status_code == 303 and "AI review done" in loc(r), "AI review redirects with summary")

con = db.connect()
def cat_name(desc):
    cid = con.execute("SELECT category_id FROM staged WHERE description=?", (desc,)).fetchone()["category_id"]
    return con.execute("SELECT name FROM accounts WHERE id=?", (cid,)).fetchone()["name"] if cid else None
ok(cat_name("MYSTERYVENDOR LLC") == supplies, "AI categorized MysteryVendor -> Materials & Supplies")
ok(cat_name("ADOBE SUBSCR") == software, "AI categorized Adobe -> Software & Subscriptions")
ok(cat_name("HOME DEPOT #5") is not None, "HOME DEPOT still categorized (rule)")
# nothing posted - all still pending
n_pending = con.execute("SELECT COUNT(*) c FROM staged WHERE status='pending'").fetchone()["c"]
n_entries = con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
con.close()
ok(n_pending == 3 and n_entries == 0, "AI review posts nothing - all 3 still pending, 0 entries")

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nAI-REVIEW TESTS DONE")
