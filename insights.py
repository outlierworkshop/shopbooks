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
from datetime import date, datetime, timedelta

import db
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

def expense_changes(con, period="this-year", base="last-year", today=None):
    """Per-expense-category totals this period vs the base period, sorted by biggest absolute
    change — the movers/outliers worth noticing. Each row: name, current, previous, delta, pct."""
    cs, ce, clabel = parse_period(period, today)
    bs, be, blabel = parse_period(base, today)
    cur = {c["name"]: c["amount"] for c in pnl_summary(con, cs, ce)["expense_by_category"]}
    prev = {c["name"]: c["amount"] for c in pnl_summary(con, bs, be)["expense_by_category"]}
    rows = []
    for n in set(cur) | set(prev):
        a, b = cur.get(n, 0), prev.get(n, 0)
        d = a - b
        rows.append({"name": n, "current": a, "previous": b, "delta": d,
                     "pct_change": round(d / abs(b) * 100, 1) if b else None})
    rows.sort(key=lambda x: abs(x["delta"]), reverse=True)
    return {"current_label": clabel, "base_label": blabel, "rows": rows}


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


def briefing(con, today=None):
    """The 'what needs me today' snapshot for the dashboard (and the Assistant): cash, receivables,
    the next estimated-tax date, and a prioritized list of things needing attention. Deterministic —
    every figure comes from the ledger/insights/invoicing layers; the model only narrates it.

    `attention` items are dicts {level: 'warn'|'info', text, href}. `all_clear` is True when the
    list is empty (books are tidy and nothing is chasing the owner)."""
    import invoicing   # lazy: avoids a top-level insights<->invoicing import edge
    import reconcile
    import recurring
    today = today or date.today().isoformat()
    yr = int(today[:4])
    fmt = ledger.fmt_cents

    cash = cash_position(con)
    ar = invoicing.ar_aging(con, today)
    credit_avail = invoicing.available_credit_total(con)
    health = bookkeeping_health(con)            # overall (not period-scoped)
    miss = missing_receipts(con, f"{yr}-01-01", f"{yr}-12-31")
    out_of_bal = [a for a in reconcile.status(con) if a["out_of_balance"]]

    try:
        rate = float(db.get_setting(con, "estimated_income_tax_rate", "15"))
    except ValueError:
        rate = 15.0
    upcoming = [q for y in (yr, yr + 1) for q in estimated_taxes(con, y, rate)["quarters"]
                if q["due_date"] >= today and q["total_due"] > 0]
    upcoming.sort(key=lambda q: q["due_date"])
    next_tax = upcoming[0] if upcoming else None
    days_to_tax = ((datetime.strptime(next_tax["due_date"], "%Y-%m-%d")
                    - datetime.strptime(today, "%Y-%m-%d")).days) if next_tax else None

    attn = []
    def add(level, text, href=None):
        attn.append({"level": level, "text": text, "href": href})

    if health["pending_review"]:
        add("warn", f"{health['pending_review']} imported transaction(s) waiting in Review", "/review")
    due_recur = recurring.due(con, today)
    if due_recur:
        add("info", f"{len(due_recur)} recurring bill(s) ready to post", "/recurring")
    if ar["overdue_count"]:
        add("warn", f"{ar['overdue_count']} overdue invoice(s) — ${fmt(ar['overdue_total'])} past due", "/invoices")
    if credit_avail > 0:
        add("info", f"${fmt(credit_avail)} in unused customer credit to apply", "/invoices")
    if out_of_bal:
        add("warn", f"{len(out_of_bal)} account(s) out of balance at last reconcile", "/reconcile")
    fc = cash_forecast(con, horizon_days=90, today=today)
    if fc["goes_negative"]:
        add("warn", f"Cash is projected to dip below $0 around {fc['low_point']['label']}", "/forecast")
    if health["unmatched_receipts"]:
        add("info", f"{health['unmatched_receipts']} receipt(s) not matched to a transaction", "/receipts")
    if miss:
        add("info", f"{len(miss)} expense(s) this year missing a receipt", f"/receipts/missing?period={yr}")
    if health["uncategorized"]:
        add("info", f"{health['uncategorized']} entry(ies) still in Uncategorized Expense", "/insights")
    if next_tax and days_to_tax is not None and days_to_tax <= 45:
        add("info", f"Estimated tax {next_tax['quarter']} (~${fmt(next_tax['total_due'])}) due {next_tax['due_date']}", "/taxes")

    return {
        "today": today,
        "cash_on_hand": cash["cash_on_hand"], "card_debt": cash["card_debt"],
        "receivables_total": ar["total"], "receivables_overdue": ar["overdue_total"],
        "overdue_count": ar["overdue_count"], "open_invoices": ar["open_count"],
        "customer_credit": credit_avail,
        "next_tax": ({"quarter": next_tax["quarter"], "due_date": next_tax["due_date"],
                      "amount": next_tax["total_due"], "days": days_to_tax} if next_tax else None),
        "attention": attn, "all_clear": not attn,
    }


