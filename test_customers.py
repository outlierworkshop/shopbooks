"""Tests for Customers page, details, file uploads, notes, and statement reports.
Isolated via SHOPBOOKS_DATA_DIR.
"""
import os
import tempfile
import io
from pathlib import Path
from urllib.parse import unquote

# Set temp directory for data isolation BEFORE importing db/app
TMP = Path(tempfile.mkdtemp(prefix="shopbooks_customers_test_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db  # noqa: E402
import ledger  # noqa: E402
import invoicing  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)
client = TestClient(appmod.app)

# Initialize database
db.init()

con = db.connect()

# 1. Verify schema tables exist
tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
ok("customer_files" in tables, "customer_files table created")
ok("customer_notes" in tables, "customer_notes table created")

# 2. Add Customer via POST /customers
r = client.post("/customers", data={
    "name": "Jane Doe Woodworking",
    "email": "jane@doe.com",
    "phone": "555-1234",
    "address": "123 Forest Rd",
    "notes": "Prefers premium walnut wood."
}, follow_redirects=False)
ok(r.status_code == 303 and r.headers["location"] == "/customers", "POST /customers redirects to /customers")

cust = con.execute("SELECT * FROM customers WHERE name='Jane Doe Woodworking'").fetchone()
ok(cust is not None, "Customer inserted in database")
ok(cust["email"] == "jane@doe.com" and cust["phone"] == "555-1234", "Customer contact info matches")
ok(cust["notes"] == "Prefers premium walnut wood.", "Customer initial notes match")

customer_id = cust["id"]

# 3. Update Customer via POST /customers/update
r = client.post("/customers/update", data={
    "customer_id": str(customer_id),
    "name": "Jane Doe Woodworking LLC",
    "email": "jane@doewoodworking.com",
    "phone": "555-4321",
    "address": "456 Timber Lane",
    "notes": "Prefers premium walnut and maple."
}, follow_redirects=False)
ok(r.status_code == 303 and r.headers["location"] == f"/customers/{customer_id}", "POST /customers/update redirects to detail page")

cust_updated = con.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
ok(cust_updated["name"] == "Jane Doe Woodworking LLC", "Customer name updated successfully")
ok(cust_updated["email"] == "jane@doewoodworking.com", "Customer email updated successfully")
ok(cust_updated["notes"] == "Prefers premium walnut and maple.", "Customer notes updated successfully")

# 4. Upload Customer File via POST /customers/{customer_id}/upload-file
mock_file_content = b"PDF Sales Tax Exempt Form Content"
mock_file = ("tax_exempt_form.pdf", io.BytesIO(mock_file_content))

r = client.post(
    f"/customers/{customer_id}/upload-file",
    files={"file": mock_file},
    data={"kind": "tax_form"},
    follow_redirects=False
)
ok(r.status_code == 303, "POST upload-file returns 303 Redirect")

file_row = con.execute("SELECT * FROM customer_files WHERE customer_id=?", (customer_id,)).fetchone()
ok(file_row is not None, "Uploaded file record created in DB")
ok(file_row["filename"] == "tax_exempt_form.pdf", "Uploaded filename matches")
ok(os.path.exists(file_row["path"]), "Uploaded file exists on disk")

file_id = file_row["id"]

# 5. Retrieve Customer File via GET /customers/file/{file_id}
r = client.get(f"/customers/file/{file_id}")
ok(r.status_code == 200, "GET /customers/file/{file_id} returns 200 OK")
ok(r.content == mock_file_content, "Retrieved file contents match original upload")

# 6. Add Chronological Note via POST /customers/{customer_id}/add-note
r = client.post(f"/customers/{customer_id}/add-note", data={"note": "Delivered first batch of maple wood today."}, follow_redirects=False)
ok(r.status_code == 303, "POST add-note returns 303 Redirect")

note_row = con.execute("SELECT * FROM customer_notes WHERE customer_id=?", (customer_id,)).fetchone()
ok(note_row is not None, "Customer note created in DB")
ok(note_row["note"] == "Delivered first batch of maple wood today.", "Note content matches")

note_id = note_row["id"]

# 7. Delete Note via POST /customers/note/{note_id}/delete
r = client.post(f"/customers/note/{note_id}/delete", follow_redirects=False)
ok(r.status_code == 303, "POST note delete returns 303 Redirect")
note_check = con.execute("SELECT * FROM customer_notes WHERE id=?", (note_id,)).fetchone()
ok(note_check is None, "Note successfully deleted from database")

# 8. Create Invoices/Memos and Verify Calculations
# Let's post an invoice for this customer
sales_acct = con.execute("SELECT id FROM accounts WHERE name='Sales - square' OR name='Sales - Square'").fetchone()["id"]
checking_acct = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]

# Create an invoice in DB
con.execute(
    "INSERT INTO invoices(number, customer_id, date, due_date, status, kind) VALUES(?,?,?,?,?,?)",
    ("INV-TEST-100", customer_id, "2026-07-01", "2026-07-31", "sent", "invoice")
)
inv = con.execute("SELECT id FROM invoices WHERE number='INV-TEST-100'").fetchone()
inv_id = inv["id"]

# Add items to invoice
con.execute("INSERT INTO invoice_items(invoice_id, description, qty, unit_cents) VALUES(?,?,?,?)", (inv_id, "Planing Work", 10, 5000)) # $500 total
con.commit()

# Verify balance calculations
tot = invoicing.invoice_total(con, inv_id)
ok(tot == 50000, f"Invoice total is $500.00 ({tot})")

bal = invoicing.invoice_outstanding_balance(con, inv_id)
ok(bal == 50000, f"Outstanding balance is $500.00 ({bal})")

# 9. Verify Customer Statement/Report Page returns 200
r = client.get(f"/customers/{customer_id}/report")
ok(r.status_code == 200, "GET /customers/{customer_id}/report returns 200 OK")
ok("Customer Statement" in r.text, "Report page body contains 'Customer Statement'")
ok("Jane Doe Woodworking LLC" in r.text, "Report page contains customer name")
ok("INV-TEST-100" in r.text, "Report page contains invoice number")

# 10. Delete Customer File via POST /customers/file/{file_id}/delete
r = client.post(f"/customers/file/{file_id}/delete", follow_redirects=False)
ok(r.status_code == 303, "POST delete-file returns 303 Redirect")
file_check = con.execute("SELECT * FROM customer_files WHERE id=?", (file_id,)).fetchone()
ok(file_check is None, "File record successfully deleted from DB")
ok(not os.path.exists(file_row["path"]), "Uploaded file deleted from disk")

# Cleanup
con.close()
import shutil
shutil.rmtree(TMP, ignore_errors=True)
print("\nCUSTOMERS MANAGEMENT TESTS DONE")
