"""Recategorize a matched transaction from its receipt. Isolated; AI monkeypatched."""
import io
import os
import tempfile
from urllib.parse import unquote

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_recat_")
import ai  # noqa: E402
import db  # noqa: E402
import ledger  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
loc = lambda r: unquote(r.headers.get("location", ""))
client = TestClient(appmod.app)

con = db.connect()
card = con.execute("SELECT id FROM accounts WHERE name='Credit Card 1'").fetchone()["id"]
chk = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
office = con.execute("SELECT id FROM accounts WHERE name='Office Supplies'").fetchone()["id"]
tools = con.execute("SELECT id FROM accounts WHERE name='Tools & Small Equipment'").fetchone()["id"]
mats = con.execute("SELECT id FROM accounts WHERE name='Materials & Supplies'").fetchone()["id"]

# a posted Amazon charge categorized (weakly) as Office Supplies
e1 = ledger.post_entry(con, "2026-04-10", "AMAZON MKTPL", [(office, 91399), (card, -91399)])
# a transfer (no category leg) - must be left untouched
e2 = ledger.post_entry(con, "2026-04-12", "CC payment", [(chk, -50000), (card, 50000)])
con.commit(); con.close()

# --- primitive: set_entry_category re-points the expense leg, stays balanced ---
con = db.connect()
old = ledger.set_entry_category(con, e1, tools)
ok(old is not None and old["name"] == "Office Supplies", "set_entry_category returns the old category")
cat = ledger.entry_category(con, e1)
ok(cat["account_id"] == tools, "expense leg now points to Tools & Small Equipment")
bad = con.execute("SELECT entry_id, SUM(amount_cents) t FROM splits GROUP BY entry_id HAVING t!=0").fetchall()
ok(not bad, "entry still balances after recategorize")
ok(ledger.set_entry_category(con, e2, tools) is None, "transfer (no single expense leg) is refused")
# cross-type guard: can't point an expense leg at a bank account
ledger.set_entry_category(con, e1, office)  # reset to office for the AI test below
con.commit(); con.close()
ok(True, "reset to Office Supplies for the next step")

# --- attach a receipt with item detail, then AI-recategorize ---
con = db.connect()
cur = con.execute("INSERT INTO documents(filename,path,vendor,doc_date,amount_cents,status,entry_id,sha256) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  ("amazon_x.txt", str(db.DOCS / "amazon_x.txt"), "Amazon", "2026-04-10", 91399,
                   "matched", e1, "hash-x"))
db.DOCS.mkdir(parents=True, exist_ok=True)
(db.DOCS / "amazon_x.txt").write_text("Amazon order\n  - RTX 5070 Ti Graphics Card\nOrder total: $913.99", encoding="utf-8")
doc_id = cur.lastrowid
con.commit(); con.close()

# AI returns 'Tools & Small Equipment' for the GPU items
ai.available = lambda con: True
def fake_categorize(con, txns, names):
    return ["Tools & Small Equipment" if "RTX" in t["description"] or "Graphics" in t["description"]
            else "Office Supplies" for t in txns]
ai.categorize = lambda con, txns, names: fake_categorize(con, txns, names)

r = client.post("/receipts/recategorize", data={"doc_id": str(doc_id)}, follow_redirects=False)
ok("msg=" in loc(r) and "updated" in loc(r), "recategorize route reports success")
con = db.connect()
ok(ledger.entry_category(con, e1)["account_id"] == tools, "AI recategorized the GPU charge -> Tools & Small Equipment")
ne = con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
ok(ne == 2, "no entries created/deleted by recategorizing")
con.close()

# --- manual override via the dropdown route ---
r = client.post("/receipts/setcategory", data={"doc_id": str(doc_id), "account_id": str(mats)}, follow_redirects=False)
con = db.connect()
ok(ledger.entry_category(con, e1)["account_id"] == mats, "manual setcategory override works (reversible)")
con.close()

# --- batch route ---
r = client.post("/receipts/recategorize-all", follow_redirects=False)
ok("msg=" in loc(r), "batch recategorize route returns a summary")
con = db.connect()
ok(ledger.entry_category(con, e1)["account_id"] == tools, "batch re-applied the AI suggestion (GPU -> Tools)")
bad = con.execute("SELECT entry_id, SUM(amount_cents) t FROM splits GROUP BY entry_id HAVING t!=0").fetchall()
ok(not bad, "books still balanced after batch recategorize")
con.close()

import shutil  # noqa: E402
shutil.rmtree(os.environ["SHOPBOOKS_DATA_DIR"], ignore_errors=True)
print("\nRECATEGORIZE TESTS DONE")
