"""Tests for Schedule C mapping and Estimated Quarterly Payments.
Isolated via SHOPBOOKS_DATA_DIR.
"""
import os
import tempfile
import io
import zipfile
import csv
from pathlib import Path
from urllib.parse import unquote

# Set temp directory for data isolation BEFORE importing db/app
TMP = Path(tempfile.mkdtemp(prefix="shopbooks_taxes_c_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db  # noqa: E402
import ledger  # noqa: E402
import insights  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)
client = TestClient(appmod.app)

# Initialize database
db.init()

con = db.connect()

# 1. Verify schema column schedule_c_line exists
columns = {r["name"] for r in con.execute("PRAGMA table_info(accounts)").fetchall()}
ok("schedule_c_line" in columns, "accounts table has schedule_c_line column")

# 2. Map some seeded accounts
checking = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
sales = con.execute("SELECT id FROM accounts WHERE name='Sales - Square'").fetchone()["id"]
adv = con.execute("SELECT id FROM accounts WHERE name='Advertising & Marketing'").fetchone()["id"]
supplies = con.execute("SELECT id FROM accounts WHERE name='Materials & Supplies'").fetchone()["id"]
postage = con.execute("SELECT id FROM accounts WHERE name='Shipping & Postage'").fetchone()["id"]

# Verify that accounts are mapped via POST /accounts/schedule_c
r = client.post("/accounts/schedule_c", data={"account_id": str(sales), "schedule_c_line": "Gross receipts or sales (Line 1)"}, follow_redirects=False)
ok(r.status_code == 303, "POST /accounts/schedule_c for Sales returns 303 Redirect")

r = client.post("/accounts/schedule_c", data={"account_id": str(adv), "schedule_c_line": "Advertising (Line 8)"}, follow_redirects=False)
ok(r.status_code == 303, "POST /accounts/schedule_c for Advertising returns 303 Redirect")

r = client.post("/accounts/schedule_c", data={"account_id": str(supplies), "schedule_c_line": "Supplies (not included in Part III) (Line 22)"}, follow_redirects=False)
ok(r.status_code == 303, "POST /accounts/schedule_c for Supplies returns 303 Redirect")

# Post some transactions to verify calculations
# Q1: Jan 15 - Net profit of $10,000 (Sales: $12,000, Adv: -$2,000)
# Splits: debits positive, credits negative.
# Entry 1: Checking (+12000), Sales (-12000)
ledger.post_entry(con, "2026-01-15", "Customer Square Payment", [(checking, 1200000), (sales, -1200000)])
# Entry 2: Adv (+2000), Checking (-2000)
ledger.post_entry(con, "2026-02-15", "Google Ads", [(adv, 200000), (checking, -200000)])

# Q2: Apr 20 - Net profit of $5,000 (Sales: $8,000, Supplies: -$3,000)
# Entry 3: Checking (+8000), Sales (-8000)
ledger.post_entry(con, "2026-04-20", "Customer Payment", [(checking, 800000), (sales, -800000)])
# Entry 4: Supplies (+3000), Checking (-3000)
ledger.post_entry(con, "2026-05-15", "Supplies Depot", [(supplies, 300000), (checking, -300000)])

# Let's post something in Q3 to an UNMAPPED account to check unmapped flagging
# Entry 5: Postage (+1000), Checking (-1000)
ledger.post_entry(con, "2026-07-10", "USPS Postage", [(postage, 100000), (checking, -100000)])

con.commit()

# 3. Test schedule_c_report calculation
rep = insights.schedule_c_report(con, "2026-01-01", "2026-12-31")

# Total Income should be Sales = $20,000
ok(rep["total_income"] == 2000000, f"schedule_c_report total income is $20,000 ({rep['total_income']})")
# Total Expenses should be Adv ($2,000) + Supplies ($3,000) = $5,000
ok(rep["total_expenses"] == 500000, f"schedule_c_report total expenses is $5,000 ({rep['total_expenses']})")
ok(rep["net"] == 1500000, f"schedule_c_report net is $15,000 ({rep['net']})")

# Unmapped checklist should contain "Shipping & Postage" with balance $1,000
unmapped_names = {a["name"] for a in rep["unmapped"]}
ok("Shipping & Postage" in unmapped_names, "Shipping & Postage is flagged as unmapped")
postage_item = [a for a in rep["unmapped"] if a["name"] == "Shipping & Postage"][0]
ok(postage_item["balance"] == 100000, f"Shipping & Postage unmapped balance is $1,000 ({postage_item['balance']})")

# 4. Test estimated_taxes calculation
est = insights.estimated_taxes(con, 2026, 15.0)

q1 = [q for q in est["quarters"] if q["quarter"] == "Q1"][0]
ok(q1["net_profit"] == 1000000, f"Q1 net profit is $10,000 ({q1['net_profit']})")
ok(q1["se_tax"] == 141296, f"Q1 SE tax is $1,412.96 ({q1['se_tax']})")
ok(q1["income_tax"] == 150000, f"Q1 Income tax is $1,500.00 ({q1['income_tax']})")
ok(q1["total_due"] == 291296, f"Q1 total due is $2,912.96 ({q1['total_due']})")

q2 = [q for q in est["quarters"] if q["quarter"] == "Q2"][0]
ok(q2["net_profit"] == 500000, f"Q2 net profit is $5,000 ({q2['net_profit']})")
ok(q2["se_tax"] == 70648, f"Q2 SE tax is $706.48 ({q2['se_tax']})")
ok(q2["income_tax"] == 75000, f"Q2 Income tax is $750.00 ({q2['income_tax']})")
ok(q2["total_due"] == 145648, f"Q2 total due is $1,456.48 ({q2['total_due']})")

# Q3 net profit should be -$1,000 (USPS postage expense)
# SE and Income tax should be 0 because it's a loss
q3 = [q for q in est["quarters"] if q["quarter"] == "Q3"][0]
ok(q3["net_profit"] == -100000, f"Q3 net profit is -$1,000 ({q3['net_profit']})")
ok(q3["se_tax"] == 0, f"Q3 SE tax is 0 ({q3['se_tax']})")
ok(q3["income_tax"] == 0, f"Q3 Income tax is 0 ({q3['income_tax']})")
ok(q3["total_due"] == 0, f"Q3 total due is 0 ({q3['total_due']})")

# 5. Test settings rate change via POST /taxes/settings
r = client.post("/taxes/settings", data={"estimated_income_tax_rate": "20.0"}, follow_redirects=False)
ok(r.status_code == 303, "POST /taxes/settings returns 303")
# Verify setting updated in DB
rate_db = db.get_setting(con, "estimated_income_tax_rate")
ok(rate_db == "20.0", f"Tax rate updated in settings table to 20.0 ({rate_db})")

# 6. Verify zip contains schedule_c.csv
r = client.get("/taxes/package.zip?year=2026")
ok(r.status_code == 200, "GET /taxes/package.zip returns 200 OK")
z = zipfile.ZipFile(io.BytesIO(r.content))
ok("2026_schedule_c.csv" in z.namelist(), "ZIP contains 2026_schedule_c.csv")

# Read and verify CSV content
csv_content = z.read("2026_schedule_c.csv").decode("utf-8")
rows_csv = list(csv.reader(io.StringIO(csv_content)))

# Verify total lines and key fields
ok(any(r and "Gross receipts or sales (Line 1)" in r[0] for r in rows_csv), "Gross receipts or sales line found in CSV")
ok(any(r and "Total Schedule C Income" in r[0] and "20000.00" in r[1] for r in rows_csv), "Total Schedule C Income is correct in CSV")
ok(any(r and "Advertising (Line 8)" in r[0] for r in rows_csv), "Advertising line found in CSV")
ok(any(r and "Supplies (not included in Part III) (Line 22)" in r[0] for r in rows_csv), "Supplies line found in CSV")
ok(any(r and "Total Schedule C Expenses" in r[0] and "5000.00" in r[1] for r in rows_csv), "Total Schedule C Expenses is correct in CSV")
ok(any(r and "Net Schedule C Profit/Loss" in r[0] and "15000.00" in r[1] for r in rows_csv), "Net Schedule C Profit/Loss is correct in CSV")

# Cleanup
con.close()
import shutil
shutil.rmtree(TMP, ignore_errors=True)
print("\nTAXES SCHEDULE C TESTS DONE")
