"""Tests for the assistant's tool layer (chat.py) — no network, no API key needed.

Same isolation pattern as test_insights: point SHOPBOOKS_DATA_DIR at a temp dir BEFORE importing
db. We also clear ANTHROPIC_API_KEY so the AI-off path is deterministic. We exercise the tool
dispatch and the cents->dollars conversion against a known seed; we never call the Anthropic API
(the tool layer is the deterministic part — the model only narrates it).
"""
import os
import tempfile
from datetime import date
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_chattest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)
os.environ.pop("ANTHROPIC_API_KEY", None)  # force the AI-off path

import db        # noqa: E402
import ledger    # noqa: E402
import chat      # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)

db.init()
con = db.connect()
acct = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
CHK, SALES, MAT, CARD = (acct["Business Checking"], acct["Sales - Square"],
                         acct["Materials & Supplies"], acct["Credit Card 1"])

# 2026: income $1500, materials $500 across two expenses (one $300, one $200, both on checking).
ledger.post_entry(con, "2026-01-10", "income", [(CHK, 100000), (SALES, -100000)])
ledger.post_entry(con, "2026-01-15", "expense", [(MAT, 30000), (CHK, -30000)])
ledger.post_entry(con, "2026-02-10", "income", [(CHK, 50000), (SALES, -50000)])
ledger.post_entry(con, "2026-02-15", "expense", [(MAT, 20000), (CHK, -20000)])
con.commit()

TODAY = date(2026, 12, 31)

# --- _to_dollars: cents -> dollars on money keys, leaves the rest alone -------
d = chat._to_dollars({"net": 95000, "hours": 12.5, "count": 3, "name": "x", "tidy": True,
                      "income": {"current": 150000, "pct_change": 87.5}})
ok(d["net"] == 950.0, "money key converted to dollars")
ok(d["hours"] == 12.5 and d["count"] == 3, "non-money numbers left untouched")
ok(d["name"] == "x" and d["tidy"] is True, "strings and bools left untouched")
ok(d["income"]["current"] == 1500.0 and d["income"]["pct_change"] == 87.5,
   "nested money dict recursed (current converted, pct_change kept)")
ok(chat._to_dollars([{"amount": 30000}])[0]["amount"] == 300.0, "lists handled")

# --- tool registry is well-formed --------------------------------------------
tool_names = {t["name"] for t in chat.TOOLS}
ok(tool_names == set(chat._HANDLERS), "every advertised tool has a handler and vice versa")
ok(all(t["input_schema"]["additionalProperties"] is False for t in chat.TOOLS),
   "every tool schema sets additionalProperties:false")
ok(all(t["input_schema"]["type"] == "object" and isinstance(t["input_schema"]["required"], list)
       for t in chat.TOOLS), "every tool schema is an object with a required list")

# --- _run_tool returns real figures, in dollars, as JSON ---------------------
import json  # noqa: E402
pnl = json.loads(chat._run_tool(con, TODAY, "profit_and_loss", {"period": "2026"}))
ok(pnl["income_total"] == 1500.0 and pnl["expense_total"] == 500.0 and pnl["net"] == 1000.0,
   "profit_and_loss tool returns the right dollars")
ok(pnl["period"] == "2026", "tool echoes the resolved period label")

snap = json.loads(chat._run_tool(con, TODAY, "business_snapshot", {"period": "2026"}))
ok(snap["pnl"]["net"] == 1000.0 and "monthly_trend" in snap and "cash_position" in snap,
   "business_snapshot bundles P&L, trend, and cash (in dollars)")
ok(snap["cash_position"]["cash_on_hand"] == 1000.0, "cash on hand converted to dollars (1500 in - 500 out)")

miss = json.loads(chat._run_tool(con, TODAY, "missing_receipts", {"period": "2026", "min_amount": 2.5}))
ok(miss["count"] == 2 and all(r["amount"] >= 2.5 for r in miss["rows"]),
   "missing_receipts converts the min_amount dollars filter to cents")

# --- graceful errors ----------------------------------------------------------
bad = json.loads(chat._run_tool(con, TODAY, "profit_and_loss", {"period": "nonsense"}))
ok("error" in bad, "an unrecognized period comes back as a tool error, not a crash")
ok("error" in json.loads(chat._run_tool(con, TODAY, "no_such_tool", {})), "unknown tool name is an error")

# --- ask() is AI-optional -----------------------------------------------------
reply, err = chat.ask(con, [{"role": "user", "content": "how am I doing?"}])
ok(reply is None and err and "API key" in err, "with no key, ask() returns the AI-off message (no network call)")

con.close()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nCHAT TESTS DONE")
