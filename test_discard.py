"""Discard-batch test. Isolated via SHOPBOOKS_DATA_DIR."""
import io
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_discard_")
import db  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ok = lambda c, w: print(("PASS" if c else "FAIL"), w)
client = TestClient(appmod.app)

con = db.connect()
card = con.execute("SELECT id FROM accounts WHERE name='Credit Card 1'").fetchone()["id"]
uncat = con.execute("SELECT id FROM accounts WHERE name='Uncategorized Expense'").fetchone()["id"]
con.close()

csv = "Date,Description,Amount\n01/05/2026,A STORE,-10.00\n01/06/2026,B STORE,-20.00\n"
client.post("/import", files={"file": ("c.csv", io.BytesIO(csv.encode()), "text/csv")}, data={"account_id": str(card)})

con = db.connect()
batch = con.execute("SELECT id FROM batches ORDER BY id DESC LIMIT 1").fetchone()["id"]
# post ONE row so we can prove discard keeps posted rows
first = con.execute("SELECT id FROM staged WHERE status='pending' ORDER BY id LIMIT 1").fetchone()["id"]
con.close()
client.post("/review", data={f"cat_{first}": str(uncat), "post_one": str(first)})

con = db.connect()
pending_before = con.execute("SELECT COUNT(*) c FROM staged WHERE status='pending'").fetchone()["c"]
posted_before = con.execute("SELECT COUNT(*) c FROM staged WHERE status='posted'").fetchone()["c"]
con.close()
ok(pending_before == 1 and posted_before == 1, "setup: 1 pending, 1 posted")

client.post("/review", data={"discard_batch": str(batch)})

con = db.connect()
pending_after = con.execute("SELECT COUNT(*) c FROM staged WHERE status='pending'").fetchone()["c"]
posted_after = con.execute("SELECT COUNT(*) c FROM staged WHERE status='posted'").fetchone()["c"]
entries = con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
con.close()
ok(pending_after == 0, "discard removed the pending rows")
ok(posted_after == 1 and entries == 1, "discard kept the already-posted row and its ledger entry")

print("\nDISCARD TESTS DONE")
