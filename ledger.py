"""Double-entry ledger core: posting, balances, registers, reports.

Sign convention: every entry's splits sum to zero.
A positive split is a debit, a negative split is a credit.
  asset/expense accounts increase with positive (debit) splits;
  liability/equity/income accounts increase with negative (credit) splits.
Display balances are sign-adjusted per account type so everything reads
as a normal positive number when the account holds its natural balance.
"""
from datetime import datetime

CREDIT_NORMAL = ("liability", "equity", "income")


def fmt_cents(cents):
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}{c // 100:,}.{c % 100:02d}"


def parse_amount_to_cents(text):
    """Parse '$1,234.56', '(45.00)', '45.00-' style amounts into signed cents."""
    s = str(text).strip().replace("$", "").replace(",", "").replace(" ", "")
    if not s:
        raise ValueError("empty amount")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.endswith("-"):
        neg, s = True, s[:-1]
    if s.startswith("-"):
        neg, s = True, s[1:]
    val = round(float(s) * 100)
    return -val if neg else val


def normalize_date(text):
    """Normalize common bank date formats to YYYY-MM-DD."""
    s = str(text).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"unrecognized date: {text!r}")


def post_entry(con, date, payee, splits, memo="", job_id=None):
    """splits: list of (account_id, amount_cents). Must sum to zero.
    job_id optionally tags the whole transaction to a job (for job costing)."""
    if sum(c for _, c in splits) != 0:
        raise ValueError("splits do not balance")
    cur = con.execute("INSERT INTO entries(date,payee,memo,job_id) VALUES(?,?,?,?)",
                      (date, payee, memo, job_id or None))
    entry_id = cur.lastrowid
    for account_id, cents in splits:
        if cents != 0:
            con.execute("INSERT INTO splits(entry_id,account_id,amount_cents) VALUES(?,?,?)",
                        (entry_id, account_id, cents))
    return entry_id


def set_entry_job(con, entry_id, job_id):
    """Tag (or, with job_id=None, untag) an existing transaction to a job."""
    con.execute("UPDATE entries SET job_id=? WHERE id=?", (job_id or None, entry_id))


def entry_category(con, entry_id):
    """The income/expense leg of a simple categorized transaction, or None if it isn't one
    (e.g. a transfer between own accounts, or a multi-category split). Returns a dict
    {account_id, name, type} for the single category leg."""
    legs = con.execute(
        "SELECT s.account_id, a.name, a.type FROM splits s JOIN accounts a ON a.id=s.account_id "
        "WHERE s.entry_id=? AND a.type IN ('income','expense')", (entry_id,)).fetchall()
    if len(legs) != 1:
        return None
    return {"account_id": legs[0]["account_id"], "name": legs[0]["name"], "type": legs[0]["type"]}


def set_entry_category(con, entry_id, new_account_id):
    """Re-point the income/expense leg of a simple 2-sided transaction to a different account
    of the SAME type (amounts unchanged, so the entry stays balanced). Returns the old
    {account_id, name, type} on success, or None if the entry isn't a simple categorized txn
    or the new account is a different type (which would corrupt the sign/meaning)."""
    cur = entry_category(con, entry_id)
    if cur is None:
        return None
    new = con.execute("SELECT id, type FROM accounts WHERE id=?", (new_account_id,)).fetchone()
    if not new or new["type"] != cur["type"]:
        return None
    if new_account_id != cur["account_id"]:
        con.execute("UPDATE splits SET account_id=? WHERE entry_id=? AND account_id=?",
                    (new_account_id, entry_id, cur["account_id"]))
    return cur


def delete_entry(con, entry_id):
    con.execute("UPDATE staged SET status='pending', entry_id=NULL WHERE entry_id=?", (entry_id,))
    con.execute("UPDATE documents SET status='unmatched', entry_id=NULL WHERE entry_id=?", (entry_id,))
    con.execute("UPDATE invoices SET status='sent', paid_date=NULL, paid_entry_id=NULL WHERE paid_entry_id=?",
                (entry_id,))
    con.execute("UPDATE invoices SET status='sent', paid_date=NULL, matched_entry_id=NULL WHERE matched_entry_id=?",
                (entry_id,))
    con.execute("DELETE FROM entries WHERE id=?", (entry_id,))


