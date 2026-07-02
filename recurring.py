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


# --- auto-detection: suggest templates from posted history ---------------------

# median-gap bands (days) that map to a supported frequency; nominal drives the staleness cut
_FREQ_BANDS = (("weekly", 7, 5, 10), ("monthly", 30, 24, 35), ("yearly", 365, 330, 400))


def _classify_cadence(gaps):
    """Map a list of day-gaps between occurrences to a supported frequency, or None if the
    spacing is irregular. Requires the median gap to sit in a band AND most (>=70%) of the
    individual gaps to sit in that same band — one skipped month shouldn't disqualify rent,
    but random shopping at the same store shouldn't look like a subscription."""
    if not gaps:
        return None
    med = sorted(gaps)[len(gaps) // 2]
    for freq, nominal, lo, hi in _FREQ_BANDS:
        if lo <= med <= hi:
            in_band = sum(1 for g in gaps if lo <= g <= hi)
            if in_band >= max(1, round(len(gaps) * 0.7)):
                return freq, nominal
            return None
    return None


def detect_candidates(con, today=None, lookback_months=12, min_occurrences=3):
    """Scan posted history for bills/income that repeat on a regular cadence and suggest recurring
    templates for them. Deterministic (no AI) and advisory — nothing is created here; the Recurring
    page offers each candidate as a one-click Create.

    Looks at 2-split entries with exactly one bank/card leg and one real category leg (which
    naturally excludes transfers and Uncategorized Expense), grouped by normalized vendor
    (importer.payee_key: 'RENT 03/01' and 'RENT 04/01' group together) + category + account.
    A group qualifies with >= min_occurrences on a regular weekly/monthly/yearly cadence whose
    latest occurrence is recent (a pattern that stopped isn't suggested). Patterns that already
    have a template (by vendor key + account + category) are skipped, so a suggestion disappears
    once created."""
    import importer  # lazy: keeps recurring light for callers that never detect
    today = datetime.strptime(today, "%Y-%m-%d").date() if today else date.today()
    y, m = today.year, today.month - lookback_months
    while m <= 0:
        m += 12
        y -= 1
    start = date(y, m, min(today.day, monthrange(y, m)[1])).isoformat()

    rows = con.execute(
        "SELECT e.date, e.payee, ABS(s_cat.amount_cents) amount, "
        "  cat.id cat_id, cat.name cat_name, cat.type cat_type, "
        "  acct.id acct_id, acct.name acct_name "
        "FROM entries e "
        "JOIN splits s_cat ON s_cat.entry_id=e.id "
        "JOIN accounts cat ON cat.id=s_cat.account_id AND cat.type IN ('income','expense') "
        "  AND cat.name != 'Uncategorized Expense' "
        "JOIN splits s_acct ON s_acct.entry_id=e.id AND s_acct.id != s_cat.id "
        "JOIN accounts acct ON acct.id=s_acct.account_id AND acct.kind IN ('bank','card') "
        "WHERE e.date >= ? AND e.date <= ? "
        "  AND (SELECT COUNT(*) FROM splits WHERE entry_id=e.id) = 2 "
        "ORDER BY e.date", (start, today.isoformat())).fetchall()

    groups = {}
    for r in rows:
        key = (importer.payee_key(r["payee"]), r["cat_id"], r["acct_id"])
        g = groups.setdefault(key, {"dates": {}, "payee": r["payee"], "cat_name": r["cat_name"],
                                    "cat_type": r["cat_type"], "acct_name": r["acct_name"]})
        g["dates"].setdefault(r["date"], r["amount"])  # one occurrence per date
        g["payee"] = r["payee"]                        # keep the freshest raw payee as the name

    # a template (active or paused) suppresses matching suggestions — paused means "I decided"
    templated = {(importer.payee_key(t["name"]), t["category_id"], t["account_id"])
                 for t in con.execute("SELECT name, category_id, account_id FROM recurring").fetchall()}

    out = []
    for (vkey, cat_id, acct_id), g in groups.items():
        if not vkey or (vkey, cat_id, acct_id) in templated:
            continue
        dates = sorted(g["dates"])
        if len(dates) < min_occurrences:
            continue
        ds = [datetime.strptime(x, "%Y-%m-%d").date() for x in dates]
        cadence = _classify_cadence([(b - a).days for a, b in zip(ds, ds[1:])])
        if not cadence:
            continue
        freq, nominal = cadence
        if (today - ds[-1]).days > nominal * 1.8:
            continue  # the pattern appears to have stopped — don't suggest a dead bill
        amounts = sorted(g["dates"].values())
        out.append({
            "name": g["payee"], "amount_cents": amounts[len(amounts) // 2],
            "flow": "income" if g["cat_type"] == "income" else "expense",
            "account_id": acct_id, "account_name": g["acct_name"],
            "category_id": cat_id, "category_name": g["cat_name"],
            "frequency": freq, "occurrences": len(dates),
            "last_date": dates[-1], "next_date": advance(dates[-1], freq),
        })
    out.sort(key=lambda c: (-c["occurrences"], c["name"]))
    return out


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
