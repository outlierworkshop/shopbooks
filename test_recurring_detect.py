"""Tests for recurring auto-detection (recurring.detect_candidates) + the suggestions UI.

Deterministic, no AI: detection groups posted 2-split entries by normalized vendor
(importer.payee_key) + category + account, requires >= 3 occurrences on a regular
weekly/monthly/yearly cadence with a recent latest occurrence, and skips anything already
templated. Isolated via SHOPBOOKS_DATA_DIR before importing db.
"""
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_detecttest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db          # noqa: E402
import ledger      # noqa: E402
import recurring   # noqa: E402
import importer    # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed

TODAY = date.today()  # relative dates so the HTTP route (which uses real today) agrees
def ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


db.init()
con = db.connect()
acct = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
CHK, CARD, SALES, MAT, UNCAT = (acct["Business Checking"], acct["Credit Card 1"],
                                acct["Sales - Square"], acct["Materials & Supplies"],
                                acct["Uncategorized Expense"])


def expense(d, payee, cents, cat=MAT, acc=CHK):
    ledger.post_entry(con, d, payee, [(cat, cents), (acc, -cents)])


def income(d, payee, cents):
    ledger.post_entry(con, d, payee, [(CHK, cents), (SALES, -cents)])


# monthly rent: digits differ per line but payee_key groups them; one amount differs (median test)
expense(ago(62), "SHOP RENT 04", 120000)
expense(ago(31), "SHOP RENT 05", 121000)
expense(ago(1),  "SHOP RENT 06", 120000)
# weekly subscription on the card
for n, days in enumerate((21, 14, 7, 0)):
    expense(ago(days), f"SPOTIFY {n}", 1099, acc=CARD)
# monthly retainer (income)
for days in (60, 30, 0):
    income(ago(days), "ACME RETAINER", 80000)
# one-off + too-few + irregular + stale + uncategorized + transfer + already-templated
expense(ago(45), "HOME DEPOT 111", 8340)                                  # once
expense(ago(20), "GAS STATION 1", 4000); expense(ago(17), "GAS STATION 2", 4000)  # only twice
expense(ago(200), "AMAZON 1", 2500); expense(ago(150), "AMAZON 2", 2500); expense(ago(5), "AMAZON 3", 2500)  # irregular gaps
expense(ago(300), "OLD GYM 1", 3500); expense(ago(270), "OLD GYM 2", 3500); expense(ago(240), "OLD GYM 3", 3500)  # regular but dead
for days in (60, 30, 0):
    expense(ago(days), "MYSTERY CHARGE", 999, cat=UNCAT)                  # uncategorized: never suggested
for days in (60, 30, 0):
    ledger.post_entry(con, ago(days), "CARD AUTOPAY", [(CARD, 50000), (CHK, -50000)])  # transfer: no category leg
for days in (60, 30, 0):
    expense(ago(days), "ADOBE 99", 5999)
con.execute("INSERT INTO recurring(name,amount_cents,flow,account_id,category_id,frequency,next_date) "
            "VALUES('Adobe',5999,'expense',?,?,'monthly',?)", (CHK, MAT, ago(0)))
con.commit()

cands = {importer.payee_key(c["name"]): c for c in recurring.detect_candidates(con)}

# --- what IS detected ----------------------------------------------------------
ok(set(cands) == {"SHOP RENT", "SPOTIFY", "ACME RETAINER"},
   f"exactly the three real patterns are suggested (got {sorted(cands)})")
rent = cands["SHOP RENT"]
ok(rent["frequency"] == "monthly" and rent["flow"] == "expense", "rent: monthly expense")
ok(rent["amount_cents"] == 120000, "rent amount is the median occurrence ($1,200, not the $1,210 outlier)")
ok(rent["account_id"] == CHK and rent["category_id"] == MAT, "rent carries the right account + category")
ok(rent["occurrences"] == 3 and rent["last_date"] == ago(1), "rent: 3 occurrences, freshest last_date")
ok(rent["next_date"] == recurring.advance(ago(1), "monthly"), "rent next_date = one month after the last")
ok(cands["SPOTIFY"]["frequency"] == "weekly" and cands["SPOTIFY"]["account_id"] == CARD,
   "weekly card subscription detected on the card account")
ok(cands["ACME RETAINER"]["flow"] == "income" and cands["ACME RETAINER"]["frequency"] == "monthly",
   "monthly income retainer detected with income flow")

# --- what is NOT detected -------------------------------------------------------
for absent, why in (("HOME DEPOT", "a one-off"), ("GAS STATION", "only two occurrences"),
                    ("AMAZON", "irregular spacing"), ("OLD GYM", "a pattern that stopped"),
                    ("MYSTERY CHARGE", "Uncategorized Expense"), ("CARD AUTOPAY", "a transfer"),
                    ("ADOBE", "already templated")):
    ok(absent not in cands, f"{why} is not suggested ({absent})")

con.close()

# --- the suggestions UI: render + one-click create ------------------------------
from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402
c = TestClient(app.app)
page = c.get("/recurring")
ok(page.status_code == 200 and "Suggested from your history" in page.text,
   "/recurring renders the suggestions section")
ok("SHOP RENT 06" in page.text, "the rent suggestion appears on the page")

r = c.post("/recurring", data={
    "name": rent["name"], "amount": f"{rent['amount_cents'] / 100:.2f}", "flow": rent["flow"],
    "account_id": rent["account_id"], "category_id": rent["category_id"],
    "frequency": rent["frequency"], "next_date": rent["next_date"]}, follow_redirects=False)
ok(r.status_code == 303, "clicking Create on a suggestion posts to the existing /recurring route")

con = db.connect()
made = [t for t in recurring.list_all(con) if t["name"] == rent["name"]]
ok(len(made) == 1 and made[0]["amount_cents"] == 120000 and made[0]["frequency"] == "monthly",
   "the template was created from the suggestion")
still = {importer.payee_key(x["name"]) for x in recurring.detect_candidates(con)}
ok("SHOP RENT" not in still, "once created, the suggestion disappears")
ok({"SPOTIFY", "ACME RETAINER"} <= still, "the other suggestions remain")
con.close()

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nRECURRING DETECT TESTS DONE")
