"""Tests for bank feeds via SimpleFIN (feeds.py + routes). Isolated via SHOPBOOKS_DATA_DIR.

ZERO network: feeds' HTTP layer (_http_post / _http_get_json) is monkeypatched with canned
protocol-shaped payloads. Verifies the claim flow, sign mapping (balance-perspective -> staged
positive-is-money-out), pending exclusion, both dedupe layers, unmapped/disabled handling, and that
staged rows ride the normal Review pipeline (rules categorization, batch per account).
"""
import base64
import os
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_feedtest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db     # noqa: E402
import feeds  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed

db.init()
con = db.connect()
acct = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
CHK, CARD, MAT = acct["Business Checking"], acct["Credit Card 1"], acct["Materials & Supplies"]
con.execute("INSERT INTO rules(pattern, account_id) VALUES('SPINDLE', ?)", (MAT,))  # rules categorization
con.commit()


def ts(days_ago):
    d = date.today() - timedelta(days=days_ago)
    return int(datetime.combine(d, time.min, tzinfo=timezone.utc).timestamp())


def iso(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


ACCESS = "https://u:p@bridge.example/simplefin"
PAYLOAD = {"errors": [], "accounts": [
    {"id": "act-chk", "name": "Biz Checking *1234", "org": {"name": "Eastern Bank"},
     "transactions": [
         {"id": "t1", "posted": ts(5), "amount": "1500.00", "description": "SQUARE DEPOSIT"},      # money in
         {"id": "t2", "posted": ts(3), "amount": "-42.50", "description": "SPINDLE SUPPLY CO 88"},  # money out, rule hits
         {"id": "t3", "posted": ts(1), "amount": "-10.00", "description": "PENDING HOLD", "pending": True},
     ]},
    {"id": "act-card", "name": "Ink *9876", "org": {"name": "Chase"},
     "transactions": [
         {"id": "t4", "posted": ts(2), "amount": "-99.99", "description": "TOOL VENDOR"},           # card charge
     ]},
    {"id": "act-new", "name": "Spark *1111", "org": {"name": "Capital One"}, "transactions": [
         {"id": "t5", "posted": ts(2), "amount": "-5.00", "description": "SHOULD NOT STAGE"},
     ]},
]}

posts, gets = [], []
feeds._http_post = lambda url: (posts.append(url), ACCESS)[1]
feeds._http_get_json = lambda url, params: (gets.append((url, dict(params))), PAYLOAD)[1]

# --- claim ---------------------------------------------------------------------
token = base64.b64encode(b"https://bridge.example/claim/abc123").decode()
ok(feeds.claim_setup_token(token) == ACCESS, "a valid setup token claims to the access URL")
ok(posts == ["https://bridge.example/claim/abc123"], "claim POSTs the decoded claim URL exactly once")
for bad, why in (("not base64!!", "garbage"), (base64.b64encode(b"hello world").decode(), "non-URL")):
    try:
        feeds.claim_setup_token(bad)
        ok(False, f"{why} token raises")
    except ValueError:
        ok(True, f"{why} token raises a readable error")

db.set_setting(con, "simplefin_access_url", ACCESS)
con.commit()
ok(feeds.connected(con), "connected() true once the access URL is stored")

# --- first fetch: mapping, signs, pending, unmapped ------------------------------
feeds.refresh_accounts(con)
con.commit()
ok(con.execute("SELECT COUNT(*) c FROM feed_accounts").fetchone()["c"] == 3,
   "refresh_accounts lists all three bridge accounts")
con.execute("UPDATE feed_accounts SET account_id=? WHERE id='act-chk'", (CHK,))
con.execute("UPDATE feed_accounts SET account_id=? WHERE id='act-card'", (CARD,))
con.commit()

r = feeds.fetch(con)
con.commit()
ok(r["staged"] == 3, f"three posted transactions staged (got {r['staged']})")
ok(r["unmapped"] == ["Capital One Spark *1111"], "the unmapped account is reported, not staged")

rows = {s["description"]: s for s in con.execute(
    "SELECT s.*, b.account_id acct FROM staged s JOIN batches b ON b.id=s.batch_id").fetchall()}
ok(rows["SQUARE DEPOSIT"]["amount_cents"] == -150000 and rows["SQUARE DEPOSIT"]["acct"] == CHK,
   "a deposit lands as money-in (negative) on the checking batch")
ok(rows["SPINDLE SUPPLY CO 88"]["amount_cents"] == 4250, "a checking debit lands as money-out (positive)")
ok(rows["SPINDLE SUPPLY CO 88"]["category_id"] == MAT, "rules categorization applied on the way in")
ok(rows["TOOL VENDOR"]["amount_cents"] == 9999 and rows["TOOL VENDOR"]["acct"] == CARD,
   "a card charge lands as money-out (positive) on the card's own batch")
ok("PENDING HOLD" not in rows, "pending transactions are excluded")
ok("SHOULD NOT STAGE" not in rows, "unmapped account's transactions are not staged")
batches = con.execute("SELECT filename, account_id FROM batches").fetchall()
ok(len(batches) == 2 and all(b["filename"].startswith("feed:") for b in batches),
   "one feed batch per mapped account")
ok(all(s["status"] == "pending" for s in rows.values()), "everything lands PENDING in Review (nothing posts)")
ls = con.execute("SELECT last_synced FROM feed_accounts WHERE id='act-chk'").fetchone()["last_synced"]
ok(ls == date.today().isoformat(), "last_synced stamped after a fetch")

# --- second fetch: feed-id dedupe -------------------------------------------------
r2 = feeds.fetch(con)
con.commit()
ok(r2["staged"] == 0 and r2["skipped"] >= 3, "refetching the same window stages nothing (feed_txns dedupe)")

# --- cross-source dedupe: a statement twin blocks the same date+amount ------------
PAYLOAD["accounts"][0]["transactions"].append(
    {"id": "t9", "posted": ts(4), "amount": "-77.00", "description": "FEED COPY OF STATEMENT TXN"})
cur = con.execute("INSERT INTO batches(filename,account_id) VALUES('stmt.csv',?)", (CHK,))
con.execute("INSERT INTO staged(batch_id,date,description,amount_cents) VALUES(?,?,?,?)",
            (cur.lastrowid, iso(4), "STATEMENT VERSION", 7700))
con.commit()
r3 = feeds.fetch(con)
con.commit()
ok(r3["staged"] == 0, "a feed txn matching an existing staged date+amount on the account is skipped")
ok(con.execute("SELECT 1 FROM feed_txns WHERE id='t9'").fetchone() is not None,
   "the cross-source skip is remembered (won't re-check forever)")

# --- disabled mapping is skipped ---------------------------------------------------
PAYLOAD["accounts"][1]["transactions"].append(
    {"id": "t10", "posted": ts(1), "amount": "-1.00", "description": "WHILE DISABLED"})
con.execute("UPDATE feed_accounts SET enabled=0 WHERE id='act-card'")
con.commit()
r4 = feeds.fetch(con)
con.commit()
ok(r4["staged"] == 0 and any("Ink" in u for u in r4["unmapped"]),
   "a disabled mapping is skipped and reported")
con.execute("UPDATE feed_accounts SET enabled=1 WHERE id='act-card'")
con.commit()

# --- fetch while not connected raises readably ------------------------------------
db.set_setting(con, "simplefin_access_url", "")
con.commit()
try:
    feeds.fetch(con)
    ok(False, "fetch without a connection raises")
except ValueError as e:
    ok("Settings" in str(e), "fetch without a connection raises a readable error")
db.set_setting(con, "simplefin_access_url", ACCESS)
con.commit()
con.close()

# --- HTTP routes -------------------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402
c = TestClient(app.app)

page = c.get("/settings")
ok(page.status_code == 200 and "Bank feeds (SimpleFIN)" in page.text, "settings shows the feeds section")
ok("Fetch from bank feeds" in page.text and "Eastern Bank" in page.text,
   "connected view lists the mapped feed accounts")
ok("Fetch from bank feeds" in c.get("/import").text, "import page offers the fetch button when connected")

r = c.post("/feeds/fetch", follow_redirects=False)
ok(r.status_code == 303, "POST /feeds/fetch redirects")
r = c.post("/feeds/map", data={"feed_account_id": "act-card", "account_id": str(CARD), "enabled": "1"},
           follow_redirects=False)
ok(r.status_code == 303, "POST /feeds/map redirects")
r = c.post("/feeds/disconnect", follow_redirects=False)
ok(r.status_code == 303, "disconnect redirects")
con = db.connect()
ok(db.get_setting(con, "simplefin_access_url", "") == "", "disconnect clears the access URL")
ok(con.execute("SELECT COUNT(*) c FROM feed_accounts").fetchone()["c"] == 3, "mappings are kept on disconnect")
con.close()
page2 = c.get("/settings")
ok("paste setup token" in page2.text, "after disconnect, settings shows the connect form again")

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nFEEDS TESTS DONE")
