"""Read-only 'book query' layer: deterministic numbers for reports and AI tools.

Every function here computes exact figures straight from the ledger (reusing
ledger.py) and returns plain, JSON-serialisable dicts. This is the foundation
the AI features stand on: the analyses and the chatbot call these for *real*
numbers and only interpret them — the model never does the arithmetic. So the
books stay auditable and reproducible no matter what the AI says about them.

All money is integer cents (positive = the account's natural direction, e.g.
income and asset balances read positive). See ledger.py for the sign convention.
"""
import re
from datetime import date, timedelta

import ledger


# --- periods -----------------------------------------------------------------

def _month_end(year, month):
    first_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return first_next - timedelta(days=1)


def parse_period(period="this-year", today=None):
    """Turn a human/period string into (start, end, label), both dates YYYY-MM-DD.

    Accepts: 'YYYY', 'YYYY-Qn', 'YYYY-MM', and the relative words
    this-year/last-year, this-quarter/last-quarter, this-month/last-month
    (and 'ytd' as an alias for this-year). Built so the chatbot can pass a
    period straight through from a question. Raises ValueError on junk.
    """
    today = today or date.today()
    p = (period or "this-year").strip().lower()

    def yr(y):
        return (f"{y}-01-01", f"{y}-12-31", str(y))

    def quarter(y, q):
        sm = 3 * (q - 1) + 1
        return (f"{y}-{sm:02d}-01", _month_end(y, sm + 2).isoformat(), f"{y} Q{q}")

    def month(y, m):
        return (f"{y}-{m:02d}-01", _month_end(y, m).isoformat(), f"{y}-{m:02d}")

    if p in ("this-year", "ytd", "year"):
        return yr(today.year)
    if p == "last-year":
        return yr(today.year - 1)
    if p in ("this-month", "month"):
        return month(today.year, today.month)
    if p == "last-month":
        y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        return month(y, m)
    cur_q = (today.month - 1) // 3 + 1
    if p in ("this-quarter", "quarter"):
        return quarter(today.year, cur_q)
    if p == "last-quarter":
        return quarter(today.year - 1, 4) if cur_q == 1 else quarter(today.year, cur_q - 1)

    if re.fullmatch(r"\d{4}", p):
        return yr(int(p))
    m = re.fullmatch(r"(\d{4})-q([1-4])", p)
    if m:
        return quarter(int(m.group(1)), int(m.group(2)))
    m = re.fullmatch(r"(\d{4})-(\d{2})", p)
    if m and 1 <= int(m.group(2)) <= 12:
        return month(int(m.group(1)), int(m.group(2)))
    raise ValueError(f"unrecognized period: {period!r}")


# --- profit & loss -----------------------------------------------------------

def _by_category(tree):
    """Flatten ledger's rolled-up account tree to [{name, amount}], biggest first."""
    return sorted(({"name": t["name"], "amount": t["amount"]} for t in tree),
                  key=lambda x: x["amount"], reverse=True)


def pnl_summary(con, start, end):
    """Income/expense/net for a date range, with a per-category breakdown."""
    p = ledger.pnl(con, start, end)
    return {
        "start": start, "end": end,
        "income_total": p["total_income"],
        "expense_total": p["total_expenses"],
        "net": p["net"],
        "income_by_category": _by_category(p["income"]),
        "expense_by_category": _by_category(p["expenses"]),
    }


def _delta(current, previous):
    d = current - previous
    pct = round(d / abs(previous) * 100, 1) if previous else None
    return {"current": current, "previous": previous, "delta": d, "pct_change": pct}


def compare(con, period="this-year", base="last-year", today=None):
    """Period-over-period growth: income, expenses, and net, each with a delta and
    percent change. `period` and `base` are anything parse_period accepts."""
    cs, ce, clabel = parse_period(period, today)
    bs, be, blabel = parse_period(base, today)
    cur = pnl_summary(con, cs, ce)
    prev = pnl_summary(con, bs, be)
    return {
        "current_label": clabel, "base_label": blabel,
        "income": _delta(cur["income_total"], prev["income_total"]),
        "expenses": _delta(cur["expense_total"], prev["expense_total"]),
        "net": _delta(cur["net"], prev["net"]),
    }


def monthly_trend(con, start, end):
    """Income/expense/net for each calendar month spanned by [start, end]."""
    y, m = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    out = []
    while (y, m) <= (ey, em):
        p = ledger.pnl(con, f"{y}-{m:02d}-01", _month_end(y, m).isoformat())
        out.append({"month": f"{y}-{m:02d}", "income": p["total_income"],
                    "expenses": p["total_expenses"], "net": p["net"]})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# --- cash & bookkeeping health ----------------------------------------------

def cash_position(con, as_of=None):
    """Bank balances and credit-card balances owed (as of a date, or right now)."""
    rows = con.execute(
        "SELECT id, name, type, kind FROM accounts "
        "WHERE active=1 AND kind IN ('bank','card') ORDER BY type, name").fetchall()
    banks, cards = [], []
    for r in rows:
        bal = ledger.display_balance(r["type"], ledger.raw_balance(con, r["id"], as_of))
        (banks if r["kind"] == "bank" else cards).append({"name": r["name"], "balance": bal})
    return {
        "as_of": as_of,
        "bank_accounts": banks, "card_accounts": cards,
        "cash_on_hand": sum(b["balance"] for b in banks),
        "card_debt": sum(c["balance"] for c in cards),
    }


def bookkeeping_health(con, start=None, end=None):
    """What still needs attention before the books can be trusted: transactions
    awaiting review, entries left in 'Uncategorized Expense', and receipts not yet
    matched. `tidy` is True only when nothing is outstanding."""
    if start and end:
        uncat = con.execute(
            "SELECT COUNT(DISTINCT e.id) c FROM entries e JOIN splits s ON s.entry_id=e.id "
            "JOIN accounts a ON a.id=s.account_id WHERE a.name='Uncategorized Expense' "
            "AND e.date BETWEEN ? AND ?", (start, end)).fetchone()["c"]
    else:
        uncat = con.execute(
            "SELECT COUNT(DISTINCT e.id) c FROM entries e JOIN splits s ON s.entry_id=e.id "
            "JOIN accounts a ON a.id=s.account_id WHERE a.name='Uncategorized Expense'").fetchone()["c"]
    pending = con.execute("SELECT COUNT(*) c FROM staged WHERE status='pending'").fetchone()["c"]
    unmatched = con.execute("SELECT COUNT(*) c FROM documents WHERE status='unmatched'").fetchone()["c"]
    issues = []
    if pending:
        issues.append(f"{pending} transaction(s) awaiting review")
    if uncat:
        issues.append(f"{uncat} entry(ies) still in 'Uncategorized Expense'")
    if unmatched:
        issues.append(f"{unmatched} receipt(s) not matched to a transaction")
    return {"pending_review": pending, "uncategorized": uncat,
            "unmatched_receipts": unmatched, "issues": issues, "tidy": not issues}


# --- one-call snapshot for the chatbot --------------------------------------

def business_snapshot(con, period="this-year", today=None):
    """Everything the chatbot needs for a 'how's the business doing?' answer, in one
    deterministic call: P&L, the monthly trend, current cash position, and what
    still needs tidying. The model reads this and explains it — it computes nothing."""
    start, end, label = parse_period(period, today)
    return {
        "period": label, "start": start, "end": end,
        "pnl": pnl_summary(con, start, end),
        "monthly_trend": monthly_trend(con, start, end),
        "cash_position": cash_position(con, end),
        "health": bookkeeping_health(con, start, end),
    }
