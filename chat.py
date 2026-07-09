"""The ShopBooks assistant (#7): a conversational helper grounded in the real ledger.

Three jobs, in priority order:
  1. Explain how to USE ShopBooks (import, review, receipts, taxes, sync, ...).
  2. General TAX STRATEGY for a one-person workshop/sole proprietor.
  3. BUSINESS ANALYSIS on the owner's ACTUAL numbers.

The numbers always come from insights.py (and timetracking) via tools — Claude reads and
interprets them, it never invents or computes figures. That's the whole point: the ledger
stays deterministic and auditable no matter what the assistant says about it. The tool layer
converts integer cents to dollars so the model reports figures rather than doing arithmetic.

AI-optional, like everything else: with no Anthropic key set, ask() returns a friendly
"AI is off" message and the page explains how to turn it on.
"""
import json
from datetime import date

import ai
import db
import insights
import timetracking
from logutil import log

MAX_TOOL_ROUNDS = 8       # safety cap on the tool-use loop
MAX_TURNS = 24            # cap transcript length sent to the model

PERIOD_DESC = (
    "Time period. One of the relative words this-year, last-year, this-quarter, last-quarter, "
    "this-month, last-month, ytd; or an explicit 'YYYY' (e.g. 2025), 'YYYY-Qn' (e.g. 2025-Q2), "
    "or 'YYYY-MM' (e.g. 2025-03). Defaults to this-year."
)

# Keys whose integer values are money (cents). _to_dollars converts these to dollars so the
# model never has to divide by 100. Everything else (hours, counts, ids, dates) is left as-is.
MONEY_KEYS = {
    "income_total", "expense_total", "net", "amount", "balance", "cash_on_hand", "card_debt",
    "current", "previous", "delta", "income", "expenses", "billable_value", "net_cash",
}


