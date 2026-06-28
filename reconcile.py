"""Reconciliation: confirm a bank/card account's books match its statement.

Two complementary tools, both deterministic (numbers come straight from the ledger):
  - Quick balance check: enter the statement's closing date + ending balance; we compare it to
    the book balance as of that date and show the difference (compute/record), flagging likely
    duplicates when it's off.
  - Per-transaction clearing (Phase 2): tick each transaction that appears on the statement; when
    the cleared balance equals the statement's ending balance, finish() stamps those splits
    reconciled (splits.reconciled_id) so they carry forward as the next statement's beginning
    balance and are never re-presented.

Balances are display-signed (natural reading): assets read positive, a credit card
reads positive when you owe money — matching how the user reads a statement, so the
entered ending balance and the book balance are directly comparable.
"""
from datetime import datetime

import ledger


def _book_balance(con, account, as_of=None):
    return ledger.display_balance(account["type"], ledger.raw_balance(con, account["id"], as_of))


def compute(con, account_id, statement_date, statement_balance_cents):
    """Compare the statement's ending balance to the book balance as of that date.
    Pure (no writes). Returns the account, both balances, and the difference."""
    a = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not a:
        return None
    book = _book_balance(con, a, statement_date)
    diff = statement_balance_cents - book
    return {"account": a, "statement_date": statement_date,
            "statement_balance": statement_balance_cents, "book_balance": book,
            "difference": diff, "reconciled": diff == 0}


def last_reconciliation(con, account_id):
    return con.execute(
        "SELECT * FROM reconciliations WHERE account_id=? ORDER BY statement_date DESC, id DESC LIMIT 1",
        (account_id,)).fetchone()


def record(con, account_id, statement_date, statement_balance_cents):
    """Compute and save a reconciliation checkpoint. Returns the compute() result."""
    c = compute(con, account_id, statement_date, statement_balance_cents)
    if c is None:
        raise ValueError("account not found")
    con.execute(
        "INSERT INTO reconciliations(account_id,statement_date,statement_balance_cents,"
        "book_balance_cents,difference_cents) VALUES(?,?,?,?,?)",
        (account_id, statement_date, statement_balance_cents, c["book_balance"], c["difference"]))
    return c


def history(con, account_id, limit=24):
    return con.execute(
        "SELECT * FROM reconciliations WHERE account_id=? ORDER BY statement_date DESC, id DESC LIMIT ?",
        (account_id, limit)).fetchall()


def period_transactions(con, account_id, after_date, on_or_before):
    """Transactions hitting an account in (after_date, on_or_before], newest first, with the
    amount as it affects that account (display-signed)."""
    a = con.execute("SELECT type FROM accounts WHERE id=?", (account_id,)).fetchone()
    q = ("SELECT e.id, e.date, e.payee, s.amount_cents FROM entries e JOIN splits s ON s.entry_id=e.id "
         "WHERE s.account_id=? AND e.date<=? ")
    args = [account_id, on_or_before]
    if after_date:
        q += "AND e.date>? "
        args.append(after_date)
    q += "ORDER BY e.date DESC, e.id DESC"
    rows = con.execute(q, args).fetchall()
    return [{"id": r["id"], "date": r["date"], "payee": r["payee"],
             "amount": ledger.display_balance(a["type"], r["amount_cents"])} for r in rows]


def likely_duplicates(con, account_id, after_date, on_or_before, window_days=5):
    """Pairs of transactions on the account with the same amount within `window_days` in the
    period — the usual cause of a reconciliation gap (a line entered or imported twice)."""
    txns = period_transactions(con, account_id, after_date, on_or_before)
    flagged = []
    for i, a in enumerate(txns):
        for b in txns[i + 1:]:
            if a["amount"] == b["amount"] and abs(
                    (datetime.strptime(a["date"], "%Y-%m-%d")
                     - datetime.strptime(b["date"], "%Y-%m-%d")).days) <= window_days:
                flagged.append((a, b))
    return flagged


# --- Phase 2: per-transaction clearing ---------------------------------------

def cleared_balance(con, account_id):
    """Display-signed balance of everything already cleared on this account — the carry-forward
    'beginning' balance for the next statement. Independent of dates (driven by reconciled_id)."""
    a = con.execute("SELECT type FROM accounts WHERE id=?", (account_id,)).fetchone()
    raw = con.execute("SELECT COALESCE(SUM(amount_cents),0) s FROM splits "
                      "WHERE account_id=? AND reconciled_id IS NOT NULL", (account_id,)).fetchone()["s"]
    return ledger.display_balance(a["type"], raw)