def cash_forecast(con, horizon_days=90, today=None, history_months=6):
    """Project cash over the next `horizon_days`, by calendar month: starting cash, expected invoice
    collections in (outstanding invoices, placed on their due date — overdue ones assumed to land now),
    and an estimated expense burn out (trailing `history_months` average). Flags the low point and any
    month the balance would go negative. Deterministic + explainable; the Assistant narrates it.

    Estimate, not a promise: expenses are a historical average (sharper once recurring bills land, #39),
    and invoice timing assumes customers pay by the due date."""
    import invoicing  # lazy (see briefing)
    import recurring
    today = today or date.today().isoformat()
    start = date.fromisoformat(today)
    end = start + timedelta(days=horizon_days)

    starting_cash = cash_position(con)["cash_on_hand"]

    # trailing-average TOTAL expense burn from the ledger
    hy, hm = start.year, start.month - history_months
    while hm <= 0:
        hm += 12
        hy -= 1
    hist_exp = ledger.pnl(con, f"{hy}-{hm:02d}-01", today)["total_expenses"]
    avg_monthly_expense = round(hist_exp / max(history_months, 1))

    # The recurring EXPENSE templates are a known subset of that burn. Estimate their monthly cost and
    # carve it out, so we can place the real recurring bills explicitly (sharper) without double-counting:
    # variable_burn is the smoothed "everything else".
    _per_month = {"weekly": 52 / 12, "monthly": 1.0, "yearly": 1 / 12}
    recurring_monthly_expense = round(sum(
        r["amount_cents"] * _per_month.get(r["frequency"], 1.0) for r in
        con.execute("SELECT amount_cents, frequency FROM recurring WHERE active=1 AND flow='expense'").fetchall()))
    variable_burn = max(0, avg_monthly_expense - recurring_monthly_expense)

    # expected inflows from open invoices, bucketed by the month they're expected to land
    inflow_by = {}
    expected_inflow_total = 0
    for r in invoicing.ar_aging(con, today)["rows"]:
        if r["total"] <= 0:                            # skip net-credit rows
            continue
        due = date.fromisoformat(r["due_date"])
        when = due if due >= start else start          # overdue -> assume it comes in now
        if when <= end:
            key = f"{when.year}-{when.month:02d}"
            inflow_by[key] = inflow_by.get(key, 0) + r["total"]
            expected_inflow_total += r["total"]

    # known recurring flows in the horizon, by month (income in, expense out)
    rec_in, rec_out = {}, {}
    recurring_income_total = recurring_expense_total = 0
    for occ in recurring.upcoming(con, today, end.isoformat()):
        key = occ["date"][:7]
        if occ["amount"] >= 0:
            rec_in[key] = rec_in.get(key, 0) + occ["amount"]
            recurring_income_total += occ["amount"]
        else:
            rec_out[key] = rec_out.get(key, 0) - occ["amount"]
            recurring_expense_total -= occ["amount"]

    months = []
    bal = starting_cash
    low = {"label": "now", "balance": starting_cash}
    cy, cm = start.year, start.month
    while (cy, cm) <= (end.year, end.month):
        key = f"{cy}-{cm:02d}"
        inflow = inflow_by.get(key, 0) + rec_in.get(key, 0)
        outflow = variable_burn + rec_out.get(key, 0)
        bal = bal + inflow - outflow
        label = date(cy, cm, 1).strftime("%b %Y")
        months.append({"month": key, "label": label, "inflow": inflow,
                       "outflow": outflow, "end_balance": bal})
        if bal < low["balance"]:
            low = {"label": label, "balance": bal}
        cy, cm = (cy + 1, 1) if cm == 12 else (cy, cm + 1)

    return {
        "today": today, "horizon_days": horizon_days, "starting_cash": starting_cash,
        "avg_monthly_expense": avg_monthly_expense, "variable_burn": variable_burn,
        "recurring_monthly_expense": recurring_monthly_expense,
        "expected_inflow_total": expected_inflow_total,
        "recurring_income_total": recurring_income_total, "recurring_expense_total": recurring_expense_total,
        "months": months, "low_point": low,
        "goes_negative": any(m["end_balance"] < 0 for m in months) or starting_cash < 0,
        "projected_end": months[-1]["end_balance"] if months else starting_cash,
    }


