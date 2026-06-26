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


# Order-level total columns (one value per order, repeated on every item row) == the card charge.
# Preferred over item-level columns because order-level promos/adjustments make the item sum differ.
AMAZON_ORDER_TOTAL_H = ("order net total", "total amount", "payment amount", "order total", "grand total")
# Per-item total columns (consumer "Retail.OrderHistory" export has only these) -> summed per order.
AMAZON_ITEM_TOTAL_H = ("total owed", "item net total", "item total", "item subtotal")
AMAZON_DATE_H = ("order date", "date")
AMAZON_ID_H = ("order id", "order number", "order #")
AMAZON_NAME_H = ("title", "product name", "item name", "product")


def parse_amazon_orders(raw_bytes):
    """Parse an Amazon order-history CSV into a list of orders (no AI; deterministic).

    Handles both shapes Amazon ships:
      - Business/Order Reports: has an order-level total ('Order Net Total' / 'Total Amount' /
        'Payment Amount') repeated on each item row -> taken ONCE per order (this equals the card
        charge; item subtotals can differ due to order-level promos).
      - Consumer 'Request My Data -> Your Orders' (Retail.OrderHistory.*.csv): per-item totals
        only ('Total Owed') -> summed per order.
    Returns [{date, order_id, total_cents, items: [names]}] sorted by date.
    """
    try:
        text = raw_bytes.decode("utf-8-sig")          # most exports
    except UnicodeDecodeError:
        text = raw_bytes.decode("cp1252", errors="replace")  # some Amazon reports (™/® in titles)
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("That file is empty.")
    hi = di = oi = ni = order_col = item_col = None
    for idx, row in enumerate(rows[:5]):
        d, o = _find_col(row, AMAZON_DATE_H), _find_col(row, AMAZON_ID_H)
        oc, ic = _find_col(row, AMAZON_ORDER_TOTAL_H), _find_col(row, AMAZON_ITEM_TOTAL_H)
        if d is not None and o is not None and (oc is not None or ic is not None):
            hi, di, oi, ni, order_col, item_col = idx, d, o, _find_col(row, AMAZON_NAME_H), oc, ic
            break
    if hi is None:
        raise ValueError("Couldn't find Amazon columns (need Order Date, Order ID, and a total). "
                         "Use Amazon -> Account -> Request My Data -> 'Your Orders', or a Business order report.")
    total_col = order_col if order_col is not None else item_col
    by_order_level = order_col is not None

    orders = {}
    for r in rows[hi + 1:]:
        if max(di, oi, total_col) >= len(r):
            continue
        oid = r[oi].strip()
        if not oid:
            continue
        try:
            d = _amazon_date(r[di])
        except ValueError:
            continue
        try:
            cents = parse_amount_to_cents(r[total_col])
        except ValueError:
            cents = None
        o = orders.setdefault(oid, {"date": d, "order_id": oid, "total_cents": None, "items": []})
        o["date"] = min(o["date"], d)  # earliest line date for the order
        if ni is not None and ni < len(r) and r[ni].strip():
            o["items"].append(r[ni].strip())
        if cents is not None:
            if by_order_level:
                o["total_cents"] = cents          # identical on every row; take once
            else:
                o["total_cents"] = (o["total_cents"] or 0) + cents  # per-item; sum
    out = [o for o in orders.values() if o["total_cents"]]
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


def payee_key(description):
    """Normalize a bank descriptor to a stable vendor key for history matching:
    uppercase, drop punctuation and the digits banks tack on (store #, dates, txn ids),
    collapse spaces, cap length. 'AMAZON.COM*A12' and 'AMAZON.COM 999' both -> 'AMAZON COM'."""
    s = re.sub(r"[^A-Z0-9 ]", " ", str(description).upper())
    s = re.sub(r"\d+", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:24]


