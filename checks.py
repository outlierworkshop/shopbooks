"""Check writing/printing: payees, per-account numbering, the ledger posting, and the PDF laid out
for standard 8.5x11 "check on top" stock (check in the top 3.5in, two voucher stubs below).

Printing a check books the payment immediately (cash basis): bank credit / expense-category debit,
exactly like any money-out entry — so when that check later clears on the bank statement, Review's
existing duplicate detection flags it and you Skip the second copy. Records-only otherwise: the PDF
positions variable fields onto PRE-PRINTED check stock (the bank's MICR line / routing / design are
already on the paper); a per-printer X/Y offset (check_offset_x/y settings) dials in the alignment.
"""
import db
import invoicing
import ledger

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
         "fifteen sixteen seventeen eighteen nineteen").split()
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def _under_thousand(n):
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")
    return _ONES[n // 100] + " hundred" + (" " + _under_thousand(n % 100) if n % 100 else "")


def _int_words(n):
    if n == 0:
        return "zero"
    parts = []
    for div, name in ((10 ** 9, "billion"), (10 ** 6, "million"), (10 ** 3, "thousand")):
        if n >= div:
            parts.append(_under_thousand(n // div) + " " + name)
            n %= div
    if n:
        parts.append(_under_thousand(n))
    return " ".join(parts)


def amount_to_words(cents):
    """The legal 'written amount' for a check, e.g. 123456 -> 'One thousand two hundred thirty-four
    and 56/100'."""
    dollars, c = divmod(abs(int(cents)), 100)
    w = _int_words(dollars)
    return f"{w[0].upper()}{w[1:]} and {c:02d}/100"


def _us_date(iso):
    """ISO 'YYYY-MM-DD' -> 'MM/DD/YYYY' for the printed check face (banks/recipients expect it);
    passes anything unparseable straight through."""
    try:
        y, m, d = str(iso).split("-")
        return f"{int(m):02d}/{int(d):02d}/{y}"
    except (ValueError, AttributeError):
        return str(iso)


def next_check_number(con, account_id):
    """One past the highest printed check number on this account, or None if none printed yet
    (the first time, the owner types the starting number off their checkbook)."""
    row = con.execute("SELECT MAX(check_number) m FROM checks WHERE account_id=? AND status='printed'",
                      (account_id,)).fetchone()
    return (row["m"] + 1) if row and row["m"] is not None else None


def bank_accounts(con):
    return con.execute("SELECT id, name FROM accounts WHERE kind='bank' AND active=1 ORDER BY name").fetchall()


def resolve_payee(con, form):
    """(payee_id, payee_name) from the check form: an existing payee, or a brand-new one created from
    the typed name (+ optional email) — mirrors invoicing.resolve_customer_id. Raises ValueError if
    neither is given."""
    picked = (form.get("payee_id") or "").strip()
    if picked:
        p = con.execute("SELECT id, name FROM payees WHERE id=?", (int(picked),)).fetchone()
        if p:
            return p["id"], p["name"]
    name = (form.get("new_payee_name") or "").strip()
    if not name:
        raise ValueError("Pick a payee, or enter a new payee's name.")
    email = (form.get("new_payee_email") or "").strip()
    address = (form.get("new_payee_address") or "").strip()
    pid = con.execute("INSERT INTO payees(name, email, address) VALUES(?, ?, ?)",
                      (name, email, address)).lastrowid
    return pid, name


def create_and_post(con, *, account_id, payee_id, payee_name, date, amount_cents, memo, category_id,
                    check_number):
    """Post the payment (category debit / bank credit) and record the printed check linked to it."""
    entry_id = ledger.post_entry(
        con, date, payee_name,
        [(category_id, amount_cents), (account_id, -amount_cents)],
        memo=(memo or f"Check #{check_number}"))
    cur = con.execute(
        "INSERT INTO checks(check_number,account_id,payee_id,payee_name,date,amount_cents,memo,"
        "category_id,entry_id,status) VALUES(?,?,?,?,?,?,?,?,?, 'printed')",
        (check_number, account_id, payee_id, payee_name, date, amount_cents, memo or "",
         category_id, entry_id))
    return cur.lastrowid


def void_check(con, check_id):
    """Void a printed check and unwind its ledger entry (raises LockedPeriodError if the period is
    closed)."""
    c = con.execute("SELECT * FROM checks WHERE id=? AND status='printed'", (check_id,)).fetchone()
    if not c:
        return
    if c["entry_id"]:
        ledger.delete_entry(con, c["entry_id"])
    con.execute("UPDATE checks SET status='void', entry_id=NULL WHERE id=?", (check_id,))


def list_checks(con, limit=200):
    return con.execute(
        "SELECT k.*, a.name account_name, cat.name category_name FROM checks k "
        "JOIN accounts a ON a.id=k.account_id LEFT JOIN accounts cat ON cat.id=k.category_id "
        "ORDER BY k.id DESC LIMIT ?", (limit,)).fetchall()


def get_check(con, check_id):
    return con.execute("SELECT * FROM checks WHERE id=?", (check_id,)).fetchone()


def _chk_get(chk, key):
    """Read a key from either a dict (preview) or a sqlite Row (reprint); None if absent."""
    try:
        return chk[key]
    except (KeyError, IndexError):
        return None


def _payee_address_lines(con, chk):
    """Mailing-address lines for the DWE001 window block: from chk['payee_addr'] when the preview
    passes it, else looked up by chk['payee_id']. Empty list when no address is on file (then the
    window block is skipped — the name is already on the Pay-to line)."""
    raw = _chk_get(chk, "payee_addr")
    if raw is None:
        pid = _chk_get(chk, "payee_id")
        if pid:
            row = con.execute("SELECT address FROM payees WHERE id=?", (pid,)).fetchone()
            raw = row["address"] if row else ""
    return [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]


def render_check_pdf(con, chk):
    """A one-page 8.5x11 PDF: variable fields positioned for pre-printed 'check on top' stock, plus
    two record stubs. `chk` is a dict/row with account_id, payee_name, date, amount_cents, memo,
    category_id, check_number. Positions in mm from the top-left; X/Y offsets nudge printer alignment."""
    from fpdf import FPDF
    ox = float(db.get_setting(con, "check_offset_x", "0") or 0)
    oy = float(db.get_setting(con, "check_offset_y", "0") or 0)
    acct = con.execute("SELECT name FROM accounts WHERE id=?", (chk["account_id"],)).fetchone()
    cat = con.execute("SELECT name FROM accounts WHERE id=?", (chk["category_id"],)).fetchone() \
        if chk["category_id"] else None
    amt = ledger.fmt_cents(chk["amount_cents"])
    words = amount_to_words(chk["amount_cents"])

    pdf = FPDF(format="letter")   # mm units, 215.9 x 279.4
    pdf.add_page()

    def text(x, y, s, size=10, style=""):
        pdf.set_font("Helvetica", style, size)
        pdf.text(x + ox, y + oy, invoicing._latin(str(s)))

    # --- the check itself (top 3.5in / 88.9mm): standard QuickBooks/Quicken voucher layout ---
    #     STCHK1 is cut to the QB voucher template; positions are mm from the top-left. Fine-tune
    #     for a specific printer with the check_offset_x/y nudge (Settings on the Write-a-check page).
    pdf.set_font("Helvetica", "", 10)                            # DATE, right-justified to the $ right edge
    ds = invoicing._latin(_us_date(chk["date"]))
    pdf.text(200 - pdf.get_string_width(ds) + ox, 18 + oy, ds)
    text(25, 31, chk["payee_name"], 11)                          # PAY TO THE ORDER OF   (~1.0in, 1.22in)
    pdf.set_font("Helvetica", "B", 11)                           # courtesy $ amount, right-aligned in box
    s = invoicing._latin("**" + amt)
    pdf.text(200 - pdf.get_string_width(s) + ox, 31 + oy, s)     # right edge ~7.87in, same line as payee
    text(9, 38, words.upper(), 10)                               # written/legal amount  (~0.35in, 1.5in)

    # payee name + address block for the DWE001 lower window (~0.9in, 1.85in) — skipped if no address
    addr_lines = _payee_address_lines(con, chk)
    if addr_lines:
        text(22, 47, chk["payee_name"], 9)
        ay = 47
        for line in addr_lines:
            ay += 4.2
            text(22, ay, line, 9)

    if chk["memo"]:
        text(23, 66, chk["memo"], 9)                             # MEMO line             (~0.9in, 2.6in)

    # --- two voucher stubs below the perforations (remittance record) ---
    for top in (100, 190):
        text(15, top, f"Check #{chk['check_number']}", 10, "B")
        text(15, top + 7, f"Date:     {_us_date(chk['date'])}")
        text(15, top + 13, f"Pay to:   {chk['payee_name']}")
        text(15, top + 19, f"Amount:   ${amt}")
        if cat:
            text(15, top + 25, f"Category: {cat['name']}")
        if chk["memo"]:
            text(15, top + 31, f"Memo:     {chk['memo']}")
        text(120, top, f"Account: {acct['name'] if acct else ''}", 9)
    return bytes(pdf.output())