def missing_receipts(con, start, end, min_cents=0):
    """Posted EXPENSE transactions in [start, end] with no receipt attached and an expense
    amount >= min_cents — the 'which purchases lack documentation' list for tax time. Excludes
    transfers/income (no expense leg) and anything already matched to a document. Newest first."""
    rows = con.execute(
        "SELECT e.id, e.date, e.payee, "
        "  COALESCE(SUM(CASE WHEN a.type='expense' THEN s.amount_cents ELSE 0 END),0) amount, "
        "  (SELECT a2.name FROM splits s2 JOIN accounts a2 ON a2.id=s2.account_id "
        "   WHERE s2.entry_id=e.id AND a2.type='expense' ORDER BY s2.amount_cents DESC LIMIT 1) category, "
        "  (SELECT a3.name FROM splits s3 JOIN accounts a3 ON a3.id=s3.account_id "
        "   WHERE s3.entry_id=e.id AND a3.type IN ('asset','liability') LIMIT 1) source_account "
        "FROM entries e JOIN splits s ON s.entry_id=e.id JOIN accounts a ON a.id=s.account_id "
        "WHERE e.date BETWEEN ? AND ? "
        "  AND NOT EXISTS (SELECT 1 FROM document_entry_links d WHERE d.entry_id=e.id) "
        "GROUP BY e.id HAVING amount >= ? "
        "ORDER BY e.date DESC, e.id DESC", (start, end, max(int(min_cents), 1))).fetchall()
    return [{"entry_id": r["id"], "date": r["date"], "payee": r["payee"],
             "amount": r["amount"], "category": r["category"], "account": r["source_account"]} for r in rows]