def history_map(con):
    """Learn from the user's own confirmed history: vendor key -> the category they've used
    most for it. Built from posted entries' income/expense legs (excludes Uncategorized and
    transfers, which hit bank/card accounts, not categories)."""
    tally = {}
    for r in con.execute(
            "SELECT e.payee, s.account_id, COUNT(*) n FROM entries e "
            "JOIN splits s ON s.entry_id=e.id JOIN accounts a ON a.id=s.account_id "
            "WHERE a.type IN ('income','expense') AND a.name!='Uncategorized Expense' "
            "GROUP BY e.payee, s.account_id").fetchall():
        k = payee_key(r["payee"])
        if k:
            tally.setdefault(k, {})[r["account_id"]] = tally.setdefault(k, {}).get(r["account_id"], 0) + r["n"]
    return {k: max(d, key=d.get) for k, d in tally.items()}


def history_category(con, description, hist=None):
    """The category this business has previously used for this vendor, or None."""
    hist = history_map(con) if hist is None else hist
    return hist.get(payee_key(description))


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


def rescan_transfers(con, window=TRANSFER_WINDOW):
    """Pair internal transfers across ALL pending rows - not just at import time.

    Finds equal-and-opposite amounts between two of the user's OWN bank/card accounts within
    `window` days and points each side's category at the other account, so posting books a single
    transfer (the second side auto-skips via the post-once logic in app._post_staged). Handles
    bank<->card credit-card payments AND bank<->bank / card<->card transfers. Greedy by nearest
    date so repeated identical amounts pair sensibly; each row is used at most once. Suggestions
    only - nothing posts. Returns the number of pairs matched.
    """
    rows = con.execute(
        "SELECT st.id, st.amount_cents, st.date, st.category_id, b.account_id AS acct_id "
        "FROM staged st JOIN batches b ON b.id=st.batch_id JOIN accounts a ON a.id=b.account_id "
        "WHERE st.status='pending' AND a.kind IN ('bank','card')").fetchall()
    outs = sorted((r for r in rows if r["amount_cents"] > 0), key=lambda r: (r["date"], r["id"]))
    ins = [r for r in rows if r["amount_cents"] < 0]
    used, paired_ids, pairs = set(), set(), 0
    # Pass 1: pair two PENDING sides of a transfer against each other.
    for o in outs:
        best, best_gap = None, None
        for n in ins:
            if n["id"] in used or n["acct_id"] == o["acct_id"] \
                    or n["amount_cents"] != -o["amount_cents"]:
                continue
            gap = abs((date.fromisoformat(n["date"]) - date.fromisoformat(o["date"])).days)
            if gap > window:
                continue
            if best is None or gap < best_gap:
                best, best_gap = n, gap
        if best is None:
            continue
        used.add(best["id"])
        paired_ids.update((o["id"], best["id"]))
        # Already paired to each other from a prior scan? Leave it, don't re-count.
        if o["category_id"] == best["acct_id"] and best["category_id"] == o["acct_id"]:
            continue
        con.execute("UPDATE staged SET category_id=? WHERE id=?", (best["acct_id"], o["id"]))
        con.execute("UPDATE staged SET category_id=? WHERE id=?", (o["acct_id"], best["id"]))
        pairs += 1
    # Pass 2: cross-import — a pending side whose transfer is ALREADY posted from the other
    # statement. Point it at the other own account so it books once (auto-skips on post).
    for r in rows:
        if r["id"] in paired_ids:
            continue
        other = find_posted_transfer(con, r["acct_id"], r["amount_cents"], r["date"], window)
        if other is not None and r["category_id"] != other:
            con.execute("UPDATE staged SET category_id=? WHERE id=?", (other, r["id"]))
            pairs += 1
    return pairs


