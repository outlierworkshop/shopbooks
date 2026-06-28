"""Recurring transactions: templates for predictable bills/income the owner confirms to post.

Nothing posts automatically — each due occurrence is one click, so the ledger stays
human-confirmed (consistent with Review). Posting an occurrence advances the template to its
next date; skipping advances without posting. `upcoming()` projects future occurrences, which the
cash-flow forecast (#38) builds on. All money is integer cents; `amount_cents` is always positive
and `flow` decides direction.
"""
from calendar import monthrange
from datetime import date, datetime, timedelta

import ledger


def _step(d, frequency):
    """The next date after `d` for a frequency. Month/year steps clamp to the month's last day
    (so the 31st recurs on the 30th/28th where needed) rather than rolling over."""
    if frequency == "weekly":
        return d + timedelta(days=7)
    if frequency == "yearly":
        y = d.year + 1
        return date(y, d.month, min(d.day, monthrange(y, d.month)[1]))
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)   # monthly (default)
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def advance(date_str, frequency, n=1):
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    for _ in range(n):
        d = _step(d, frequency)
    return d.isoformat()


def _rows(con, where="", args=()):
    return con.execute(
        "SELECT r.*, acct.name account_name, cat.name category_name FROM recurring r "
        "JOIN accounts acct ON acct.id=r.account_id JOIN accounts cat ON cat.id=r.category_id "
        + where + " ORDER BY r.active DESC, r.next_date", args).fetchall()


def list_all(con, today=None):
    """Every template with account/category names and a `due`/`days_overdue` flag for the UI."""
    today = today or date.today().isoformat()
    out = []
    for r in _rows(con):
        due = bool(r["active"]) and r["next_date"] <= today
        days = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(r["next_date"], "%Y-%m-%d")).days
        out.append({**dict(r), "due": due, "days_overdue": max(days, 0) if due else 0})
    return out


def due(con, today=None):
    """Active templates whose next_date has arrived — the ones ready to post."""
    today = today or date.today().isoformat()
    return _rows(con, "WHERE r.active=1 AND r.next_date<=?", (today,))


def _splits(r):
    amt = r["amount_cents"]
    if r["flow"] == "income":
        return [(r["account_id"], amt), (r["category_id"], -amt)]   # money into the bank/card
    return [(r["category_id"], amt), (r["account_id"], -amt)]        # expense out of the bank/card


def post_occurrence(con, rid, on_date=None):
    """Post this occurrence to the ledger (human-confirmed) and advance the template one period.
    Raises ledger.LockedPeriodError if the date falls in a closed period."""
    r = con.execute("SELECT * FROM recurring WHERE id=?", (rid,)).fetchone()
    if not r:
        raise ValueError("recurring item not found")
    d = on_date or r["next_date"]
    entry_id = ledger.post_entry(con, d, r["name"], _splits(r), memo=r["memo"] or "")
    con.execute("UPDATE recurring SET next_date=?, last_posted_date=? WHERE id=?",
                (advance(r["next_date"], r["frequency"]), d, rid))
    return entry_id


def skip_occurrence(con, rid):
    """Advance the template one period WITHOUT posting (the bill didn't happen this time)."""
    r = con.execute("SELECT next_date, frequency FROM recurring WHERE id=?", (rid,)).fetchone()
    if not r:
        raise ValueError("recurring item not found")
    con.execute("UPDATE recurring SET next_date=? WHERE id=?",
                (advance(r["next_date"], r["frequency"]), rid))


def upcoming(con, start, end):
    """Projected occurrences of every active template within [start, end], signed (income +,
    expense -). For the cash-flow forecast. Capped per template to avoid runaway loops."""
    out = []
    for r in con.execute("SELECT * FROM recurring WHERE active=1").fetchall():
        d = r["next_date"]
        for _ in range(400):
            if d > end:
                break
            if d >= start:
                signed = r["amount_cents"] if r["flow"] == "income" else -r["amount_cents"]
                out.append({"date": d, "name": r["name"], "amount": signed,
                            "flow": r["flow"], "recurring_id": r["id"]})
            d = advance(d, r["frequency"])
    out.sort(key=lambda x: x["date"])
    return out