def _to_dollars(obj):
    """Recursively convert known cents fields to dollars (float, 2dp). A money key whose value is
    a nested dict (e.g. compare()'s 'income') is recursed into, not converted."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, bool):
                out[k] = v
            elif isinstance(v, int) and k in MONEY_KEYS:
                out[k] = round(v / 100, 2)
            else:
                out[k] = _to_dollars(v)
        return out
    if isinstance(obj, list):
        return [_to_dollars(x) for x in obj]
    return obj


# ---------------------------------------------------------------- tool handlers
# Each takes (con, today, **input) and returns a plain dict of REAL figures from insights.py.

def _snapshot(con, today, period="this-year"):
    return insights.business_snapshot(con, period, today=today)


def _pnl(con, today, period="this-year"):
    s, e, label = insights.parse_period(period, today)
    return {"period": label, **insights.pnl_summary(con, s, e)}


def _compare(con, today, period="this-year", base="last-year"):
    return insights.compare(con, period, base, today=today)


def _trend(con, today, period="this-year"):
    s, e, label = insights.parse_period(period, today)
    return {"period": label, "months": insights.monthly_trend(con, s, e)}


def _expense_changes(con, today, period="this-year", base="last-year"):
    return insights.expense_changes(con, period, base, today=today)


def _cash(con, today):
    return insights.cash_position(con)  # balances as of now


def _health(con, today, period="this-year"):
    s, e, label = insights.parse_period(period, today)
    return {"period": label, **insights.bookkeeping_health(con, s, e)}


def _missing_receipts(con, today, period="this-year", min_amount=0):
    s, e, label = insights.parse_period(period, today)
    rows = insights.missing_receipts(con, s, e, int(round((min_amount or 0) * 100)))
    return {"period": label, "count": len(rows), "rows": rows[:50]}


def _jobs(con, today):
    rows = [j for j in timetracking.jobs_overview(con) if j["net_cash"] or j["hours"]]
    return {"jobs": rows}


_HANDLERS = {
    "business_snapshot": _snapshot, "profit_and_loss": _pnl, "compare_periods": _compare,
    "monthly_trend": _trend, "expense_changes": _expense_changes, "cash_position": _cash,
    "bookkeeping_health": _health, "missing_receipts": _missing_receipts, "jobs_overview": _jobs,
}


def _period_schema(extra=None):
    props = {"period": {"type": "string", "description": PERIOD_DESC}}
    if extra:
        props.update(extra)
    return {"type": "object", "properties": props, "required": [], "additionalProperties": False}


_NO_ARGS = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}

TOOLS = [
    {"name": "business_snapshot",
     "description": "Best one-call overview of how the business is doing in a period: profit & loss, "
                    "the month-by-month trend, current cash position, and any bookkeeping that needs "
                    "tidying. Start here for broad 'how are we doing?' questions.",
     "input_schema": _period_schema()},
    {"name": "profit_and_loss",
     "description": "Income, expenses, and net profit for a period, with a per-category breakdown "
                    "(largest first). Use for 'what did I make/spend' and 'where does the money go'.",
     "input_schema": _period_schema()},
    {"name": "compare_periods",
     "description": "Growth: income, expenses, and net profit for `period` versus a `base` period, each "
                    "with the dollar change and percent change. Use for 'am I growing?' / year-over-year.",
     "input_schema": _period_schema({"base": {"type": "string", "description": PERIOD_DESC + " The comparison/base period (default last-year)."}})},
    {"name": "monthly_trend",
     "description": "Income, expenses, and net for each calendar month in a period — the shape of the "
                    "year. Use for seasonality and 'which months were strong/weak'.",
     "input_schema": _period_schema()},
    {"name": "expense_changes",
     "description": "Per-expense-category totals this period vs a base period, sorted by biggest change "
                    "— the movers and outliers worth noticing. Use for 'what's costing more this year?'.",
     "input_schema": _period_schema({"base": {"type": "string", "description": PERIOD_DESC + " The comparison/base period (default last-year)."}})},
    {"name": "cash_position",
     "description": "Current bank balances (cash on hand) and credit-card balances owed, right now.",
     "input_schema": _NO_ARGS},
    {"name": "bookkeeping_health",
     "description": "What still needs attention before the books can be trusted for a period: "
                    "transactions awaiting review, entries left in Uncategorized Expense, and receipts "
                    "not yet matched. Use for 'are my books ready for taxes / clean?'.",
     "input_schema": _period_schema()},
    {"name": "missing_receipts",
     "description": "Posted expense transactions in a period with no receipt attached (the purchases "
                    "lacking documentation at tax time). Optional min_amount (dollars) to focus on bigger buys.",
     "input_schema": _period_schema({"min_amount": {"type": "number", "description": "Only include expenses at or above this dollar amount. Default 0 (all)."}})},
    {"name": "jobs_overview",
     "description": "Per-job profitability: hours logged, billable value, and net cash profit (income "
                    "minus expenses tagged to that job). Use for 'which jobs/customers are profitable?'.",
     "input_schema": _NO_ARGS},
]


# ---------------------------------------------------------------- system prompt

_HELP = (
    "HOW SHOPBOOKS WORKS (so you can answer 'how do I...' questions):\n"
    "- Import: upload bank/credit-card statements (PDF/CSV). With AI on, transactions are read out "
    "automatically; otherwise use the built-in parser. Imported transactions land in Review.\n"
    "- Review: confirm/categorize each imported transaction, then post it to the ledger. AI suggests "
    "categories; Rules and the user's own history refine them. Transfers between the owner's own "
    "accounts are linked, not counted as income/expense.\n"
    "- Receipts: upload images/PDFs (or scan a whole folder, or import an Amazon order CSV). They "
    "auto-match to expense transactions by amount + date. The Receipts → 'missing a receipt' report "
    "lists posted expenses with no documentation.\n"
    "- + Entry: add a transaction by hand. Invoices: track what customers owe. Mileage: log business "
    "miles for the standard-mileage deduction. Time: track hours against Jobs.\n"
    "- Reconcile: check an account against a statement's ending balance. Reports: P&L, balance sheet, "
    "and CSV exports. Insights: growth, trends, and an AI readout. Taxes: a per-year package (P&L, "
    "balance sheet, every transaction with receipt filenames, mileage, and the receipt images) as a ZIP "
    "to hand a tax preparer.\n"
    "- Accounts: the chart of accounts. Rules: keyword rules that auto-categorize. Settings: AI backend "
    "and key, cloud sync, and backups.\n"
    "- The books auto-save and back up on every close, and sync between the owner's Mac and PC through a "
    "shared cloud folder. Everything runs locally on the owner's machine."
)


def _system(con):
    name = db.get_setting(con, "business_name", "the business")
    return (
        f"You are the built-in assistant for ShopBooks, the bookkeeping app {name} uses. It is a "
        "one-person workshop / fabrication business (a sole proprietor). Today is "
        f"{date.today().isoformat()}.\n\n"
        "Your job is to help the owner in three ways:\n"
        "1. Explain how to use ShopBooks.\n"
        "2. Offer practical, general tax strategy for a one-person US business.\n"
        "3. Analyze the business using its REAL numbers.\n\n"
        "GROUND EVERY NUMBER IN THE TOOLS. For anything about this business's actual finances — income, "
        "expenses, profit, cash, growth, categories, jobs, receipts, what's clean — call a tool and answer "
        "from what it returns. Never guess, estimate, or do arithmetic on figures yourself; if you need a "
        "number you don't have, fetch it. Tool results report money in US DOLLARS already. You may combine "
        "several tools for one question. If a tool returns an 'error', tell the owner plainly and suggest a fix.\n\n"
        "When giving TAX guidance, be concrete for a sole proprietor (Schedule C): typical deductions like "
        "materials/cost of goods, tools and equipment, the standard mileage deduction, home office, software "
        "and subscriptions, and bank/processing fees; quarterly estimated taxes and self-employment tax; and "
        "keeping receipts and a separate business account. Tie advice to the owner's real figures when useful "
        "(e.g. pull the P&L). Always add a brief reminder that this is general information, not a substitute "
        "for a qualified tax professional, and that you don't file anything for them.\n\n"
        "STYLE: concise and direct — lead with the answer, then the few supporting figures. Use plain dollars "
        "($1,250.00). Short paragraphs or tight bullets. You can't change any data or take actions; you read "
        "and explain. If the owner asks you to record or change something, point them to the right page.\n\n"
        + _HELP
    )


# ---------------------------------------------------------------- the loop

def _run_tool(con, today, name, args):
    fn = _HANDLERS.get(name)
    if not fn:
        return json.dumps({"error": f"unknown tool: {name}"})
    try:
        return json.dumps(_to_dollars(fn(con, today, **(args or {}))), default=str)
    except ValueError as e:           # e.g. an unrecognized period string
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"lookup failed: {e}"})


def ask(con, history):
    """Answer the latest question given `history` (a list of {"role","content"} TEXT turns,
    ending with the user's message). Runs the tool-use loop server-side and returns
    (reply_text, error_text) — exactly one is set. AI-optional.

    Tools are re-callable every turn, so follow-up questions ("what about last year?") work
    from the prior text alone; the deterministic figures are always re-fetched fresh.
    """
    if not ai._claude_ok(con):
        return None, ("The assistant needs an Anthropic API key. Add one under Settings → AI, "
                      "then come back. (Everything else in ShopBooks works without it.)")
    client = ai._claude_client(con)
    today = date.today()
    messages = [{"role": m["role"], "content": m["content"]}
                for m in history[-MAX_TURNS:] if m.get("content")]
    if not messages or messages[0]["role"] != "user":
        return None, "Ask a question to get started."
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            resp = client.messages.create(
                model=ai._claude_model(con), max_tokens=4000,  # follows the ai_model setting, like every other AI feature
                thinking={"type": "adaptive"},
                system=_system(con), tools=TOOLS, messages=messages)
            if resp.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": resp.content})
                results = [{"type": "tool_result", "tool_use_id": b.id,
                            "content": _run_tool(con, today, b.name, b.input)}
                           for b in resp.content if b.type == "tool_use"]
                messages.append({"role": "user", "content": results})
                continue
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            return (text or "I'm not sure how to answer that."), None
        return "I had to look up too many things to finish that — try asking something narrower.", None
    except Exception as e:
        log.warning("chat assistant failed: %s", e)
        return None, f"The assistant hit an error: {e}"