def schedule_c_report(con, start, end):
    """Group active income and expense account balances by schedule_c_line.
    Returns mapped line totals and details of any active unmapped categories.
    """
    rows = con.execute(
        "SELECT id, name, type, schedule_c_line FROM accounts "
        "WHERE active=1 AND type IN ('income', 'expense')"
    ).fetchall()

    unmapped = []
    line_totals = {}  # {line_name: {"amount": cents, "type": type, "accounts": [...]}}
    
    total_income = 0
    total_expenses = 0
    
    for r in rows:
        # Get balance for the range
        raw = con.execute(
            "SELECT COALESCE(SUM(s.amount_cents),0) t FROM splits s "
            "JOIN entries e ON e.id=s.entry_id "
            "WHERE s.account_id=? AND e.date BETWEEN ? AND ?",
            (r["id"], start, end)
        ).fetchone()["t"]
        
        # Display balance normalisation (flip sign for income)
        bal = -raw if r["type"] == "income" else raw
        
        line = r["schedule_c_line"]
        if not line:
            unmapped.append({
                "id": r["id"],
                "name": r["name"],
                "type": r["type"],
                "balance": bal
            })
        else:
            if line not in line_totals:
                line_totals[line] = {
                    "amount": 0,
                    "type": r["type"],
                    "accounts": []
                }
            line_totals[line]["amount"] += bal
            line_totals[line]["accounts"].append({
                "name": r["name"],
                "amount": bal
            })
            
            if r["type"] == "income":
                total_income += bal
            else:
                total_expenses += bal

    # Format the sections
    income_lines = []
    expense_lines = []
    for line, info in line_totals.items():
        if info["amount"] != 0 or any(a["amount"] != 0 for a in info["accounts"]):
            item = {
                "line": line,
                "amount": info["amount"],
                "accounts": sorted(info["accounts"], key=lambda x: x["amount"], reverse=True)
            }
            if info["type"] == "income":
                income_lines.append(item)
            else:
                expense_lines.append(item)

    income_lines.sort(key=lambda x: x["line"])
    expense_lines.sort(key=lambda x: x["line"])

    return {
        "start": start,
        "end": end,
        "income": income_lines,
        "expenses": expense_lines,
        "total_income": total_income,
        "total_expenses": total_expenses,
        "net": total_income - total_expenses,
        "unmapped": sorted(unmapped, key=lambda x: x["name"])
    }


def estimated_taxes(con, year, est_income_tax_rate):
    """Calculates quarterly estimated taxes for the business for a given year.
    Quarters follow the IRS estimated payment dates:
      - Q1: Jan 1 - Mar 31
      - Q2: Apr 1 - May 31
      - Q3: Jun 1 - Aug 31
      - Q4: Sep 1 - Dec 31
    """
    quarters = [
        {"quarter": "Q1", "period": "Jan 1 – Mar 31", "start": f"{year}-01-01", "end": f"{year}-03-31", "due_date": f"{year}-04-15"},
        {"quarter": "Q2", "period": "Apr 1 – May 31", "start": f"{year}-04-01", "end": f"{year}-05-31", "due_date": f"{year}-06-15"},
        {"quarter": "Q3", "period": "Jun 1 – Aug 31", "start": f"{year}-06-01", "end": f"{year}-08-31", "due_date": f"{year}-09-15"},
        {"quarter": "Q4", "period": "Sep 1 – Dec 31", "start": f"{year}-09-01", "end": f"{year}-12-31", "due_date": f"{year+1}-01-15"},
    ]
    
    rows = []
    total_net = 0
    total_se = 0
    total_inc = 0
    total_due_sum = 0
    
    for q in quarters:
        p = ledger.pnl(con, q["start"], q["end"])
        net = p["net"]
        
        # Self-Employment Tax: net_profit * 92.35% * 15.3%
        se = max(0, round(net * 0.9235 * 0.153))
        # Income Tax: net_profit * estimated_income_tax_rate
        inc = max(0, round(net * (est_income_tax_rate / 100.0)))
        due = se + inc
        
        rows.append({
            "quarter": q["quarter"],
            "period": q["period"],
            "net_profit": net,
            "se_tax": se,
            "income_tax": inc,
            "total_due": due,
            "due_date": q["due_date"]
        })
        
        total_net += net
        total_se += se
        total_inc += inc
        total_due_sum += due
        
    return {
        "year": year,
        "rate": est_income_tax_rate,
        "quarters": rows,
        "total_net_profit": total_net,
        "total_se_tax": total_se,
        "total_income_tax": total_inc,
        "total_due": total_due_sum
    }
