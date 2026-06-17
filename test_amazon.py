"""Amazon order-history import -> receipts. Isolated via SHOPBOOKS_DATA_DIR."""
import io
import os
import tempfile
from urllib.parse import unquote

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_amzn_")
import db  # noqa: E402
import ledger  # noqa: E402
import importer  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ok = lambda c, w: print(("PASS" if c else "FAIL"), w)
client = TestClient(appmod.app)

# --- parser: newer "Retail.OrderHistory" format, multi-item order grouped + summed ---
new_csv = (
    "Order ID,Order Date,Product Name,Total Owed,Quantity\n"
    "111-2223334-0001,2026-02-10T08:30:00Z,USB Cable,12.99,1\n"
    "111-2223334-0001,2026-02-10T08:30:00Z,Wall Charger,17.00,1\n"
    "111-9998887-0002,2026-02-14T10:00:00Z,Sandpaper 5pk,8.50,1\n"
)
orders = importer.parse_amazon_orders(new_csv.encode())
by_id = {o["order_id"]: o for o in orders}
ok(len(orders) == 2, f"two orders grouped (got {len(orders)})")
ok(by_id["111-2223334-0001"]["total_cents"] == 2999, "multi-item order summed to 29.99")
ok(by_id["111-2223334-0001"]["date"] == "2026-02-10", "ISO datetime reduced to date")
ok("USB Cable" in by_id["111-2223334-0001"]["items"], "item names collected")

# --- parser: older Order Reports format (Title / Item Total), with a title prefix line ---
old_csv = (
    "Your Orders Report\n"
    "Order Date,Order ID,Title,Item Total\n"
    "02/20/2026,D01-111,Drill Bits,\"$45.00\"\n"
)
o2 = importer.parse_amazon_orders(old_csv.encode())
ok(len(o2) == 1 and o2[0]["total_cents"] == 4500 and o2[0]["date"] == "2026-02-20",
   "older format + prefix line + MM/DD/YYYY parsed")

# --- Business/Order Reports format: order-level total taken ONCE, not summed from items ---
# (item subtotals 100+60=160, but an order-level promo makes the real Order Net Total 148.62)
biz_csv = (
    "Order Date,Order ID,Title,Order Promotion,Order Net Total,Item Subtotal\n"
    "06/11/2026,111-AAA,Widget A,\"-11.38\",\"148.62\",\"100.00\"\n"
    "06/11/2026,111-AAA,Widget B,\"-11.38\",\"148.62\",\"60.00\"\n"
)
b = importer.parse_amazon_orders(biz_csv.encode())
ok(len(b) == 1, "business format: one order")
ok(b[0]["total_cents"] == 14862, f"order-level total taken once (148.62), not item-sum (160.00) — got {b[0]['total_cents']}")
ok(len(b[0]["items"]) == 2, "both item titles still collected")

# --- end to end: a matching card charge exists -> auto-match ---
con = db.connect()
card = con.execute("SELECT id FROM accounts WHERE name='Credit Card 1'").fetchone()["id"]
supplies = con.execute("SELECT id FROM accounts WHERE name='Materials & Supplies'").fetchone()["id"]
ledger.post_entry(con, "2026-02-11", "AMAZON MKTPL", [(supplies, 2999), (card, -2999)])  # matches order 1 within 7d
con.commit(); con.close()

r = client.post("/receipts/import-amazon",
                files={"file": ("Retail.OrderHistory.1.csv", io.BytesIO(new_csv.encode()), "text/csv")},
                follow_redirects=False)
ok(r.status_code == 303 and "1 matched" in unquote(r.headers["location"]), "import auto-matched the 29.99 order")
con = db.connect()
amz = {d["amount_cents"]: d for d in con.execute("SELECT * FROM documents WHERE vendor='Amazon'")}
ok(len(amz) == 2, "two Amazon receipt documents created")
matched = con.execute("SELECT status FROM documents WHERE amount_cents=2999 AND vendor='Amazon'").fetchone()["status"]
ok(matched == "matched", "29.99 Amazon order linked to the card charge")
ok(con.execute("SELECT status FROM documents WHERE amount_cents=850").fetchone()["status"] == "unmatched",
   "the 8.50 order with no charge stays unmatched")
con.close()

# --- re-import dedupes on order id ---
r = client.post("/receipts/import-amazon",
                files={"file": ("Retail.OrderHistory.1.csv", io.BytesIO(new_csv.encode()), "text/csv")},
                follow_redirects=False)
ok("2 already imported" in unquote(r.headers["location"]), "re-import dedupes by order id")
con = db.connect()
ok(con.execute("SELECT COUNT(*) c FROM documents WHERE vendor='Amazon'").fetchone()["c"] == 2,
   "no duplicate Amazon documents after re-import")
ok(con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 1, "importing receipts never posts ledger entries")
con.close()

# --- a non-Amazon CSV gives a clear error ---
r = client.post("/receipts/import-amazon",
                files={"file": ("x.csv", io.BytesIO(b"foo,bar\n1,2\n"), "text/csv")}, follow_redirects=False)
ok("Couldn't find Amazon columns" in unquote(r.headers["location"]), "non-Amazon CSV -> clear error")

import shutil  # noqa: E402
shutil.rmtree(os.environ["SHOPBOOKS_DATA_DIR"], ignore_errors=True)
print("\nAMAZON TESTS DONE")