def update_entry_fields(con, entry_id, payee, memo, category_id, job_id, date, register_account_id, new_register_account_id=None):
    """Update date, payee, memo, and job on entries table.
    For 2-split entries, update:
      - the split that IS register_account_id to new_register_account_id (if provided)
      - the split that is NOT register_account_id to category_id (if category_id is provided)"""
    con.execute(
        "UPDATE entries SET date=?, payee=?, memo=?, job_id=? WHERE id=?",
        (date, payee, memo, job_id or None, entry_id)
    )
    splits = con.execute("SELECT id, account_id FROM splits WHERE entry_id=?", (entry_id,)).fetchall()
    if len(splits) == 2:
        reg_split = None
        other_split = None
        for s in splits:
            if s["account_id"] == register_account_id:
                reg_split = s
            else:
                other_split = s
        
        # Fallbacks
        if not reg_split and not other_split:
            reg_split, other_split = splits[0], splits[1]
        elif not reg_split:
            reg_split = splits[0] if other_split["id"] != splits[0]["id"] else splits[1]
        elif not other_split:
            other_split = splits[0] if reg_split["id"] != splits[0]["id"] else splits[1]
            
        if new_register_account_id is not None and new_register_account_id != reg_split["account_id"]:
            con.execute("UPDATE splits SET account_id=? WHERE id=?", (new_register_account_id, reg_split["id"]))
            
        if category_id is not None and category_id != other_split["account_id"]:
            con.execute("UPDATE splits SET account_id=? WHERE id=?", (category_id, other_split["id"]))


def raw_balance(con, account_id, as_of=None):
    if as_of:
        row = con.execute(
            "SELECT COALESCE(SUM(s.amount_cents),0) b FROM splits s JOIN entries e ON e.id=s.entry_id "
            "WHERE s.account_id=? AND e.date<=?", (account_id, as_of)).fetchone()
    else:
        row = con.execute("SELECT COALESCE(SUM(amount_cents),0) b FROM splits WHERE account_id=?",
                          (account_id,)).fetchone()
    return row["b"]


def display_balance(acct_type, raw):
    return -raw if acct_type in CREDIT_NORMAL else raw


def accounts_with_balances(con, kinds=None, include_inactive=False):
    """Accounts in tree order (each parent followed by its sub-accounts). Active only by default;
    pass include_inactive=True (the Accounts page) to also list hidden accounts so they can be
    reactivated. `has_history` flags accounts with posted splits (can't be hidden)."""
    where = "" if include_inactive else " WHERE active=1"
    rows = con.execute("SELECT * FROM accounts" + where).fetchall()
    names = {r["id"]: r["name"] for r in rows}

    def mk(a, is_parent):
        has_history = con.execute("SELECT 1 FROM splits WHERE account_id=? LIMIT 1", (a["id"],)).fetchone() is not None
        return {"id": a["id"], "name": a["name"], "type": a["type"], "kind": a["kind"],
                "parent_id": a["parent_id"], "parent_name": names.get(a["parent_id"]),
                "active": a["active"], "has_history": has_history,
                "is_parent": is_parent, "balance": display_balance(a["type"], raw_balance(con, a["id"])),
                "schedule_c_line": a["schedule_c_line"]}

    tops = sorted((r for r in rows if not r["parent_id"]), key=lambda r: (r["type"], r["name"]))
    out = []
    for p in tops:
        kids = sorted((r for r in rows if r["parent_id"] == p["id"]), key=lambda r: r["name"])
        if not kinds or p["kind"] in kinds:
            out.append(mk(p, bool(kids)))
        for c in kids:
            if not kinds or c["kind"] in kinds:
                out.append(mk(c, False))
    return out


def _range_total(con, account_id, start, end):
    row = con.execute(
        "SELECT COALESCE(SUM(s.amount_cents),0) t FROM splits s JOIN entries e ON e.id=s.entry_id "
        "WHERE s.account_id=? AND e.date BETWEEN ? AND ?", (account_id, start, end)).fetchone()
    return row["t"]


