"""Match invoices to existing deposits WITHOUT posting ledger entries. Isolated."""
import os
import tempfile
from urllib.parse import unquote

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_invmatch_")
import db  # noqa: E402
import ledger  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ok = lambda c, w: print(("PASS" if c else "FAIL"), w)
loc = lambda r: unquote(r.headers.get("location", ""))
client = TestClient(appmod.app)

con = db.connect()
chk = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
inc = con.execute("INSERT INTO accounts(name,type,kind) VALUES('Fabrication','income','category')").lastrowid
cust = con.execute("INSERT INTO customers(name) VALUES('Acme Corp')").lastrowid
# an imported invoice record (sent, no ledger entry), total 1,200.00
inv = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,memo) "
                  "VALUES('1042',?,?,?,'sent','Imported from QuickBooks')",
                  (cust, "2026-03-01", "2026-03-31")).lastrowid
con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,120000)",
            (inv, "Imported from QuickBooks (invoice total)"))
# the real deposit, already on the books from a statement import (income credit 1,200)
dep = ledger.post_entry(con, "2026-03-20", "ACH from Acme", [(chk, 120000), (inc, -120000)])
entries_before = con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
con.commit(); con.close()

# candidate should be found (income leg == -total, within window)
con = db.connect()
i, _, total = appmod.invoicing.get_invoice(con, inv)
cands = appmod.invoice_deposit_candidates(con, i, total)
con.close()
ok(len(cands) == 1 and cands[0]["id"] == dep, "the deposit is offered as a candidate")

# match -> paid + linked, but NO new ledger entry
r = client.post(f"/invoices/{inv}/match", data={"entry_id": str(dep)}, follow_redirects=False)
ok(r.status_code == 303, "match route ok")
con = db.connect()
row = con.execute("SELECT status, paid_date, matched_entry_id, paid_entry_id FROM invoices WHERE id=?", (inv,)).fetchone()
ok(row["status"] == "paid" and row["matched_entry_id"] == dep, "invoice now paid + linked to the deposit")
ok(row["paid_entry_id"] is None, "no posted entry owns this (matched, not recorded)")
ok(row["paid_date"] == "2026-03-20", "paid_date taken from the deposit")
ok(con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == entries_before, "NO new ledger entry created")
con.close()

# the same deposit is no longer offered to other invoices
con = db.connect()
i, _, total = appmod.invoicing.get_invoice(con, inv)
con.close()
# unmatch -> back to sent, link cleared, deposit STILL on the books
r = client.post(f"/invoices/{inv}/unmatch", follow_redirects=False)
con = db.connect()
row = con.execute("SELECT status, matched_entry_id FROM invoices WHERE id=?", (inv,)).fetchone()
ok(row["status"] == "sent" and row["matched_entry_id"] is None, "unmatch reverts to sent and clears the link")
ok(con.execute("SELECT 1 FROM entries WHERE id=?", (dep,)).fetchone() is not None, "unmatch did NOT delete the deposit")
con.close()

# auto-match-all links it again (exactly one candidate)
r = client.post("/invoices/match-all", follow_redirects=False)
ok("Matched 1" in loc(r), "match-all links the invoice")
con = db.connect()
ok(con.execute("SELECT matched_entry_id FROM invoices WHERE id=?", (inv,)).fetchone()["matched_entry_id"] == dep,
   "match-all linked to the right deposit")
ok(con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == entries_before, "still no extra entries")

# deleting the deposit unlinks the invoice (doesn't orphan), reverts to sent
ledger.delete_entry(con, dep); con.commit()
row = con.execute("SELECT status, matched_entry_id FROM invoices WHERE id=?", (inv,)).fetchone()
ok(row["matched_entry_id"] is None and row["status"] == "sent", "deleting the deposit cleanly unlinks the invoice")
con.close()

import shutil  # noqa: E402
shutil.rmtree(os.environ["SHOPBOOKS_DATA_DIR"], ignore_errors=True)
print("\nINVOICE-MATCH TESTS DONE")
