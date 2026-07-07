"""Global search: one query -> matches grouped by entity type, each linking to a detail page.

Text is matched case-insensitively (SQLite LIKE is case-insensitive for ASCII). A query that looks
like a money amount ALSO matches on abs(amount_cents), because signs differ across tables (splits are
signed; staged positive = money out; documents positive). Every value is a bound parameter, so the
query is injection-safe. Read-only; each group is capped at LIMIT.
"""
import re

import invoicing
import ledger

LIMIT = 50
_AMOUNT_RE = re.compile(r"^\$?\(?-?[\d,]*\.?\d+\)?$")


def _as_cents(q):
    """abs cents if q looks like a money amount, else None (used only as an extra match, never
    replacing the text search)."""
    if not _AMOUNT_RE.match(q):
        return None
    try:
        return abs(ledger.parse_amount_to_cents(q))
    except Exception:
        return None


def run(con, q):
    """Return a dict of result lists (one per entity type) plus 'total'. Empty if q is blank."""
    q = (q or "").strip()
    out = {k: [] for k in ("transactions", "invoices", "customers", "receipts",
                           "accounts", "review", "jobs", "mileage")}
    out["total"] = 0
    if not q:
        return out
    like = f"%{q}%"
    amt = _as_cents(q)
    amt = amt if amt is not None else -1   # abs() is never negative, so -1 matches nothing

    # Transactions (posted): match payee/memo, or any split amount. One row per entry, linking to the
    # bank/card leg's register (falls back to the first account) so the row lands somewhere useful.
    out["transactions"] = [dict(r) for r in con.execute(
        "SELECT e.id AS entry_id, e.date, e.payee, e.memo, "
        "  (SELECT a.id   FROM splits s JOIN accounts a ON a.id=s.account_id WHERE s.entry_id=e.id "
        "     ORDER BY (a.kind IN ('bank','card')) DESC, s.id LIMIT 1) AS acct_id, "
        "  (SELECT a.name FROM splits s JOIN accounts a ON a.id=s.account_id WHERE s.entry_id=e.id "
        "     ORDER BY (a.kind IN ('bank','card')) DESC, s.id LIMIT 1) AS acct, "
        "  (SELECT s.amount_cents FROM splits s JOIN accounts a ON a.id=s.account_id WHERE s.entry_id=e.id "
        "     ORDER BY (a.kind IN ('bank','card')) DESC, s.id LIMIT 1) AS amount_cents "
        "FROM entries e "
        "WHERE e.payee LIKE ? OR e.memo LIKE ? OR e.id IN (SELECT entry_id FROM splits WHERE abs(amount_cents)=?) "
        "ORDER BY e.date DESC, e.id DESC LIMIT ?", (like, like, amt, LIMIT)).fetchall()]

    # Customers
    out["customers"] = [dict(r) for r in con.execute(
        "SELECT id, name, email, phone FROM customers WHERE name LIKE ? OR email LIKE ? OR phone LIKE ? "
        "ORDER BY name LIMIT ?", (like, like, like, LIMIT)).fetchall()]

    # Invoices & estimates: text on number/memo, plus amount via computed totals (no stored column).
    inv, seen = [], set()
    for r in con.execute(
            "SELECT i.id, i.number, i.kind, i.date, i.status, c.name AS customer "
            "FROM invoices i JOIN customers c ON c.id=i.customer_id "
            "WHERE i.number LIKE ? OR i.memo LIKE ? ORDER BY i.date DESC LIMIT ?", (like, like, LIMIT)).fetchall():
        d = dict(r); d["total_cents"] = invoicing.invoice_total(con, r["id"]); inv.append(d); seen.add(r["id"])
    if amt >= 0 and len(inv) < LIMIT:
        for r in con.execute(
                "SELECT i.id, i.number, i.kind, i.date, i.status, c.name AS customer "
                "FROM invoices i JOIN customers c ON c.id=i.customer_id ORDER BY i.date DESC").fetchall():
            if r["id"] in seen:
                continue
            if abs(invoicing.invoice_total(con, r["id"])) == amt:
                d = dict(r); d["total_cents"] = invoicing.invoice_total(con, r["id"]); inv.append(d)
                if len(inv) >= LIMIT:
                    break
    out["invoices"] = inv

    # Receipts (documents)
    out["receipts"] = [dict(r) for r in con.execute(
        "SELECT id, vendor, doc_date, amount_cents, status FROM documents "
        "WHERE vendor LIKE ? OR abs(amount_cents)=? ORDER BY doc_date DESC LIMIT ?", (like, amt, LIMIT)).fetchall()]

    # Accounts (chart of accounts)
    out["accounts"] = [dict(r) for r in con.execute(
        "SELECT id, name, type, kind, active FROM accounts WHERE name LIKE ? ORDER BY name LIMIT ?",
        (like, LIMIT)).fetchall()]

    # Review queue (unposted staged lines)
    out["review"] = [dict(r) for r in con.execute(
        "SELECT s.id, s.date, s.description, s.amount_cents, src.name AS acct "
        "FROM staged s JOIN batches b ON b.id=s.batch_id JOIN accounts src ON src.id=b.account_id "
        "WHERE s.status='pending' AND (s.description LIKE ? OR s.memo LIKE ? OR abs(s.amount_cents)=?) "
        "ORDER BY s.date DESC LIMIT ?", (like, like, amt, LIMIT)).fetchall()]

    # Jobs
    out["jobs"] = [dict(r) for r in con.execute(
        "SELECT id, name, status FROM jobs WHERE name LIKE ? OR notes LIKE ? ORDER BY name LIMIT ?",
        (like, like, LIMIT)).fetchall()]

    # Mileage
    out["mileage"] = [dict(r) for r in con.execute(
        "SELECT id, date, miles, purpose, from_loc, to_loc FROM mileage "
        "WHERE purpose LIKE ? OR from_loc LIKE ? OR to_loc LIKE ? ORDER BY date DESC LIMIT ?",
        (like, like, like, LIMIT)).fetchall()]

    out["total"] = sum(len(out[k]) for k in out if k != "total")
    return out


