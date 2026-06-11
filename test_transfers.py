"""Credit-card-payment transfer matching. Isolated via SHOPBOOKS_DATA_DIR."""
import io
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_xfer_")
import db  # noqa: E402
import ledger  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ok = lambda c, w: print(("PASS" if c else "FAIL"), w)
client = TestClient(appmod.app)

con = db.connect()
chk = con.execute("SELECT id FROM accounts WHERE name='Business Checking'").fetchone()["id"]
card = con.execute("SELECT id FROM accounts WHERE name='Credit Card 1'").fetchone()["id"]
mats = con.execute("SELECT id FROM accounts WHERE name='Materials & Supplies'").fetchone()["id"]
con.close()

# bank CSV: -500 single-amount => staged +50000 (money out = withdrawal/CC payment)
bank_csv = "Date,Description,Amount\n02/10/2026,PAYMENT TO CARD,-500.00\n02/11/2026,HOME DEPOT,-84.37\n"
# card CSV: +500 => staged -50000 (money in = payment received); plus a normal charge
card_csv = "Date,Description,Amount\n02/08/2026,PAYMENT THANK YOU,500.00\n02/09/2026,MCMASTER,-40.00\n"


def import_csv(name, data, acct):
    client.post("/import", files={"file": (name, io.BytesIO(data.encode()), "text/csv")}, data={"account_id": str(acct)})


# ---- Scenario A: both statements pending at once ----
import_csv("card.csv", card_csv, card)
import_csv("bank.csv", bank_csv, chk)

con = db.connect()
rows = {r["description"]: r for r in con.execute("SELECT * FROM staged WHERE status='pending'")}
# the two payment sides should be auto-categorized as transfers pointing at each other's account
ok(rows["PAYMENT TO CARD"]["category_id"] == card, "bank-side payment auto-categorized -> the card")
ok(rows["PAYMENT THANK YOU"]["category_id"] == chk, "card-side payment auto-categorized -> checking")
# the unrelated charge ($40 MCMASTER) must NOT be paired as a transfer
ok(rows["MCMASTER"]["category_id"] in (None, mats) and
   con.execute("SELECT kind FROM accounts WHERE id=?", (rows["MCMASTER"]["category_id"] or mats,)).fetchone()["kind"] != "bank",
   "unrelated charge not mistaken for a transfer")
con.close()

# post everything; the transfer must book exactly once
form = {"post_all": "1"}
con = db.connect()
for s in con.execute("SELECT * FROM staged WHERE status='pending'"):
    form[f"cat_{s['id']}"] = str(s["category_id"] or mats)
con.close()
client.post("/review", data=form)

con = db.connect()
# how many transfer entries (both legs are own accounts) got posted?
xfer = con.execute(
    "SELECT COUNT(*) c FROM entries e WHERE (SELECT COUNT(*) FROM splits s JOIN accounts a ON a.id=s.account_id "
    "WHERE s.entry_id=e.id AND a.kind IN ('bank','card'))=2").fetchone()["c"]
ok(xfer == 1, f"transfer booked exactly once (got {xfer})")
chk_bal = ledger.display_balance("asset", ledger.raw_balance(con, chk))
card_bal = ledger.display_balance("liability", ledger.raw_balance(con, card))
# checking: -500 (payment) -84.37 (home depot) = -584.37 ; card: +500 payment reduces, +40 charge = -460
ok(chk_bal == -58437, f"checking balance correct after one transfer (got {chk_bal})")
ok(card_bal == -46000, f"card balance correct, payment counted once (got {card_bal})")
bad = con.execute("SELECT entry_id, SUM(amount_cents) t FROM splits GROUP BY entry_id HAVING t!=0").fetchall()
ok(not bad, "all entries balance (zero-sum)")
con.close()
print("PASS scenario A (both pending) books the transfer once")

# ---- Scenario B: cross-import (card posted first, bank imported later) ----
con = db.connect()
for t in ("staged", "batches", "splits", "entries"):
    con.execute(f"DELETE FROM {t}")
con.commit(); con.close()

import_csv("card2.csv", "Date,Description,Amount\n03/05/2026,PAYMENT THANK YOU,500.00\n", card)
con = db.connect(); sid = con.execute("SELECT id FROM staged WHERE status='pending'").fetchone()["id"]; con.close()
client.post("/review", data={f"cat_{sid}": str(chk), "post_one": str(sid)})  # post the card payment as a transfer

import_csv("bank2.csv", "Date,Description,Amount\n03/06/2026,PAYMENT TO CARD,-500.00\n", chk)
con = db.connect()
brow = con.execute("SELECT * FROM staged WHERE status='pending'").fetchone()
ok(brow["category_id"] == card, "later bank side auto-categorized to the card (already-booked transfer)")
ok(appmod.importer.find_posted_transfer(con, chk, brow["amount_cents"], brow["date"]) is not None,
   "bank side detected as already-recorded transfer")
con.close()
# try to post it -> must be skipped, not double-booked
client.post("/review", data={f"cat_{brow['id']}": str(card), "post_one": str(brow['id'])})
con = db.connect()
status = con.execute("SELECT status FROM staged WHERE id=?", (brow["id"],)).fetchone()["status"]
n_entries = con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
chk_bal = ledger.display_balance("asset", ledger.raw_balance(con, chk))
con.close()
ok(status == "skipped", "second side was auto-skipped on post")
ok(n_entries == 1 and chk_bal == -50000, "transfer still booked once; no double count")
print("PASS scenario B (cross-import) skips the second side")

print("\nTRANSFER TESTS DONE")
