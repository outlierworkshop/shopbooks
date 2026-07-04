"""Bank feeds via SimpleFIN Bridge (simplefin.org): pull transactions straight from the banks
into the existing import pipeline.

How it fits the app's constraints:
  - Bank credentials NEVER touch ShopBooks. The owner connects banks on the bridge's site and pastes
    a one-time SETUP TOKEN here; claiming it yields a durable read-only ACCESS URL (stored like the
    other secrets in settings; revocable anytime from the bridge dashboard).
  - Fetched transactions land as PENDING rows in Review — through the exact same staging path as a
    statement import (rules/history/AI categorization + transfer pairing) — so nothing ever posts
    without the owner confirming it.
  - No daemon: fetching is a button. The bridge refreshes bank data ~daily and allows ~24 requests a
    day, so one GET per click covers every account.

Protocol (v1): setup token = base64(claim URL); POST the claim URL once -> access URL with basic-auth
embedded; GET {access}/accounts?start-date=<unix> -> {"accounts":[{id, name, org:{name}, transactions:
[{id, posted, amount (signed decimal string), description, pending?}]}]}. Amounts are from the
account's balance perspective (deposit +, card charge -); staged rows use positive = money out, so
staged_amount = -amount.
"""
import base64
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit

import db
import importer

OVERLAP_DAYS = 7      # refetch window overlap; feed_txns ids absorb the duplicates
FIRST_FETCH_DAYS = 30
CROSS_SOURCE_DAYS = 3  # a feed txn within this many days of a same-account, same-amount statement
                       # row/entry is treated as the same transaction (feeds and statements often
                       # disagree on posting date by a day or two)


# ---------------------------------------------------------------- HTTP layer (mockable)

def _http_post(url):
    import httpx
    r = httpx.post(url, timeout=30)
    r.raise_for_status()
    return r.text


def _http_get_json(url, params):
    import httpx
    parts = urlsplit(url)
    auth = (parts.username or "", parts.password or "")
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    clean = urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    r = httpx.get(clean, params=params, auth=auth, timeout=60)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- connection

def access_url(con):
    return db.get_setting(con, "simplefin_access_url", "")


def connected(con):
    return bool(access_url(con))


def claim_setup_token(token):
    """Exchange a one-time SimpleFIN setup token for the durable access URL. Raises ValueError with a
    readable message on a garbled or already-used token."""
    token = "".join(str(token or "").split())
    if not token:
        raise ValueError("Paste the setup token from bridge.simplefin.org first.")
    try:
        claim = base64.b64decode(token, validate=True).decode("utf-8")
    except Exception:
        raise ValueError("That doesn't look like a SimpleFIN setup token (it should be one long "
                         "block of letters/numbers copied from the bridge).")
    if not claim.startswith("http"):
        raise ValueError("That token didn't decode to a claim URL — copy it fresh from the bridge.")
    try:
        url = _http_post(claim).strip()
    except Exception as e:
        raise ValueError(f"The bridge rejected the token (setup tokens are one-use — generate a new "
                         f"one if this was already claimed): {e}")
    if not url.startswith("http"):
        raise ValueError("The bridge returned something unexpected — try a freshly generated token.")
    return url


# ---------------------------------------------------------------- fetching

def _payload(con, start_ts):
    return _http_get_json(access_url(con) + "/accounts", {"start-date": int(start_ts)})


def _upsert_feed_accounts(con, payload):
    """Make sure every account the bridge reports has a feed_accounts row (new ones unmapped)."""
    for a in payload.get("accounts", []):
        org = (a.get("org") or {}).get("name", "") or ""
        row = con.execute("SELECT id FROM feed_accounts WHERE id=?", (a["id"],)).fetchone()
        if row:
            con.execute("UPDATE feed_accounts SET name=?, org_name=? WHERE id=?", (a["name"], org, a["id"]))
        else:
            con.execute("INSERT INTO feed_accounts(id,name,org_name) VALUES(?,?,?)", (a["id"], a["name"], org))


def refresh_accounts(con):
    """Fetch the current account list from the bridge (1-day window; transactions ignored) so newly
    connected banks show up for mapping. Returns how many accounts the bridge reports."""
    payload = _payload(con, datetime.now(tz=timezone.utc).timestamp() - 86400)
    _upsert_feed_accounts(con, payload)
    return len(payload.get("accounts", []))


def list_feed_accounts(con):
    return con.execute(
        "SELECT f.*, a.name mapped_name FROM feed_accounts f "
        "LEFT JOIN accounts a ON a.id=f.account_id ORDER BY f.org_name, f.name").fetchall()


def _amount_cents(amount_str):
    """SimpleFIN amount (signed decimal string, balance perspective) -> staged cents
    (positive = money out). A deposit '+100.00' becomes -10000; a card charge '-42.50' becomes 4250."""
    try:
        return -int(Decimal(str(amount_str)) * 100)
    except (InvalidOperation, TypeError):
        raise ValueError(f"unreadable amount from feed: {amount_str!r}")


