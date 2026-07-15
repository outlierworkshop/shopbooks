"""The invoice/estimate line editor's inline "+ New service": POST /items/quick-create makes a
catalog item (name, price, income account) and returns it as JSON; the editor pages bootstrap the
income accounts the mini-form needs. Isolation: SHOPBOOKS_DATA_DIR before importing db."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_qcreate_")

import db  # noqa: E402
db.init()
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from testutil import ok  # noqa: E402

client = TestClient(appmod.app)
con = db.connect()
inc = con.execute("SELECT id FROM accounts WHERE type='income' AND active=1 ORDER BY id LIMIT 1").fetchone()["id"]

# --- create a service on the fly ---
r = client.post("/items/quick-create",
                data={"name": "Setup fee", "unit_price": "125.00", "income_account_id": str(inc),
                      "description": "Setup fee"})
ok(r.status_code == 200, "quick-create returns 200")
j = r.json()
ok(j["name"] == "Setup fee" and j["price"] == "125.00" and j["id"], "returns the new item as JSON")
row = con.execute("SELECT name, unit_cents, income_account_id FROM items WHERE id=?", (j["id"],)).fetchone()
ok(row["name"] == "Setup fee" and row["unit_cents"] == 12500 and row["income_account_id"] == inc,
   "the service is saved to the catalog with its income account")

# --- validation: empty name / bad price rejected with a JSON error, nothing saved ---
before = con.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
r2 = client.post("/items/quick-create", data={"name": "", "income_account_id": str(inc)})
ok(r2.status_code == 400 and "error" in r2.json(), "an empty name is rejected")
r3 = client.post("/items/quick-create", data={"name": "X", "unit_price": "abc", "income_account_id": str(inc)})
ok(r3.status_code == 400, "a non-numeric price is rejected")
ok(con.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == before, "nothing saved on a rejected create")

# --- the editor bootstraps income accounts for the "+ New service" mini-form ---
page = client.get("/invoices/new").text
ok("window.incomeAccounts" in page, "invoice editor exposes window.incomeAccounts")
ok(f'id: {inc}' in page, "the income account is listed for the mini-form")
ok("window.incomeAccounts" in client.get("/estimates/new").text, "estimate editor exposes income accounts too")

con.close()
print("\nITEMS QUICK-CREATE TESTS DONE")
