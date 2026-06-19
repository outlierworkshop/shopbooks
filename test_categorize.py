"""Tests for smart categorization (issue #3): learning from the user's own history.

Isolation pattern: SHOPBOOKS_DATA_DIR -> temp dir BEFORE importing db. Exercises the
deterministic pieces (no network/AI): payee normalization, the history map, the
rules > history > AI precedence in staging, and that history feeds the AI prompt.
"""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_cattest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db        # noqa: E402
import ledger    # noqa: E402
import importer  # noqa: E402
import ai        # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)

db.init()
con = db.connect()
acct = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
CHK, MAT, SHIP, SOFT, OFFICE, UNCAT = (acct["Business Checking"], acct["Materials & Supplies"],
                                       acct["Shipping & Postage"], acct["Software & Subscriptions"],
                                       acct["Office Supplies"], acct["Uncategorized Expense"])


def spend(payee, cat, dollars):
    ledger.post_entry(con, "2026-01-01", payee, [(cat, dollars * 100), (CHK, -dollars * 100)])


# Past confirmed history: Home Depot -> Materials (x2), USPS -> Shipping, one uncategorized.
spend("HOME DEPOT #123", MAT, 50)
spend("HOME DEPOT #777", MAT, 30)
spend("USPS 456", SHIP, 8)
spend("MYSTERY LLC", UNCAT, 10)
con.commit()

# --- payee_key normalization -------------------------------------------------
ok(importer.payee_key("HOME DEPOT #123") == "HOME DEPOT", "payee_key strips store numbers")
ok(importer.payee_key("HOME DEPOT #123") == importer.payee_key("HOME DEPOT #777"),
   "same vendor with different trailing ids collapses to one key")

# --- history map / lookup ----------------------------------------------------
h = importer.history_map(con)
ok(importer.history_category(con, "HOME DEPOT #999", h) == MAT, "learns Home Depot -> Materials")
ok(importer.history_category(con, "USPS 0001", h) == SHIP, "learns USPS -> Shipping")
ok(importer.history_category(con, "BRAND NEW VENDOR", h) is None, "unknown vendor: no history")
ok(importer.payee_key("MYSTERY LLC") not in h, "Uncategorized history is not learned")

# most-common category wins even if a one-off contradicts it
spend("HOME DEPOT #888", SOFT, 2)
con.commit()
ok(importer.history_category(con, "HOME DEPOT #1") == MAT, "the majority category wins")

# --- staging precedence: rules > history > AI --------------------------------
# Use made-up vendors (not in the seed rules) so we test history, not a seeded rule.
spend("ACME LUMBER 1", MAT, 40)
spend("ACME LUMBER 2", MAT, 40)   # ACME -> Materials in history
spend("ZETA POST 1", SHIP, 5)     # ZETA -> Shipping in history
con.execute("INSERT INTO rules(pattern, account_id) VALUES('ACME', ?)", (SOFT,))  # a rule should override ACME history
con.commit()
cats = {r["id"]: r["name"] for r in con.execute(
    "SELECT id, name FROM accounts WHERE active=1 AND type IN ('income','expense')").fetchall()}
bid = con.execute("INSERT INTO batches(filename, account_id) VALUES('t', ?)", (CHK,)).lastrowid
txns = [{"date": "2026-02-01", "description": "ACME LUMBER 9", "amount_cents": 500},  # rule -> SOFT
        {"date": "2026-02-02", "description": "ZETA POST 9", "amount_cents": 700},     # history -> SHIP
        {"date": "2026-02-03", "description": "WIDGET CO", "amount_cents": 900}]        # AI -> Office Supplies
importer.stage_transactions(con, bid, txns, CHK, cats, ai_categories=[None, None, "Office Supplies"])
got = con.execute("SELECT description, category_id FROM staged WHERE batch_id=? ORDER BY id", (bid,)).fetchall()
ok(got[0]["category_id"] == SOFT, "rule beats history (ACME rule wins over ACME history)")
ok(got[1]["category_id"] == SHIP, "history fills when no rule matches")
ok(got[2]["category_id"] == OFFICE, "AI fills when neither rule nor history matches")

# --- history feeds the AI prompt as few-shot ---------------------------------
ex = ai._history_examples(con)
ok(any(k == "HOME DEPOT" for k, _ in ex), "history examples include the learned vendor")
prompt = ai._categorize_prompt([{"description": "x", "amount": 100}], ["Materials & Supplies"], ex)
ok("HOME DEPOT -> Materials & Supplies" in prompt, "the prompt embeds the user's history as guidance")

# --- categorize_model setting (cheaper model for high-volume) -----------------
ok(ai._claude_model(con, "categorize") == ai._claude_model(con), "blank categorize_model falls back to ai_model")
db.set_setting(con, "categorize_model", "claude-haiku-4-5-20251001")
con.commit()
ok(ai._claude_model(con, "categorize") == "claude-haiku-4-5-20251001", "categorize_model overrides for categorization")
ok(ai._claude_model(con) == "claude-opus-4-8", "other tasks still use the main model")

con.close()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nCATEGORIZE TESTS DONE")
