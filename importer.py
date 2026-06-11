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
    """True if a posted entry already hits this source account with the same amount within 4 days."""
    row = con.execute(
        "SELECT 1 FROM splits s JOIN entries e ON e.id=s.entry_id "
        "WHERE s.account_id=? AND s.amount_cents=? AND abs(julianday(e.date)-julianday(?))<=4 LIMIT 1",
        (source_account_id, -amount_cents, date)).fetchone()
    return row is not None


def stage_transactions(con, batch_id, txns, source_account_id, category_names_by_id, ai_categories=None):
    """Insert staged rows, auto-categorizing via rules first, AI suggestions second."""
    name_to_id = {v: k for k, v in category_names_by_id.items()}
    for i, t in enumerate(txns):
        cat_id = apply_rules(con, t["description"])
        if cat_id is None and ai_categories:
            cat_id = name_to_id.get(ai_categories[i])
        con.execute(
            "INSERT INTO staged(batch_id,date,description,amount_cents,category_id) VALUES(?,?,?,?,?)",
            (batch_id, t["date"], t["description"], t["amount_cents"], cat_id))