def _account_tree(con, types, raw_fn):
    """Roll sub-accounts up under their parent. `raw_fn(account_id)` -> raw cents.
    Returns a list of {name, type, amount (rolled-up, display-signed), own, children:[{name,amount}]}."""
    placeholders = ",".join("?" * len(types))
    parents = con.execute(
        f"SELECT * FROM accounts WHERE type IN ({placeholders}) AND active=1 AND parent_id IS NULL "
        "ORDER BY type, name", types).fetchall()
    out = []
    for p in parents:
        own = display_balance(p["type"], raw_fn(p["id"]))
        kids = []
        for c in con.execute("SELECT * FROM accounts WHERE parent_id=? AND active=1 ORDER BY name", (p["id"],)).fetchall():
            camt = display_balance(c["type"], raw_fn(c["id"]))
            if camt != 0:
                kids.append({"name": c["name"], "amount": camt})
        if own == 0 and not kids:
            continue
        out.append({"name": p["name"], "type": p["type"], "own": own,
                    "amount": own + sum(k["amount"] for k in kids), "children": kids})
    return out


def register(con, account_id):
    """All splits hitting an account, oldest first, with counter-account names and running balance."""
    acct = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    rows = con.execute(
        "SELECT s.id split_id, s.amount_cents, e.id entry_id, e.date, e.payee, e.memo, "
        "e.job_id, j.name job_name "
        "FROM splits s JOIN entries e ON e.id=s.entry_id LEFT JOIN jobs j ON j.id=e.job_id "
        "WHERE s.account_id=? ORDER BY e.date, e.id", (account_id,)).fetchall()
    out, running = [], 0
    for r in rows:
        running += r["amount_cents"]
        others = con.execute(
            "SELECT a.name FROM splits s JOIN accounts a ON a.id=s.account_id "
            "WHERE s.entry_id=? AND s.account_id!=?", (r["entry_id"], account_id)).fetchall()
        doc = con.execute("SELECT id FROM documents WHERE entry_id=?", (r["entry_id"],)).fetchone()
        out.append({
            "entry_id": r["entry_id"], "date": r["date"], "payee": r["payee"], "memo": r["memo"],
            "amount": display_balance(acct["type"], r["amount_cents"]),
            "balance": display_balance(acct["type"], running),
            "other": ", ".join(o["name"] for o in others) or "(split)",
            "doc_id": doc["id"] if doc else None,
            "job_id": r["job_id"], "job": r["job_name"],
        })
    out.reverse()  # newest first for display
    return acct, out


def pnl(con, start, end):
    raw_fn = lambda aid: _range_total(con, aid, start, end)
    income = _account_tree(con, ("income",), raw_fn)
    expenses = _account_tree(con, ("expense",), raw_fn)
    total_income = sum(i["amount"] for i in income)
    total_expenses = sum(x["amount"] for x in expenses)
    return {"income": income, "expenses": expenses, "total_income": total_income,
            "total_expenses": total_expenses, "net": total_income - total_expenses}


def balance_sheet(con, as_of):
    raw_fn = lambda aid: raw_balance(con, aid, as_of)
    assets = _account_tree(con, ("asset",), raw_fn)
    liabilities = _account_tree(con, ("liability",), raw_fn)
    equity = _account_tree(con, ("equity",), raw_fn)
    total_assets = sum(i["amount"] for i in assets)
    total_liab = sum(i["amount"] for i in liabilities)
    total_eq = sum(i["amount"] for i in equity)
    # retained earnings = cumulative net income (income/expense raw sums are credits-negative)
    row = con.execute(
        "SELECT COALESCE(SUM(s.amount_cents),0) t FROM splits s JOIN entries e ON e.id=s.entry_id "
        "JOIN accounts a ON a.id=s.account_id WHERE a.type IN ('income','expense') AND e.date<=?",
        (as_of,)).fetchone()
    retained = -row["t"]
    if retained:
        equity.append({"name": "Retained Earnings (calculated)", "amount": retained, "own": retained, "children": []})
        total_eq += retained
    return {"assets": assets, "liabilities": liabilities, "equity": equity,
            "total_assets": total_assets, "total_liabilities": total_liab, "total_equity": total_eq}