def _txn_date(posted_ts):
    return datetime.fromtimestamp(int(posted_ts), tz=timezone.utc).date().isoformat()


def _already_on_books(con, account_id, d, cents):
    """Cross-source guard: True if this ShopBooks account already has this transaction from a
    statement import — same amount within CROSS_SOURCE_DAYS of the same date, in the staged queue
    (any status) or posted to the ledger. The date window (not an exact-date match) is deliberate:
    a feed and a statement routinely post the same charge a day or two apart, so an exact-date guard
    would let the feed twin slip through and double-post. Rare same-amount-near-date false skips are
    acceptable and visible in Review; the /duplicates report catches anything that still slips."""
    if con.execute("SELECT 1 FROM staged s JOIN batches b ON b.id=s.batch_id "
                   "WHERE b.account_id=? AND s.amount_cents=? "
                   "AND abs(julianday(s.date)-julianday(?))<=? LIMIT 1",
                   (account_id, cents, d, CROSS_SOURCE_DAYS)).fetchone():
        return True
    return bool(con.execute(
        "SELECT 1 FROM entries e JOIN splits s ON s.entry_id=e.id "
        "WHERE s.account_id=? AND s.amount_cents=? "
        "AND abs(julianday(e.date)-julianday(?))<=? LIMIT 1",
        (account_id, -cents, d, CROSS_SOURCE_DAYS)).fetchone())


def _start_date(con):
    """One request covers all accounts: start at the oldest mapped account's window."""
    today = date.today()
    starts = []
    for r in con.execute("SELECT last_synced FROM feed_accounts "
                         "WHERE enabled=1 AND account_id IS NOT NULL").fetchall():
        if r["last_synced"]:
            starts.append(date.fromisoformat(r["last_synced"]) - timedelta(days=OVERLAP_DAYS))
        else:
            starts.append(today - timedelta(days=FIRST_FETCH_DAYS))
    begin = min(starts) if starts else today - timedelta(days=FIRST_FETCH_DAYS)
    return datetime(begin.year, begin.month, begin.day, tzinfo=timezone.utc).timestamp()


def fetch(con, categorize=None):
    """Pull posted transactions for every enabled+mapped feed account and stage them for Review.
    `categorize(con, txns)` is an optional callback returning AI category names (the route passes the
    same recipe the statement import uses). Returns a summary dict; raises ValueError when not
    connected, and lets network errors bubble to the route (shown as a friendly message)."""
    if not connected(con):
        raise ValueError("Bank feeds aren't connected — paste a setup token in Settings first.")
    payload = _payload(con, _start_date(con))
    _upsert_feed_accounts(con, payload)

    today = date.today().isoformat()
    staged_total = skipped = 0
    per_account, unmapped = [], []
    for a in payload.get("accounts", []):
        row = con.execute("SELECT * FROM feed_accounts WHERE id=?", (a["id"],)).fetchone()
        if not row or not row["account_id"] or not row["enabled"]:
            unmapped.append(f"{(a.get('org') or {}).get('name', '')} {a['name']}".strip())
            continue
        acct_id = row["account_id"]
        new = []
        for t in a.get("transactions", []):
            if t.get("pending"):
                continue
            if con.execute("SELECT 1 FROM feed_txns WHERE id=?", (str(t["id"]),)).fetchone():
                skipped += 1
                continue
            d = _txn_date(t["posted"])
            cents = _amount_cents(t["amount"])
            if _already_on_books(con, acct_id, d, cents):
                con.execute("INSERT OR IGNORE INTO feed_txns(id) VALUES(?)", (str(t["id"]),))
                skipped += 1
                continue
            new.append({"feed_id": str(t["id"]), "date": d,
                        "description": str(t.get("description") or "").strip() or "(no description)",
                        "amount_cents": cents})
        if new:
            new.sort(key=lambda t: t["date"])
            cur = con.execute("INSERT INTO batches(filename,account_id) VALUES(?,?)",
                              (f"feed:{row['name']} {today}", acct_id))
            batch_id = cur.lastrowid
            cats = {r2["id"]: r2["name"] for r2 in con.execute(
                "SELECT id, name FROM accounts WHERE active=1 AND type IN ('expense','income')").fetchall()}
            ai_cats = categorize(con, new) if categorize else None
            importer.stage_transactions(con, batch_id, new, acct_id, cats, ai_cats)
            staged_ids = [r2["id"] for r2 in con.execute(
                "SELECT id FROM staged WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()]
            for t, sid in zip(new, staged_ids):
                con.execute("INSERT OR IGNORE INTO feed_txns(id,staged_id) VALUES(?,?)", (t["feed_id"], sid))
            staged_total += len(new)
        con.execute("UPDATE feed_accounts SET last_synced=? WHERE id=?", (today, a["id"]))
        per_account.append({"name": row["name"], "new": len(new)})
    return {"staged": staged_total, "skipped": skipped, "accounts": per_account, "unmapped": unmapped}
