"""Possible-duplicate detection over POSTED ledger entries — the safety net behind the import-time
guards (feed txn ids, receipt hashes, statement filename/content, feeds' cross-source date+amount
check). Those all fire before staging; nothing re-examines the ledger afterward. This does: it finds
entries that look like the same transaction booked twice, so the owner can delete the extra.

Detection is deliberately conservative and NEVER deletes anything — a genuine repeat (two $20 ATM
withdrawals the same day) is indistinguishable from a double-post by amount alone, so this only
surfaces candidates for a human to confirm. Matching is anchored on the BANK/CARD leg (the register
the owner reconciles against a real statement): same account + same signed amount, dates within a
small window. Anchoring on the money-movement account also means each duplicate pair is reported once,
not twice (it would otherwise also collide on the shared expense/income category)."""
from datetime import date

import ledger

WINDOW_DAYS = 4  # two same-account, same-amount entries within this many days = a possible duplicate


def _counter_account(con, entry_id, account_id):
    """Name of the other side of the entry (the category, or '(split)' for a >2-leg entry), to help
    the owner judge whether two same-amount entries are really the same transaction."""
    others = con.execute(
        "SELECT a.name FROM splits s JOIN accounts a ON a.id=s.account_id "
        "WHERE s.entry_id=? AND s.account_id!=?", (entry_id, account_id)).fetchall()
    if not others:
        return ""
    return others[0]["name"] if len(others) == 1 else "(split)"


def find_duplicate_groups(con, window_days=WINDOW_DAYS):
    """Return groups of posted entries that may be duplicates of each other, most recent first.

    Each group: {account_id, account_name, account_type, amount_cents, amount_display,
                 entries: [{entry_id, date, payee, memo, counter}]}. A group always has >= 2 entries.
    Grouping: within one bank/card account and one exact signed cent amount, entries are clustered
    when each is within `window_days` of the previous one (chained), so a run of near-date repeats
    lands in a single group.
    """
    rows = con.execute(
        "SELECT s.account_id, s.amount_cents, e.id entry_id, e.date, e.payee, e.memo, "
        "a.name account_name, a.type account_type "
        "FROM splits s JOIN entries e ON e.id=s.entry_id JOIN accounts a ON a.id=s.account_id "
        "WHERE a.kind IN ('bank','card') "
        "ORDER BY s.account_id, s.amount_cents, e.date, e.id").fetchall()

    groups = []
    cluster = []  # list of rows sharing (account_id, amount_cents) and chained within the window

    def flush():
        if len(cluster) >= 2:
            first = cluster[0]
            groups.append({
                "account_id": first["account_id"],
                "account_name": first["account_name"],
                "account_type": first["account_type"],
                "amount_cents": first["amount_cents"],
                "amount_display": ledger.display_balance(first["account_type"], first["amount_cents"]),
                "entries": [{
                    "entry_id": r["entry_id"], "date": r["date"],
                    "payee": r["payee"], "memo": r["memo"],
                    "counter": _counter_account(con, r["entry_id"], r["account_id"]),
                } for r in cluster],
            })

    prev = None
    for r in rows:
        if prev is not None:
            same_bucket = (r["account_id"] == prev["account_id"] and r["amount_cents"] == prev["amount_cents"])
            close = same_bucket and _days_between(prev["date"], r["date"]) <= window_days
            if not close:
                flush()
                cluster = []
        cluster.append(r)
        prev = r
    flush()

    # most recent groups first (by the latest entry date in each group)
    groups.sort(key=lambda g: max(e["date"] for e in g["entries"]), reverse=True)
    return groups


def _days_between(d1, d2):
    try:
        return abs((date.fromisoformat(d2) - date.fromisoformat(d1)).days)
    except (ValueError, TypeError):
        return 99999  # unparseable date -> never cluster (safer to under-group than over-group)
