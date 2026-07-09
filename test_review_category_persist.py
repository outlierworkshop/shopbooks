"""Changing a transaction's category in Review and then posting/skipping a DIFFERENT row must not
revert the change on reload. Isolated via SHOPBOOKS_DATA_DIR."""
import io
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_reviewcat_")
import db  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
client = TestClient(appmod.app)

con = db.connect()
chk = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
mats = con.execute("SELECT id FROM accounts WHERE name='Materials & Supplies'").fetchone()["id"]
ship = con.execute("SELECT id FROM accounts WHERE name='Shipping & Postage'").fetchone()["id"]
con.close()

# two uncategorized pending rows
csv = "Date,Description,Amount\n03/01/2026,VENDOR A,-40.00\n03/02/2026,VENDOR B,-25.00\n"
client.post("/import", files={"file": ("s.csv", io.BytesIO(csv.encode()), "text/csv")}, data={"account_id": str(chk)})

con = db.connect()
rows = {r["description"]: r["id"] for r in con.execute("SELECT id, description FROM staged WHERE status='pending'")}
a, b = rows["VENDOR A"], rows["VENDOR B"]
con.close()

# Set a category on B, then POST A (a different row). B's category must persist.
client.post("/review", data={
    "post_one": str(a),
    f"cat_{a}": str(mats),
    f"cat_{b}": str(ship),   # changed but not posted
})

con = db.connect()
a_row = con.execute("SELECT status FROM staged WHERE id=?", (a,)).fetchone()
b_row = con.execute("SELECT status, category_id FROM staged WHERE id=?", (b,)).fetchone()
con.close()

ok(a_row["status"] == "posted", "the posted row (A) is posted")
ok(b_row["status"] == "pending", "the other row (B) is still pending")
ok(b_row["category_id"] == ship, "B's changed category persisted (did not revert)")

print("\nREVIEW CATEGORY PERSIST TESTS DONE")