def unreconciled_transactions(con, account_id, on_or_before):
    """Uncleared account-legs dated on/before the statement date — the checklist to tick. Each row
    carries its split_id (what finish() stamps) and a display-signed amount. Oldest first."""
    a = con.execute("SELECT type FROM accounts WHERE id=?", (account_id,)).fetchone()
    rows = con.execute(
        "SELECT s.id sid, e.id eid, e.date, e.payee, s.amount_cents FROM entries e "
        "JOIN splits s ON s.entry_id=e.id "
        "WHERE s.account_id=? AND s.reconciled_id IS NULL AND e.date<=? "
        "ORDER BY e.date, e.id", (account_id, on_or_before)).fetchall()
    return [{"split_id": r["sid"], "entry_id": r["eid"], "date": r["date"], "payee": r["payee"],
             "amount": ledger.display_balance(a["type"], r["amount_cents"])} for r in rows]


def finish(con, account_id, statement_date, statement_balance_cents, cleared_split_ids):
    """Clear the given splits against a new reconciliation checkpoint, computed from the CLEARED
    balance (already-cleared + the newly ticked) — not the whole book. Marks those splits
    reconciled and records the checkpoint. Returns the difference and whether it came out to zero."""
    a = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not a:
        raise ValueError("account not found")
    ids = [int(i) for i in (cleared_split_ids or [])]
    begin_raw = con.execute("SELECT COALESCE(SUM(amount_cents),0) s FROM splits "
                            "WHERE account_id=? AND reconciled_id IS NOT NULL", (account_id,)).fetchone()["s"]
    add_raw = 0
    if ids:
        qmarks = ",".join("?" * len(ids))
        add_raw = con.execute(
            f"SELECT COALESCE(SUM(amount_cents),0) s FROM splits "
            f"WHERE account_id=? AND reconciled_id IS NULL AND id IN ({qmarks})",
            [account_id] + ids).fetchone()["s"]
    cleared = ledger.display_balance(a["type"], begin_raw + add_raw)
    diff = statement_balance_cents - cleared
    cur = con.execute(
        "INSERT INTO reconciliations(account_id,statement_date,statement_balance_cents,"
        "book_balance_cents,difference_cents) VALUES(?,?,?,?,?)",
        (account_id, statement_date, statement_balance_cents, cleared, diff))
    rid = cur.lastrowid
    if ids:
        qmarks = ",".join("?" * len(ids))
        con.execute(f"UPDATE splits SET reconciled_id=? WHERE account_id=? AND reconciled_id IS NULL "
                    f"AND id IN ({qmarks})", [rid, account_id] + ids)
    return {"reconciliation_id": rid, "reconciled": diff == 0, "difference": diff,
            "cleared_balance": cleared, "cleared_count": len(ids)}


def status(con):
    """Per active bank/card account: current book balance and last-reconciliation state, for
    the Reconcile overview and health checks. `out_of_balance` = last check had a nonzero diff."""
    accts = con.execute(
        "SELECT id, name, type, kind FROM accounts WHERE active=1 AND kind IN ('bank','card') "
        "ORDER BY type, name").fetchall()
    out = []
    for a in accts:
        last = last_reconciliation(con, a["id"])
        since = last["statement_date"] if last else None
        if since:
            n = con.execute("SELECT COUNT(DISTINCT e.id) c FROM entries e JOIN splits s ON s.entry_id=e.id "
                            "WHERE s.account_id=? AND e.date>?", (a["id"], since)).fetchone()["c"]
        else:
            n = con.execute("SELECT COUNT(DISTINCT e.id) c FROM entries e JOIN splits s ON s.entry_id=e.id "
                            "WHERE s.account_id=?", (a["id"],)).fetchone()["c"]
        out.append({
            "id": a["id"], "name": a["name"], "kind": a["kind"],
            "book_balance": _book_balance(con, a),
            "last_date": since,
            "last_difference": last["difference_cents"] if last else None,
            "reconciled": bool(last) and last["difference_cents"] == 0,
            "out_of_balance": bool(last) and last["difference_cents"] != 0,
            "never_reconciled": last is None,
            "activity_since": n,
        })
    return out
