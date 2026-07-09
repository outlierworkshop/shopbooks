"""Tests for Products & Services (Item Schedule) management, QBO CSV importing, and invoice integrations.
Isolated via SHOPBOOKS_DATA_DIR.
"""
import os
import tempfile
import io
from pathlib import Path
from urllib.parse import unquote

# Set temp directory for data isolation BEFORE importing db/app
TMP = Path(tempfile.mkdtemp(prefix="shopbooks_items_test_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db  # noqa: E402
import ledger  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
client = TestClient(appmod.app)

# Initialize database
db.init()

con = db.connect()

# 1. Verify schema tables and columns exist
tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
ok("items" in tables, "items table created")

inv_items_cols = {r["name"] for r in con.execute("PRAGMA table_info(invoice_items)").fetchall()}
ok("item_id" in inv_items_cols, "invoice_items has item_id foreign key column")

# Get some seeded income account ID
sales_acct = con.execute("SELECT id FROM accounts WHERE name='Sales - square' OR name='Sales - Square'").fetchone()["id"]

# 2. Add Item via POST /items
r = client.post("/items", data={
    "name": "Hourly Planing Rate",
    "sku": "SKU-PLN",
    "description": "Standard hourly machine planing rate.",
    "unit_price": "75.50",
    "income_account_id": str(sales_acct)
}, follow_redirects=False)
ok(r.status_code == 303 and "/items" in r.headers["location"], "POST /items redirects to /items")

item = con.execute("SELECT * FROM items WHERE name='Hourly Planing Rate'").fetchone()
ok(item is not None, "Item inserted in database")
ok(item["sku"] == "SKU-PLN" and item["unit_cents"] == 7550, "SKU and rate cents match")
ok(item["income_account_id"] == sales_acct, "Income account mapping matches")

item_id = item["id"]

# 3. Update Item via POST /items/update
r = client.post("/items/update", data={
    "item_id": str(item_id),
    "name": "Hourly Planing Rate (Premium)",
    "sku": "SKU-PLN-PREM",
    "description": "Standard hourly machine planing rate for premium woods.",
    "unit_price": "95.00",
    "income_account_id": str(sales_acct),
    "active": "1"
}, follow_redirects=False)
ok(r.status_code == 303, "POST /items/update redirects successfully")

item_updated = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
ok(item_updated["name"] == "Hourly Planing Rate (Premium)", "Name updated successfully")
ok(item_updated["sku"] == "SKU-PLN-PREM" and item_updated["unit_cents"] == 9500, "SKU and price updated successfully")

# 4. Import Products & Services List from QBO CSV via POST /items/import-qbo
mock_csv_content = b'''Product/Service,SKU,Description,Price,Income Account
"Custom Cabinetry","SKU-CAB","Custom solid oak cabinets","250.00","Sales - Square"
"Staining Work","SKU-STN","Professional staining rate","80.00","Sales - ACH / Invoices"
'''

mock_file = ("products_export.csv", io.BytesIO(mock_csv_content))

r = client.post(
    "/items/import-qbo",
    files={"file": mock_file},
    follow_redirects=False
)
ok(r.status_code == 303, "POST /items/import-qbo returns 303 Redirect")

item_cab = con.execute("SELECT * FROM items WHERE name='Custom Cabinetry'").fetchone()
ok(item_cab is not None, "Custom Cabinetry imported successfully")
ok(item_cab["sku"] == "SKU-CAB" and item_cab["unit_cents"] == 25000, "Cabinetry SKU and price match")

item_stn = con.execute("SELECT * FROM items WHERE name='Staining Work'").fetchone()
ok(item_stn is not None, "Staining Work imported successfully")
ok(item_stn["sku"] == "SKU-STN" and item_stn["unit_cents"] == 8000, "Staining SKU and price match")

# 5. Check standard_items delivered in template context for GET /invoices/new
r = client.get("/invoices/new")
ok(r.status_code == 200, "GET /invoices/new returns 200 OK")

# 6. Check standard_items delivered in template context for GET /estimates/new
r = client.get("/estimates/new")
ok(r.status_code == 200, "GET /estimates/new returns 200 OK")

# Cleanup
con.close()
import shutil
shutil.rmtree(TMP, ignore_errors=True)
print("\nPRODUCTS & SERVICES MANAGEMENT TESTS DONE")
