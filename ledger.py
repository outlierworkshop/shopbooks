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


def post_entry(con, date, payee, splits, memo=""):
    """splits: list of (account_id, amount_cents). Must sum to zero."""
    if sum(c for _, c in splits) != 0:
        raise ValueError("splits do not balance")
    cur = con.execute("INSERT INTO entries(date,payee,memo) VALUES(?,?,?)", (date, payee, memo))
    entry_id = cur.lastrowid
    for account_id, cents in splits:
        if cents != 0:
            con.execute("INSERT INTO splits(entry_id,account_id,amount_cents) VALUES(?,?,?)",
                        (entry_id, account_id, cents))
    return entry_id


def delete_entry(con, entry_id):
    con.execute("UPDATE staged SET status='pending', entry_id=NULL WHERE entry_id=?", (entry_id,))
    con.execute("UPDATE documents SET status='unmatched', entry_id=NULL WHERE entry_id=?", (entry_id,))
    con.execute("UPDATE invoices SET status='sent', paid_date=NULL, paid_entry_id=NULL WHERE paid_entry_id=?",
                (entry_id,))
    con.execute("DELETE FROM entries WHERE id=?", (entry_id,))


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


def accounts_with_balances(con, kinds=None):
    rows = con.execute("SELECT * FROM accounts WHERE active=1 ORDER BY type, name").fetchall()
    out = []
    for a in rows:
        if kinds and a["kind"] not in kinds:
            continue
        raw = raw_balance(con, a["id"])
        out.append({"id": a["id"], "name": a["name"], "type": a["type"], "kind": a["kind"],
                    "balance": display_balance(a["type"], raw)})
    return out


def register(con, account_id):
    """All splits hitting an account, oldest first, with counter-account names and running balance."""
    acct = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    rows = con.execute(
        "SELECT s.id split_id, s.amount_cents, e.id entry_id, e.date, e.payee, e.memo "
        "FROM splits s JOIN entries e ON e.id=s.entry_id WHERE s.account_id=? "
        "ORDER BY e.date, e.id", (account_id,)).fetchall()
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
        })
    out.reverse()  # newest first for display
    return acct, out


def pnl(con, start, end):
    income, expenses = [], []
    for a in con.execute("SELECT * FROM accounts WHERE type IN ('income','expense') AND active=1 ORDER BY name"):
        row = con.execute(
            "SELECT COALESCE(SUM(s.amount_cents),0) t FROM splits s JOIN entries e ON e.id=s.entry_id "
            "WHERE s.account_id=? AND e.date BETWEEN ? AND ?", (a["id"], start, end)).fetchone()
        amt = display_balance(a["type"], row["t"])
        if row["t"] == 0:
            continue
        (income if a["type"] == "income" else expenses).append({"name": a["name"], "amount": amt})
    total_income = sum(i["amount"] for i in income)
    total_expenses = sum(x["amount"] for x in expenses)
    return {"income": income, "expenses": expenses, "total_income": total_income,
            "total_expenses": total_expenses, "net": total_income - total_expenses}


def balance_sheet(con, as_of):
    assets, liabilities, equity = [], [], []
    for a in con.execute("SELECT * FROM accounts WHERE type IN ('asset','liability','equity') AND active=1 ORDER BY type, name"):
        raw = raw_balance(con, a["id"], as_of)
        if raw == 0:
            continue
        item = {"name": a["name"], "amount": display_balance(a["type"], raw)}
        {"asset": assets, "liability": liabilities, "equity": equity}[a["type"]].append(item)
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
        equity.append({"name": "Retained Earnings (calculated)", "amount": retained})
        total_eq += retained
    return {"assets": assets, "liabilities": liabilities, "equity": equity,
            "total_assets": total_assets, "total_liabilities": total_liab, "total_equity": total_eq}