def suggest(con, q, cap=10):
    """A small, fast flat list of top matches for the type-ahead dropdown. Each item is
    {type, label, sub, url}. Deliberately lighter than run(): small LIMITs and text-only invoice
    match (no per-invoice total computation) so it's cheap on every keystroke."""
    q = (q or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    amt = _as_cents(q)
    amt = amt if amt is not None else -1
    items = []

    def money(c):
        return "$" + ledger.fmt_cents(c) if c is not None else ""

    for r in con.execute(
            "SELECT e.id AS entry_id, e.date, e.payee, "
            "  (SELECT a.id FROM splits s JOIN accounts a ON a.id=s.account_id WHERE s.entry_id=e.id "
            "     ORDER BY (a.kind IN ('bank','card')) DESC, s.id LIMIT 1) AS acct_id, "
            "  (SELECT s.amount_cents FROM splits s JOIN accounts a ON a.id=s.account_id WHERE s.entry_id=e.id "
            "     ORDER BY (a.kind IN ('bank','card')) DESC, s.id LIMIT 1) AS amount_cents "
            "FROM entries e WHERE e.payee LIKE ? OR e.memo LIKE ? "
            "  OR e.id IN (SELECT entry_id FROM splits WHERE abs(amount_cents)=?) "
            "ORDER BY e.date DESC, e.id DESC LIMIT 4", (like, like, amt)).fetchall():
        items.append({"type": "Transaction", "label": r["payee"] or "(no payee)",
                      "sub": f"{r['date']} · {money(r['amount_cents'])}",
                      "url": f"/register/{r['acct_id']}#entry-{r['entry_id']}"})
    for r in con.execute("SELECT id, name, email FROM customers WHERE name LIKE ? OR email LIKE ? OR phone LIKE ? "
                         "ORDER BY name LIMIT 3", (like, like, like)).fetchall():
        items.append({"type": "Customer", "label": r["name"], "sub": r["email"] or "",
                      "url": f"/customers/{r['id']}"})
    for r in con.execute("SELECT i.id, i.number, i.kind, c.name AS customer FROM invoices i "
                         "JOIN customers c ON c.id=i.customer_id WHERE i.number LIKE ? OR i.memo LIKE ? "
                         "ORDER BY i.date DESC LIMIT 3", (like, like)).fetchall():
        items.append({"type": "Invoice", "label": r["number"], "sub": r["customer"],
                      "url": f"/{'estimates' if r['kind'] == 'estimate' else 'invoices'}/{r['id']}"})
    for r in con.execute("SELECT id, name, type FROM accounts WHERE name LIKE ? ORDER BY name LIMIT 3",
                         (like,)).fetchall():
        items.append({"type": "Account", "label": r["name"], "sub": r["type"], "url": f"/register/{r['id']}"})
    for r in con.execute("SELECT id, vendor, amount_cents FROM documents WHERE vendor LIKE ? OR abs(amount_cents)=? "
                         "ORDER BY doc_date DESC LIMIT 3", (like, amt)).fetchall():
        items.append({"type": "Receipt", "label": r["vendor"] or "(receipt)", "sub": money(r["amount_cents"]),
                      "url": f"/doc/{r['id']}"})
    for r in con.execute("SELECT id, name FROM jobs WHERE name LIKE ? OR notes LIKE ? ORDER BY name LIMIT 2",
                         (like, like)).fetchall():
        items.append({"type": "Job", "label": r["name"], "sub": "", "url": f"/jobs/{r['id']}"})
    return items[:cap]
