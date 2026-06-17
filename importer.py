"""Statement importing: CSV parsing, PDF text extraction, regex fallback, rules engine."""
import csv
import io
import re
from datetime import date, datetime

from ledger import normalize_date, parse_amount_to_cents


# ---------------------------------------------------------------- year correction

def _make_date(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None  # e.g. Feb 29 in a non-leap year


def _safe_anchor(end_date_str, today):
    """The statement's closing date, used to assign years. Falls back to today if the
    closing date is missing, unparseable, or implausible (e.g. an AI-hallucinated future date)."""
    try:
        a = datetime.strptime(str(end_date_str).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return today
    return today if (a > today or a.year < 2000) else a


def reconcile_years(txns, statement_end_date="", today=None):
    """Re-derive every transaction's YEAR from the statement period.

    Statement lines usually show only MM/DD; the year is only in the header. We keep the
    month/day the model read off each line and recompute the year from the closing date
    ('most recent MM/DD on or before the closing date', which handles Dec->Jan rollover),
    ignoring whatever year the model guessed. A transaction can never be dated in the future.
    Mutates and returns txns.
    """
    today = today or date.today()
    anchor = _safe_anchor(statement_end_date, today)
    for t in txns:
        try:
            d = datetime.strptime(str(t.get("date", "")), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        m, day = d.month, d.day
        year = anchor.year if (m, day) <= (anchor.month, anchor.day) else anchor.year - 1
        nd = _make_date(year, m, day)
        if nd and nd > today:  # guardrail: never the future
            nd = _make_date(year - 1, m, day)
        if nd:
            t["date"] = nd.isoformat()
    return txns


def clamp_future_dates(txns, today=None):
    """Lighter safety net (used on the regex fallback, which keeps its own year): pull any
    future-dated transaction back by whole years until it's no longer in the future."""
    today = today or date.today()
    for t in txns:
        try:
            d = datetime.strptime(str(t.get("date", "")), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        while d > today:
            nd = _make_date(d.year - 1, d.month, d.day)
            if not nd:
                break
            d = nd
        t["date"] = d.isoformat()
    return txns



DATE_HEADERS = ("transaction date", "posted date", "post date", "posting date", "date")
DESC_HEADERS = ("description", "payee", "merchant", "name", "details", "memo")
AMOUNT_HEADERS = ("amount", "transaction amount")
DEBIT_HEADERS = ("debit", "withdrawal", "withdrawals", "money out")
CREDIT_HEADERS = ("credit", "deposit", "deposits", "money in")


def _find_col(headers, candidates):
    lowered = [h.strip().lower() for h in headers]
    for cand in candidates:
        for i, h in enumerate(lowered):
            if h == cand:
                return i
    for cand in candidates:
        for i, h in enumerate(lowered):
            if cand in h:
                return i
    return None


def parse_csv(raw_bytes):
    """Parse a bank/card CSV export. Returns list of {date, description, amount_cents}.

    Single signed amount columns are assumed to use the common bank convention
    (negative = money out); we flip to our convention (positive = money out).
    The review screen has a flip-signs button if a particular bank disagrees.
    """
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    headers = rows[0]
    di = _find_col(headers, DATE_HEADERS)
    de = _find_col(headers, DESC_HEADERS)
    am = _find_col(headers, AMOUNT_HEADERS)
    db_ = _find_col(headers, DEBIT_HEADERS)
    cr = _find_col(headers, CREDIT_HEADERS)
    if di is None or de is None or (am is None and db_ is None and cr is None):
        raise ValueError("Couldn't find date/description/amount columns in this CSV.")
    out = []
    for r in rows[1:]:
        try:
            date = normalize_date(r[di])
        except (ValueError, IndexError):
            continue
        desc = r[de].strip() if de < len(r) else ""
        try:
            if am is not None and r[am].strip():
                cents = -parse_amount_to_cents(r[am])  # bank convention: negative = money out
            else:
                d = parse_amount_to_cents(r[db_]) if db_ is not None and db_ < len(r) and r[db_].strip() else 0
                c = parse_amount_to_cents(r[cr]) if cr is not None and cr < len(r) and r[cr].strip() else 0
                cents = abs(d) - abs(c)
        except (ValueError, IndexError):
            continue
        if desc or cents:
            out.append({"date": date, "description": desc, "amount_cents": cents})
    return out


def _amazon_date(raw):
    """Amazon dates come as ISO with a time ('2026-01-15T08:30:00Z') or plain dates."""
    s = str(raw).strip().replace("T", " ").split(" ")[0]
    return normalize_date(s)


def parse_amazon_orders(raw_bytes):
    """Parse an Amazon order-history CSV into a list of orders (no AI; deterministic).

    Handles the variants Amazon ships: the newer 'Request My Data -> Your Orders'
    (Retail.OrderHistory.*.csv) and the older Order Reports. Item rows are grouped by
    Order ID and summed to an order total. Returns
    [{date, order_id, total_cents, items: [names]}] sorted by date.
    """
    date_h = ("order date", "date")
    id_h = ("order id", "order #", "order number")
    name_h = ("product name", "title", "item name", "product")
    total_h = ("total owed", "item total", "item subtotal", "total charged", "amount")

    text = raw_bytes.decode("utf-8-sig", errors="replace")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("That file is empty.")
    # find the header row (Amazon sometimes prefixes a title line)
    hi = di = oi = ti = ni = None
    for idx, row in enumerate(rows[:5]):
        d, o, t = _find_col(row, date_h), _find_col(row, id_h), _find_col(row, total_h)
        if d is not None and o is not None and t is not None:
            hi, di, oi, ti, ni = idx, d, o, t, _find_col(row, name_h)
            break
    if hi is None:
        raise ValueError("Couldn't find Amazon columns (need Order Date, Order ID, and a total). "
                         "Use Amazon -> Account -> Request My Data -> 'Your Orders'.")

    orders = {}
    for r in rows[hi + 1:]:
        if max(di, oi, ti) >= len(r):
            continue
        oid = r[oi].strip()
        if not oid:
            continue
        try:
            d = _amazon_date(r[di])
            cents = parse_amount_to_cents(r[ti])
        except (ValueError, IndexError):
            continue
        o = orders.setdefault(oid, {"date": d, "order_id": oid, "total_cents": 0, "items": []})
        o["total_cents"] += cents
        o["date"] = min(o["date"], d)  # earliest line date for the order
        if ni is not None and ni < len(r) and r[ni].strip():
            o["items"].append(r[ni].strip())
    out = [o for o in orders.values() if o["total_cents"] != 0]
    if not out:
        raise ValueError("No Amazon orders with a total were found in that file.")
    out.sort(key=lambda o: o["date"])
    return out


def pdf_text(path):
    import pdfplumber
    chunks = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            chunks.append(page.extract_text() or "")
    return "\n".join(chunks)


# Fallback regex for text-based statements: "MM/DD  DESCRIPTION ...  1,234.56"
LINE_RE = re.compile(
    r"^(?P<date>\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+(?P<desc>.+?)\s+(?P<amt>-?\(?\$?[\d,]+\.\d{2}\)?-?)\s*$")
YEAR_RE = re.compile(r"\b(20\d{2})\b")


def regex_parse_statement(text):
    year = None
    m = YEAR_RE.search(text)
    if m:
        year = m.group(1)
    out = []
    for line in text.splitlines():
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        d = m.group("date")
        if d.count("/") == 1:
            if not year:
                continue
            d = f"{d}/{year}"
        try:
            normalized = normalize_date(d)
            cents = parse_amount_to_cents(m.group("amt"))
        except ValueError:
            continue
        out.append({"date": normalized, "description": m.group("desc").strip(), "amount_cents": cents})
    return clamp_future_dates(out)


def apply_rules(con, description):
    desc = description.upper()
    for r in con.execute("SELECT * FROM rules ORDER BY length(pattern) DESC, id"):
        if r["pattern"].upper() in desc:
            return r["account_id"]
    return None


def possible_duplicate(con, source_account_id, date, amount_cents):
    """True if a posted entry already hits this source account with the same amount within 7 days."""
    row = con.execute(
        "SELECT 1 FROM splits s JOIN entries e ON e.id=s.entry_id "
        "WHERE s.account_id=? AND s.amount_cents=? AND abs(julianday(e.date)-julianday(?))<=7 LIMIT 1",
        (source_account_id, -amount_cents, date)).fetchone()
    return row is not None


# ---------------------------------------------------------------- transfers (CC payments)

TRANSFER_WINDOW = 7  # days


def find_pending_partner(con, source_account_id, amount_cents, date, exclude_id, window=TRANSFER_WINDOW):
    """The other (pending) side of a credit-card payment, if present in the Review queue.

    A CC payment is money OUT of a bank account (positive staged amount) meeting money IN to a
    card (negative staged amount of equal size) within `window` days. Direction is enforced so
    an unrelated deposit + same-size card charge are NOT mistaken for a transfer.
    Returns the partner staged row (id, account_id = its source account) or None.
    """
    src = con.execute("SELECT kind FROM accounts WHERE id=?", (source_account_id,)).fetchone()
    if not src:
        return None
    if src["kind"] == "bank" and amount_cents > 0:
        partner_kind = "card"
    elif src["kind"] == "card" and amount_cents < 0:
        partner_kind = "bank"
    else:
        return None
    return con.execute(
        "SELECT st.id, b.account_id FROM staged st JOIN batches b ON b.id=st.batch_id "
        "JOIN accounts a ON a.id=b.account_id "
        "WHERE st.status='pending' AND st.id!=? AND b.account_id!=? AND a.kind=? "
        "AND st.amount_cents=? AND abs(julianday(st.date)-julianday(?))<=? "
        "ORDER BY abs(julianday(st.date)-julianday(?)) LIMIT 1",
        (exclude_id, source_account_id, partner_kind, -amount_cents, date, window, date)).fetchone()


def find_posted_transfer(con, source_account_id, amount_cents, date, window=TRANSFER_WINDOW):
    """If this row's transfer is ALREADY booked from the other statement, return the other own
    account's id (so the row can be labelled and skipped); else None. Matches only genuine
    transfers (a posted entry whose BOTH legs are bank/card accounts), never normal expenses."""
    row = con.execute(
        "SELECT s2.account_id FROM entries e "
        "JOIN splits s1 ON s1.entry_id=e.id AND s1.account_id=? AND s1.amount_cents=? "
        "JOIN splits s2 ON s2.entry_id=e.id AND s2.account_id!=s1.account_id "
        "JOIN accounts a2 ON a2.id=s2.account_id AND a2.kind IN ('bank','card') "
        "WHERE abs(julianday(e.date)-julianday(?))<=? LIMIT 1",
        (source_account_id, -amount_cents, date, window)).fetchone()
    return row["account_id"] if row else None


def pair_transfers(con, batch_id, source_account_id):
    """After staging a batch, auto-categorize credit-card payments as transfers: set the
    category to the matching own account so posting books a transfer (not an expense)."""
    for row in con.execute("SELECT * FROM staged WHERE batch_id=? AND status='pending'", (batch_id,)).fetchall():
        partner = find_pending_partner(con, source_account_id, row["amount_cents"], row["date"], row["id"])
        if partner:
            con.execute("UPDATE staged SET category_id=? WHERE id=?", (partner["account_id"], row["id"]))
            con.execute("UPDATE staged SET category_id=? WHERE id=? AND status='pending'",
                        (source_account_id, partner["id"]))
            continue
        booked = find_posted_transfer(con, source_account_id, row["amount_cents"], row["date"])
        if booked is not None:
            con.execute("UPDATE staged SET category_id=? WHERE id=?", (booked, row["id"]))


def stage_transactions(con, batch_id, txns, source_account_id, category_names_by_id, ai_categories=None):
    """Insert staged rows, auto-categorizing via rules first, AI suggestions second, then pair
    any credit-card-payment transfers between the user's own accounts."""
    name_to_id = {v: k for k, v in category_names_by_id.items()}
    for i, t in enumerate(txns):
        cat_id = apply_rules(con, t["description"])
        if cat_id is None and ai_categories:
            cat_id = name_to_id.get(ai_categories[i])
        con.execute(
            "INSERT INTO staged(batch_id,date,description,amount_cents,category_id) VALUES(?,?,?,?,?)",
            (batch_id, t["date"], t["description"], t["amount_cents"], cat_id))
    pair_transfers(con, batch_id, source_account_id)