def stage_transactions(con, batch_id, txns, source_account_id, category_names_by_id, ai_categories=None):
    """Insert staged rows, auto-categorizing via rules first, the user's own history second, AI
    suggestions third, then pair any transfers (CC payments, bank-to-bank) across the queue."""
    name_to_id = {v: k for k, v in category_names_by_id.items()}
    hist = history_map(con)
    for i, t in enumerate(txns):
        cat_id = apply_rules(con, t["description"])
        if cat_id is None:
            h = hist.get(payee_key(t["description"]))
            if h in category_names_by_id:  # only if it's a current, active category
                cat_id = h
        if cat_id is None and ai_categories:
            cat_id = name_to_id.get(ai_categories[i])
        con.execute(
            "INSERT INTO staged(batch_id,date,description,amount_cents,category_id) VALUES(?,?,?,?,?)",
            (batch_id, t["date"], t["description"], t["amount_cents"], cat_id))
    rescan_transfers(con)


def detect_account_id(con, filename, file_content_text):
    """Auto-detect target bank/card account ID based on the filename and content text."""
    accounts = con.execute("SELECT id, name, kind FROM accounts WHERE active=1 AND kind IN ('bank', 'card')").fetchall()
    if not accounts:
        return None

    filename_lower = str(filename).lower()
    content_lower = str(file_content_text).lower()

    best_acct_id = None
    best_score = -1

    stop_words = {'bank', 'card', 'checking', 'savings', 'account', 'credit', 'biz', 'business', 'statement', 'association', 'national', 'services'}

    for acct in accounts:
        acct_id = acct["id"]
        acct_name = acct["name"]
        acct_name_lower = acct_name.lower()

        score = 0

        # 1. Direct match of full account name in filename
        if acct_name_lower in filename_lower:
            score += 50

        # 2. Direct match of full account name in content
        if acct_name_lower in content_lower:
            score += 30

        # 3. Match of individual significant words in filename and content
        words = re.findall(r'[a-z0-9]+', acct_name_lower)
        camel_words = re.findall(r'[A-Z]?[a-z0-9]+|[A-Z]+(?=[A-Z][a-z0-9]|\b)', acct_name)
        all_words = set(words + [w.lower() for w in camel_words])
        sig_words = [w for w in all_words if w not in stop_words and len(w) > 1]

        for w in sig_words:
            if w in filename_lower:
                score += 15
            if w in content_lower:
                score += 10

        if score > best_score and score > 0:
            best_score = score
            best_acct_id = acct_id

    if best_acct_id is None:
        best_acct_id = accounts[0]["id"]

    return best_acct_id


def is_duplicate_statement(con, account_id, txns, filename):
    """Check if a statement has already been imported based on filename or transaction content."""
    if filename:
        dup_batch = con.execute("SELECT 1 FROM batches WHERE filename=? AND account_id=?", (filename, account_id)).fetchone()
        if dup_batch:
            return f"A statement with the filename '{filename}' has already been imported for this account."

    if not txns:
        return None

    matched_count = 0
    for t in txns:
        t_date = t["date"]
        t_amount = t["amount_cents"]

        staged_match = con.execute(
            "SELECT 1 FROM staged st JOIN batches b ON b.id=st.batch_id "
            "WHERE b.account_id=? AND st.amount_cents=? AND abs(julianday(st.date)-julianday(?))<=2 LIMIT 1",
            (account_id, t_amount, t_date)
        ).fetchone()
        if staged_match:
            matched_count += 1
            continue

        posted_match = con.execute(
            "SELECT 1 FROM splits s JOIN entries e ON e.id=s.entry_id "
            "WHERE s.account_id=? AND s.amount_cents=? AND abs(julianday(e.date)-julianday(?))<=2 LIMIT 1",
            (account_id, -t_amount, t_date)
        ).fetchone()
        if posted_match:
            matched_count += 1

    n = len(txns)
    if n >= 3:
        is_dup = (matched_count / n) >= 0.80
    else:
        is_dup = matched_count == n

    if is_dup:
        return f"This statement appears to have been already imported: {matched_count} of {n} transactions already exist in your books for this account."

    return None
