"""Tests for the dashboard briefing (insights.briefing) — the 'what needs me today' assembly.
Deterministic, no AI: every figure/attention item comes from the ledger + invoicing + reconcile.
Isolated via SHOPBOOKS_DATA_DIR.
"""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_brieftest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db        # noqa: E402
import ledger    # noqa: E402
import insights  # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)
TODAY = "2026-06-15"

db.init()
con = db.connect()

# --- empty books: nothing to do ----------------------------------------------
b0 = insights.briefing(con, TODAY)
ok(b0["all_clear"] and b0["attention"] == [], "fresh books: all clear, no attention items")
ok(b0["cash_on_hand"] == 0 and b0["receivables_total"] == 0, "fresh books: zero cash and receivables")
ok(b0["next_tax"] is None, "fresh books: no upcoming estimated-tax amount (no profit)")

# --- seed a realistic mix -----------------------------------------------------
acct = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
CHK, SALES, MAT, UNCAT = (acct["Business Checking"], acct["Sales - Square"],
                          acct["Materials & Supplies"], acct["Uncategorized Expense"])
ledger.post_entry(con, "2026-02-01", "deposit", [(CHK, 100000), (SALES, -100000)])      # +$1000 cash
ledger.post_entry(con, "2026-03-01", "supplies", [(MAT, 20000), (CHK, -20000)])         # $200 expense, no receipt
ledger.post_entry(con, "2026-03-05", "mystery", [(UNCAT, 5000), (CHK, -5000)])          # $50 uncategorized, no receipt
con.execute("INSERT INTO customers(id,name,email) VALUES(1,'Acme','a@acme.test')")
con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,kind) "
            "VALUES('INV-2001',1,'2026-04-01','2026-04-30','sent','invoice')")
iid = con.execute("SELECT id FROM invoices WHERE number='INV-2001'").fetchone()["id"]
con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,?)", (iid, "job", 30000))
con.execute("INSERT INTO documents(filename,path,status) VALUES('loose.jpg','/x/loose.jpg','unmatched')")
con.commit()

b = insights.briefing(con, TODAY)

# --- key figures --------------------------------------------------------------
ok(b["cash_on_hand"] == 75000, "cash on hand = 1000 - 200 - 50 = $750")
ok(b["receivables_total"] == 30000 and b["overdue_count"] == 1, "AR = $300 outstanding, 1 overdue (due 4/30 < today)")
ok(b["receivables_overdue"] == 30000, "the overdue invoice total is surfaced")

# --- attention items ----------------------------------------------------------
texts = [a["text"] for a in b["attention"]]
levels = {a["text"]: a["level"] for a in b["attention"]}
ok(not b["all_clear"], "seeded books are NOT all clear")
overdue_item = next((t for t in texts if "overdue invoice" in t), None)
ok(overdue_item and levels[overdue_item] == "warn", "overdue invoices flagged as a warning")
ok(any("Uncategorized" in t for t in texts), "uncategorized entry flagged")
ok(any("missing a receipt" in t for t in texts), "expenses missing receipts flagged (the $200 + $50)")
ok(any("not matched" in t for t in texts), "the loose unmatched receipt flagged")
ok(all("href" in a for a in b["attention"]), "every attention item carries a link to act on it")

con.close()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nBRIEFING TESTS DONE")
