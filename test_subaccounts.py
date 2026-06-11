"""Sub-accounts: creation, validation, hierarchical labels, report roll-up. Isolated."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_sub_")
import db  # noqa: E402
import ledger  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ok = lambda c, w: print(("PASS" if c else "FAIL"), w)
client = TestClient(appmod.app)

con = db.connect()
veh = con.execute("SELECT id, type, kind FROM accounts WHERE name='Vehicle Expenses'").fetchone()
card = con.execute("SELECT id FROM accounts WHERE name='Credit Card 1'").fetchone()["id"]
con.close()

# create two sub-accounts under Vehicle Expenses
client.post("/accounts", data={"name": "Vehicle Fuel", "parent_id": str(veh["id"])})
client.post("/accounts", data={"name": "Vehicle Maintenance", "parent_id": str(veh["id"])})
con = db.connect()
fuel = con.execute("SELECT * FROM accounts WHERE name='Vehicle Fuel'").fetchone()
maint = con.execute("SELECT * FROM accounts WHERE name='Vehicle Maintenance'").fetchone()
ok(fuel["parent_id"] == veh["id"] and fuel["type"] == "expense" and fuel["kind"] == veh["kind"],
   "sub-account created with parent + inherited type/kind")
con.close()

# hierarchical label in dropdown options
opts = {o["id"]: o["label"] for o in appmod.categories(db.connect())}
ok(opts[fuel["id"]] == "Vehicle Expenses : Vehicle Fuel", "sub-account label is 'Parent : Child'")
ok(opts[veh["id"]] == "Vehicle Expenses", "parent label is plain name")

# validation: can't nest 3 levels (sub-account can't be a parent)
con = db.connect(); office = con.execute("SELECT id FROM accounts WHERE name='Office Supplies'").fetchone()["id"]; con.close()
r = client.post("/accounts", data={"name": "Deeper", "parent_id": str(fuel["id"])}, follow_redirects=False)
from urllib.parse import unquote  # noqa: E402
ok("two levels" in unquote(r.headers.get("location", "")), "rejects a 3rd level (parent must be top-level)")

# validation: re-parent across types is rejected
r = client.post("/accounts/parent", data={"account_id": str(fuel["id"]), "parent_id": str(card)},
                follow_redirects=False)
ok("same type" in unquote(r.headers.get("location", "")), "rejects re-parent to a different type")
con = db.connect()
ok(con.execute("SELECT parent_id FROM accounts WHERE id=?", (fuel["id"],)).fetchone()["parent_id"] == veh["id"],
   "rejected re-parent left the account unchanged")
con.close()

# post expenses to the two sub-accounts + a direct posting to the parent, then check roll-up
con = db.connect()
ledger.post_entry(con, "2026-04-01", "Gas", [(fuel["id"], 6000), (card, -6000)])
ledger.post_entry(con, "2026-04-02", "Oil change", [(maint["id"], 4000), (card, -4000)])
ledger.post_entry(con, "2026-04-03", "Car wash (direct to parent)", [(veh["id"], 1000), (card, -1000)])
con.commit()
p = ledger.pnl(con, "2026-01-01", "2026-12-31")
con.close()
veh_node = next(x for x in p["expenses"] if x["name"] == "Vehicle Expenses")
ok(veh_node["amount"] == 11000, f"parent rolls up children + direct: 60+40+10 = 110.00 (got {veh_node['amount']})")
ok(veh_node["own"] == 1000, "parent 'direct' (own) postings tracked separately")
kid_names = {c["name"]: c["amount"] for c in veh_node["children"]}
ok(kid_names.get("Vehicle Fuel") == 6000 and kid_names.get("Vehicle Maintenance") == 4000,
   "children listed with their own amounts")

# P&L total still equals the sum of all expense postings (no double-count from roll-up)
ok(p["total_expenses"] == 11000, f"total expenses correct, no double counting (got {p['total_expenses']})")

# CSV export flattens the tree (parent, direct, children, subtotal)
r = client.get("/reports/pnl.csv?start=2026-01-01&end=2026-12-31")
body = r.text
ok("Vehicle Fuel" in body and "Total Vehicle Expenses" in body, "P&L CSV shows sub-accounts + subtotal")

print("\nSUB-ACCOUNT TESTS DONE")
