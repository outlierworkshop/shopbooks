"""Invoices: queries, PDF rendering (fpdf2), and SMTP email delivery."""
import smtplib
from email.message import EmailMessage

from fpdf import FPDF

import db
import ledger
from ledger import fmt_cents


SALES_TAX_ACCOUNT = "Sales Tax Payable"


def resolve_customer_id(con, form):
    """Customer id for a new invoice/estimate. Uses an existing customer when one is picked;
    otherwise creates a brand-new customer from the name (+ optional email) typed on the form —
    so you can bill someone who isn't in your customer list yet. Raises ValueError if neither an
    existing customer nor a new-customer name is provided."""
    picked = (form.get("customer_id") or "").strip()
    if picked:
        return int(picked)
    name = (form.get("new_customer_name") or "").strip()
    if not name:
        raise ValueError("Pick an existing customer, or enter a new customer's name.")
    email = (form.get("new_customer_email") or "").strip()
    return con.execute("INSERT INTO customers(name, email) VALUES(?, ?)", (name, email)).lastrowid


def sales_tax_rate(con):
    """Business-wide sales tax rate as a percent float (0 = no sales tax)."""
    try:
        return float(db.get_setting(con, "sales_tax_rate", "0") or 0)
    except ValueError:
        return 0.0


def sales_tax_account_id(con):
    """Account id of the Sales Tax Payable liability (db.init ensures it exists), or None."""
    row = con.execute("SELECT id FROM accounts WHERE name=?", (SALES_TAX_ACCOUNT,)).fetchone()
    return row["id"] if row else None


def invoice_subtotal(con, invoice_id):
    """Sum of line amounts (qty*unit_cents), pre-tax, integer cents. Not credit-signed."""
    row = con.execute(
        "SELECT COALESCE(SUM(CAST(round(qty*unit_cents) AS INTEGER)),0) t FROM invoice_items WHERE invoice_id=?",
        (invoice_id,)).fetchone()
    return row["t"]


def invoice_tax(con, invoice_id):
    """Sales tax on the taxable lines at the current rate, integer cents. Not credit-signed."""
    rate = sales_tax_rate(con)
    if rate <= 0:
        return 0
    row = con.execute(
        "SELECT COALESCE(SUM(CAST(round(qty*unit_cents) AS INTEGER)),0) t FROM invoice_items "
        "WHERE invoice_id=? AND taxable=1", (invoice_id,)).fetchone()
    return round(row["t"] * rate / 100.0)


def invoice_total(con, invoice_id):
    """Subtotal + sales tax (credit memos negated). Tax-inclusive, so balances/aging/payment
    reconciliation all account for the tax the customer owes."""
    total = invoice_subtotal(con, invoice_id) + invoice_tax(con, invoice_id)
    inv = con.execute("SELECT kind FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if inv and inv["kind"] == "credit_memo":
        return -total
    return total


def invoice_default_income_id(con, invoice_id):
    """Best income account to credit when an invoice is paid online (no interactive picker): the
    income account most of the invoice's catalog items map to, else the first active income
    account. Returns an account id or None."""
    rows = con.execute(
        "SELECT itm.income_account_id aid, COUNT(*) n FROM invoice_items ii "
        "JOIN items itm ON itm.id=ii.item_id "
        "WHERE ii.invoice_id=? AND itm.income_account_id IS NOT NULL "
        "GROUP BY itm.income_account_id ORDER BY n DESC", (invoice_id,)).fetchall()
    if rows:
        return rows[0]["aid"]
    row = con.execute("SELECT id FROM accounts WHERE type='income' AND active=1 ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def record_invoice_payment(con, invoice_id, *, into_account_id, income_id, amount_cents, date,
                           label=None, memo=None):
    """Post a full customer payment against an invoice and mark it paid: debit the deposit account
    (a bank account, or the Square clearing account for online payments), credit income, and split
    any collected sales tax to Sales Tax Payable (tax_allocation). Sets status='paid' + paid_entry_id
    and returns the entry id. Shared by the manual Record-Payment route and Square payment sync so
    the tax-split posting lives in exactly one place."""
    inv = con.execute("SELECT i.number, i.customer_id, c.name customer FROM invoices i "
                      "JOIN customers c ON c.id=i.customer_id WHERE i.id=?", (invoice_id,)).fetchone()
    sub = invoice_subtotal(con, invoice_id)
    tax = invoice_tax(con, invoice_id)
    inc_part, tax_part = tax_allocation(sub, tax, amount_cents)
    tax_acct = sales_tax_account_id(con)
    if tax_part and tax_acct:
        legs = [(into_account_id, amount_cents), (income_id, -inc_part), (tax_acct, -tax_part)]
    else:  # no tax (or account missing) → the whole payment is income
        legs = [(into_account_id, amount_cents), (income_id, -amount_cents)]
    d = ledger.normalize_date(date)
    entry_id = ledger.post_entry(con, d, label or f"Invoice {inv['number']} - {inv['customer']}",
                                 legs, memo=memo or f"invoice #{inv['number']}",
                                 customer_id=inv["customer_id"])
    con.execute("UPDATE invoices SET status='paid', paid_date=?, paid_entry_id=? WHERE id=?",
                (d, entry_id, invoice_id))
    return entry_id


def tax_allocation(subtotal, tax, amount):
    """Split a payment `amount` into (income_cents, tax_cents) proportional to an invoice's
    subtotal/tax, so collected sales tax lands in the liability rather than income. Tax rounds and
    income takes the remainder, so income+tax == amount exactly. No tax → (amount, 0)."""
    total = subtotal + tax
    if tax <= 0 or total <= 0:
        return amount, 0
    tax_part = round(amount * tax / total)
    return amount - tax_part, tax_part


def invoice_applied_credits(con, invoice_id):
    row = con.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM credit_applications WHERE invoice_id=?", (invoice_id,)).fetchone()
    return row[0]


def invoice_credit_sources_total(con, invoice_id):
    row = con.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM credit_applications WHERE credit_invoice_id=?", (invoice_id,)).fetchone()
    return row[0]


def invoice_outstanding_balance(con, invoice_id):
    inv = con.execute("SELECT kind, status, paid_entry_id FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        return 0
    if inv["status"] == "void":
        return 0
    if inv["kind"] == "credit_memo":
        total = abs(invoice_total(con, invoice_id))
        applied = invoice_credit_sources_total(con, invoice_id)
        return -(total - applied)
    elif inv["kind"] == "invoice":
        total = invoice_total(con, invoice_id)
        payments = invoice_payments_total(con, invoice_id)
        applied = invoice_applied_credits(con, invoice_id)
        return max(0, total - payments - applied)
    return 0


def _payment_leg_filter(con):
    """SQL condition + params selecting an entry's customer-payment legs: income plus any collected
    sales tax booked to Sales Tax Payable. So a tax-split payment (income + tax legs) totals the full
    amount received, and a plain matched deposit (all income) still totals correctly."""
    tax_id = sales_tax_account_id(con)
    if tax_id:
        return "(a.type='income' OR s.account_id=?)", [tax_id]
    return "a.type='income'", []


def invoice_payments_total(con, invoice_id):
    """Total payments matched to an invoice (integer cents), counting income + collected-sales-tax
    legs so tax-inclusive invoices reconcile."""
    row = con.execute("SELECT paid_entry_id, matched_entry_id FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not row:
        return 0
    cond, p = _payment_leg_filter(con)

    if row["paid_entry_id"]:
        val = con.execute(
            f"SELECT SUM(abs(s.amount_cents)) FROM splits s JOIN accounts a ON a.id=s.account_id "
            f"WHERE s.entry_id=? AND {cond}", [row["paid_entry_id"], *p]).fetchone()[0]
        return val or 0

    val = con.execute(
        f"SELECT SUM(abs(s.amount_cents)) FROM splits s JOIN accounts a ON a.id=s.account_id "
        f"JOIN invoice_entry_links iel ON iel.entry_id=s.entry_id "
        f"WHERE iel.invoice_id=? AND {cond}", [invoice_id, *p]).fetchone()[0]

    if not val and row["matched_entry_id"]:
        val = con.execute(
            f"SELECT SUM(abs(s.amount_cents)) FROM splits s JOIN accounts a ON a.id=s.account_id "
            f"WHERE s.entry_id=? AND {cond}", [row["matched_entry_id"], *p]).fetchone()[0]

    return val or 0


def invoice_payment_entries(con, invoice_id):
    """Each payment posted against an invoice, as {entry_id, date, payee, amount_cents} (income legs,
    positive cents), oldest first. Mirrors invoice_payments_total's priority so totals reconcile:
    a single full-payment entry (paid_entry_id), else every entry linked via invoice_entry_links
    (multi-payment), else the matched deposit. Callers that need per-payment rows (e.g. the customer
    statement) use this instead of re-deriving the set from paid_entry_id/matched_entry_id alone."""
    row = con.execute("SELECT paid_entry_id, matched_entry_id FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not row:
        return []
    if row["paid_entry_id"]:
        eids = [row["paid_entry_id"]]
    else:
        eids = [r["entry_id"] for r in con.execute(
            "SELECT entry_id FROM invoice_entry_links WHERE invoice_id=?", (invoice_id,)).fetchall()]
        if not eids and row["matched_entry_id"]:
            eids = [row["matched_entry_id"]]
    if not eids:
        return []
    ph = ",".join("?" for _ in eids)
    cond, p = _payment_leg_filter(con)  # income + collected-sales-tax legs = the full amount received
    rows = con.execute(
        f"SELECT e.id entry_id, e.date, e.payee, COALESCE(SUM(abs(s.amount_cents)),0) amount_cents "
        f"FROM entries e JOIN splits s ON s.entry_id=e.id JOIN accounts a ON a.id=s.account_id "
        f"WHERE e.id IN ({ph}) AND {cond} GROUP BY e.id ORDER BY e.date, e.id", [*eids, *p]).fetchall()
    return [{"entry_id": r["entry_id"], "date": r["date"], "payee": r["payee"],
             "amount_cents": r["amount_cents"]} for r in rows if r["amount_cents"] > 0]


def get_invoice(con, invoice_id):
    inv = con.execute(
        "SELECT i.*, c.name customer, c.email customer_email, c.address customer_address "
        "FROM invoices i JOIN customers c ON c.id=i.customer_id WHERE i.id=?", (invoice_id,)).fetchone()
    if not inv:
        return None, [], 0
    # LEFT JOIN the catalog so each line carries item_name/item_active — lets the edit form keep a
    # line linked to a now-inactive item (which isn't in the active dropdown) instead of dropping it.
    items = con.execute(
        "SELECT ii.*, itm.name AS item_name, itm.active AS item_active "
        "FROM invoice_items ii LEFT JOIN items itm ON itm.id = ii.item_id "
        "WHERE ii.invoice_id=? ORDER BY ii.id", (invoice_id,)).fetchall()
    return inv, items, invoice_total(con, invoice_id)


def next_number(con):
    n = int(db.get_setting(con, "next_invoice_number", "1001"))
    db.set_setting(con, "next_invoice_number", str(n + 1))
    return f"INV-{n}"


def next_estimate_number(con):
    n = int(db.get_setting(con, "next_estimate_number", "1001"))
    db.set_setting(con, "next_estimate_number", str(n + 1))
    return f"EST-{n}"


def next_credit_memo_number(con):
    n = int(db.get_setting(con, "next_credit_memo_number", "1001"))
    db.set_setting(con, "next_credit_memo_number", str(n + 1))
    return f"CM-{n}"


def available_credits_for_customer(con, customer_id):
    """A customer's open credit SOURCES — unapplied credit memos and overpaid invoices — each with
    its remaining (unused) amount in cents. The single home for 'what credit can I apply'."""
    out = []
    for c in con.execute("SELECT id, number, kind FROM invoices "
                         "WHERE customer_id=? AND kind IN ('invoice','credit_memo') AND status!='void'",
                         (customer_id,)).fetchall():
        applied = con.execute("SELECT COALESCE(SUM(amount_cents),0) FROM credit_applications "
                              "WHERE credit_invoice_id=?", (c["id"],)).fetchone()[0]
        if c["kind"] == "credit_memo":
            avail = abs(invoice_total(con, c["id"])) - applied
        else:  # an overpaid invoice's excess can be used as a credit
            avail = invoice_payments_total(con, c["id"]) - invoice_total(con, c["id"]) - applied
        if avail > 0:
            out.append({"id": c["id"], "number": c["number"], "kind": c["kind"], "available_cents": avail})
    return out


def customer_available_credit(con, customer_id):
    """Total unused credit a customer has on file (cents)."""
    return sum(s["available_cents"] for s in available_credits_for_customer(con, customer_id))


def available_credit_total(con):
    """Total unused customer credit across every customer (cents) — for the dashboard briefing."""
    return sum(customer_available_credit(con, c["id"])
               for c in con.execute("SELECT id FROM customers").fetchall())


def _kind(inv):
    """Document kind of an invoices row ('invoice' or 'estimate'), tolerant of rows without it."""
    try:
        return inv["kind"] or "invoice"
    except (KeyError, IndexError):
        return "invoice"


# Common non-latin-1 punctuation -> ASCII, so fpdf2's built-in fonts don't render it as "?".
_PUNCT = {
    "—": "-", "–": "-", "‑": "-",           # em / en / non-breaking dash
    "‘": "'", "’": "'", "“": '"', "”": '"',  # curly quotes
    "…": "...", "•": "-", "→": "->", "™": "(TM)",
}


def _latin(s):
    s = str(s or "")
    for uni, ascii_ in _PUNCT.items():
        s = s.replace(uni, ascii_)
    return s.encode("latin-1", "replace").decode("latin-1")


def render_pdf(con, inv, items, total):
    """Build the invoice / estimate / credit-memo PDF (clean-minimal design); returns bytes.
    All money branches (subtotal, tax, payments, credits, credit-memo, estimate, balance due)
    are preserved; only the visual treatment changed. Built-in fonts only (see _latin)."""
    biz = db.get_setting(con, "business_name", "My Business")
    addr = db.get_setting(con, "business_address", "")
    bemail = db.get_setting(con, "business_email", "")
    bphone = db.get_setting(con, "business_phone", "")
    terms = db.get_setting(con, "invoice_terms", "")

    INK = (31, 36, 33); GRAY = (105, 105, 105); MUTED = (146, 146, 146); HAIR = (228, 226, 219)
    is_est = _kind(inv) == "estimate"
    is_credit_memo = _kind(inv) == "credit_memo"
    doc_label = "ESTIMATE" if is_est else "CREDIT MEMO" if is_credit_memo else "INVOICE"

    pdf = FPDF(format="letter")  # US Letter (216 x 279 mm / 8.5 x 11 in)
    pdf.add_page()
    pdf.set_auto_page_break(True, margin=22)
    L, R = 18, 198  # 18 mm side margins on a 216 mm-wide page

    # 1. Header: logo + business (left), document label + number (right)
    name_y = 20
    logo = db.company_logo_path(con)
    if logo:
        try:
            pdf.image(str(logo), x=L, y=15, h=13)  # height-constrained so any aspect ratio fits
            name_y = 31
        except Exception:
            pass  # a bad/unsupported logo image never breaks the invoice
    pdf.set_xy(L, name_y)
    pdf.set_font("helvetica", "B", 11)
    pdf.set_text_color(*INK)
    pdf.cell(0, 6, _latin(biz), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 8.5)
    pdf.set_text_color(*MUTED)
    for line in [x for x in (addr.splitlines() + [bemail, bphone]) if x.strip()]:
        pdf.set_x(L)
        pdf.cell(0, 4.4, _latin(line), new_x="LMARGIN", new_y="NEXT")
    left_end = pdf.get_y()
    pdf.set_xy(120, 20)
    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(*MUTED)
    pdf.cell(R - 120, 5, " ".join(doc_label), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(120)
    pdf.set_font("helvetica", "B", 22)
    pdf.set_text_color(*INK)
    pdf.cell(R - 120, 11, _latin(inv["number"]), align="R", new_x="LMARGIN", new_y="NEXT")

    # 2. Bill-to (left) and dates (right) — start below whichever header column is taller
    pdf.set_y(max(46, left_end, pdf.get_y()) + 8)
    y_row = pdf.get_y()
    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(*MUTED)
    pdf.cell(0, 5, "BILL TO", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "B", 10.5)
    pdf.set_text_color(*INK)
    pdf.cell(0, 6, _latin(inv["customer"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 9)
    pdf.set_text_color(*GRAY)
    for line in (inv["customer_address"] or "").splitlines():
        pdf.cell(0, 4.8, _latin(line), new_x="LMARGIN", new_y="NEXT")
    if inv["customer_email"]:
        pdf.cell(0, 4.8, _latin(inv["customer_email"]), new_x="LMARGIN", new_y="NEXT")

    pdf.set_xy(130, y_row)
    date_label = "Valid until" if is_est else "Due"
    for lbl, val in [("Date", inv["date"]), (date_label, inv["due_date"])]:
        pdf.set_x(130)
        pdf.set_font("helvetica", "", 8.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(22, 5.5, lbl)
        pdf.set_font("helvetica", "", 9)
        pdf.set_text_color(*INK)
        pdf.cell(R - 130 - 22, 5.5, str(val), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(max(pdf.get_y(), y_row + 24) + 8)

    # 3. Line items: hairline rules, no filled rows
    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(*MUTED)
    pdf.cell(102, 7, "DESCRIPTION")
    pdf.cell(16, 7, "QTY", align="R")
    pdf.cell(30, 7, "UNIT", align="R")
    pdf.cell(32, 7, "AMOUNT", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*INK)
    pdf.set_line_width(0.3)
    pdf.line(L, pdf.get_y(), R, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(*INK)
    for it in items:
        amt = round(it["qty"] * it["unit_cents"])
        qty = f"{it['qty']:g}"
        pdf.cell(102, 8, _latin(it["description"])[:64])
        pdf.cell(16, 8, qty, align="R")
        pdf.cell(30, 8, f"${fmt_cents(it['unit_cents'])}", align="R")
        pdf.cell(32, 8, f"${fmt_cents(amt)}", align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*HAIR)
        pdf.set_line_width(0.15)
        pdf.line(L, pdf.get_y() + 1, R, pdf.get_y() + 1)
        pdf.ln(2)
    pdf.ln(3)

    # 4. Totals block (right-aligned). Same money logic as before, minimal styling.
    def trow(label, value):
        pdf.set_x(115)
        pdf.set_font("helvetica", "", 10)
        pdf.set_text_color(*GRAY)
        pdf.cell(45, 6.5, label)
        pdf.cell(R - 115 - 45, 6.5, value, align="R", new_x="LMARGIN", new_y="NEXT")

    tax_cents = invoice_tax(con, inv["id"])
    if tax_cents:
        trow("Subtotal", f"${fmt_cents(invoice_subtotal(con, inv['id']))}")
        trow(f"Sales tax ({sales_tax_rate(con):g}%)", f"${fmt_cents(tax_cents)}")

    payments_total = 0
    applied_credits = 0
    credits_applied_from = 0
    if not is_est:
        if is_credit_memo:
            credits_applied_from = invoice_credit_sources_total(con, inv["id"])
        else:
            payments_total = invoice_payments_total(con, inv["id"])
            applied_credits = invoice_applied_credits(con, inv["id"])
    has_deductions = payments_total > 0 or applied_credits > 0 or credits_applied_from > 0

    if has_deductions:
        trow("Total Credit" if is_credit_memo else "Total", f"${fmt_cents(abs(total))}")
        if payments_total > 0:
            trow("Payments received", f"-${fmt_cents(payments_total)}")
        if applied_credits > 0:
            trow("Credits applied", f"-${fmt_cents(applied_credits)}")
        if credits_applied_from > 0:
            trow("Applied to invoices", f"-${fmt_cents(credits_applied_from)}")
        if is_credit_memo:
            bal = abs(total) - credits_applied_from
        else:
            bal = max(0, total - payments_total - applied_credits)
        final_label = "Remaining credit" if is_credit_memo else "Remaining balance due"
        final_value = f"${fmt_cents(bal)}"
    else:
        final_label = ("Estimated total" if is_est else
                       "Total credit" if is_credit_memo else "Total due")
        final_value = f"${fmt_cents(abs(total))}"

    pdf.set_draw_color(*INK)
    pdf.set_line_width(0.3)
    pdf.line(115, pdf.get_y() + 1, R, pdf.get_y() + 1)
    pdf.ln(3.5)
    pdf.set_x(115)
    pdf.set_font("helvetica", "B", 12.5)
    pdf.set_text_color(*INK)
    pdf.cell(45, 8, final_label)
    pdf.cell(R - 115 - 45, 8, final_value, align="R", new_x="LMARGIN", new_y="NEXT")

    # 5. Notes & terms
    if inv["memo"]:
        pdf.ln(9)
        pdf.set_font("helvetica", "", 8)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 5, "NOTES", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "", 9.5)
        pdf.set_text_color(*GRAY)
        pdf.multi_cell(0, 5, _latin(inv["memo"]))
    if terms:
        pdf.ln(3)
        pdf.set_font("helvetica", "", 8)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 5, "TERMS", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "", 9.5)
        pdf.set_text_color(*GRAY)
        pdf.multi_cell(0, 4.8, _latin(terms))

    # 6. Footer, anchored near the bottom
    pdf.set_y(-17)
    pdf.set_draw_color(*HAIR)
    pdf.set_line_width(0.3)
    pdf.line(L, pdf.get_y(), R, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("helvetica", "", 8.5)
    pdf.set_text_color(*MUTED)
    pdf.cell(0, 5, _latin(f"{biz}  -  {bemail}" if bemail else biz), align="C")

    return bytes(pdf.output())


def ar_aging(con, today=None):
    """Accounts-receivable aging: every open (sent, unpaid, or partially paid) invoice bucketed by how overdue it is.
    Deterministic — totals come straight from the line items. `current` = not yet past due."""
    from datetime import date, datetime
    today = today or date.today().isoformat()
    td = datetime.strptime(today, "%Y-%m-%d")
    rows = con.execute(
        "SELECT i.id, i.number, i.date, i.due_date, i.last_reminder_date, i.kind, "
        "c.name customer, c.email customer_email "
        "FROM invoices i JOIN customers c ON c.id=i.customer_id "
        "WHERE i.kind IN ('invoice', 'credit_memo') AND i.status IN ('sent', 'partially_paid') ORDER BY i.due_date").fetchall()
    buckets = {"current": 0, "1-30": 0, "31-60": 0, "61-90": 0, "90+": 0}
    out = []
    for r in rows:
        total = invoice_total(con, r["id"])
        payments_total = invoice_payments_total(con, r["id"])
        outstanding = invoice_outstanding_balance(con, r["id"])
        if outstanding == 0:
            continue
        
        if outstanding < 0:
            buckets["current"] += outstanding
            days = 0
            b = "current"
        else:
            days = (td - datetime.strptime(r["due_date"], "%Y-%m-%d")).days
            b = ("current" if days <= 0 else "1-30" if days <= 30 else "31-60" if days <= 60
                 else "61-90" if days <= 90 else "90+")
            buckets[b] += outstanding
            
        out.append({**dict(r), "total": total, "payments_total": payments_total, "outstanding": outstanding,
                    "days_overdue": max(days, 0), "bucket": b, "overdue": days > 0})
    total_out = sum(buckets.values())
    return {"rows": out, "buckets": buckets, "total": total_out,
            "overdue_total": max(0, total_out - buckets["current"]),
            "overdue_count": sum(1 for r in out if r["overdue"] and r["outstanding"] > 0),
            "open_count": len(out)}


def email_configured(con):
    return bool(db.get_setting(con, "smtp_user", "") and db.get_setting(con, "smtp_password", ""))


def _smtp_send(con, msg):
    """Connect to the configured SMTP server and send an already-built EmailMessage.
    Raises RuntimeError if email isn't set up; otherwise raises the underlying smtplib error
    (translate it with explain_smtp_error for a user-facing message)."""
    host = db.get_setting(con, "smtp_host", "smtp.gmail.com")
    port = int(db.get_setting(con, "smtp_port", "587") or 587)
    user = db.get_setting(con, "smtp_user", "")
    password = db.get_setting(con, "smtp_password", "")
    if not (user and password):
        raise RuntimeError("Email isn't set up - add SMTP details in Settings first.")
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)


def explain_smtp_error(e):
    """Turn an SMTP/connection failure into plain guidance for the owner."""
    if isinstance(e, RuntimeError):
        return str(e)
    if isinstance(e, smtplib.SMTPAuthenticationError) or "535" in str(e) or "5.7.8" in str(e):
        return ("The mail server rejected the login. For Gmail/Google Workspace this almost always "
                "means the password isn't an App Password (a normal password won't work), or "
                "2-Step Verification isn't turned on yet. See docs/email-setup.md.")
    if isinstance(e, (smtplib.SMTPConnectError, ConnectionError, TimeoutError, OSError)):
        return ("Couldn't reach the mail server. Check the SMTP host and port (Gmail: smtp.gmail.com "
                "port 587) and your internet connection / firewall.")
    return f"Email failed: {e}"


def _apply_email_body(msg, con, plain_body):
    """Set the message's plain-text body and add a simple branded HTML alternative, with the
    uploaded company logo as an inline header image if one is set. Plain text stays the fallback,
    so the message is readable in any client; the logo is best-effort and never blocks sending."""
    import html as _h
    from email.utils import make_msgid
    msg.set_content(plain_body)
    biz = db.get_setting(con, "business_name", "My Business")
    logo = db.company_logo_raster_path(con)  # emails need a raster; SVG uploads get a PNG companion
    cid = make_msgid() if logo else None
    logo_html = (f'<img src="cid:{cid[1:-1]}" alt="{_h.escape(biz)}" '
                 'style="max-height:60px;max-width:220px;margin-bottom:18px">') if logo else ""
    body_html = _h.escape(plain_body).replace("\n", "<br>")
    html_doc = (
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#23281f;font-size:14px;'
        'line-height:1.55;max-width:560px;margin:0 auto;padding:8px">'
        f'{logo_html}<div>{body_html}</div>'
        '<hr style="border:none;border-top:1px solid #e4e2d9;margin:22px 0 12px">'
        f'<div style="color:#8a8f83;font-size:12px">{_h.escape(biz)}</div></div>')
    msg.add_alternative(html_doc, subtype="html")
    if logo:
        subtype = logo.suffix.lower().lstrip(".")
        subtype = "jpeg" if subtype == "jpg" else subtype
        try:
            msg.get_payload()[1].add_related(logo.read_bytes(), "image", subtype, cid=cid)
        except Exception:
            pass


def send_test_email(con):
    """Send a small self-addressed test message to confirm the SMTP settings work.
    Returns the address it was sent to; raises on failure (see explain_smtp_error)."""
    user = db.get_setting(con, "smtp_user", "")
    if not (user and db.get_setting(con, "smtp_password", "")):
        raise RuntimeError("Email isn't set up - add your SMTP details above and Save first.")
    biz = db.get_setting(con, "business_name", "My Business")
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = user
    msg["Subject"] = "ShopBooks test email"
    _apply_email_body(msg, con, f"This is a test message from ShopBooks for {biz}.\n\n"
                      "If you're reading this, invoice email is working.")
    _smtp_send(con, msg)
    return user


def send_invoice_email(con, inv, total, pdf_bytes, to_addr, subject=None, body=None, pay_url=None):
    """Send the invoice PDF over SMTP. When `pay_url` is given (the Square hosted payment page), a
    'Pay online' line is appended so the customer can pay by ACH or card. Raises on failure with a
    readable message."""
    user = db.get_setting(con, "smtp_user", "")
    password = db.get_setting(con, "smtp_password", "")
    if not (user and password):
        raise RuntimeError("Email isn't set up - add SMTP details in Settings first.")
    biz = db.get_setting(con, "business_name", "My Business")
    pay_total = invoice_payments_total(con, inv["id"])
    outstanding = max(0, total - pay_total)
    fields = {"number": inv["number"], "business": biz, "customer": inv["customer"],
              "total": fmt_cents(total), "due_date": inv["due_date"], "date": inv["date"],
              "payments_total": fmt_cents(pay_total), "outstanding": fmt_cents(outstanding)}
    subject = (subject or db.get_setting(con, "email_subject")).format(**fields)
    body = (body or db.get_setting(con, "email_body")).format(**fields)
    if pay_url:
        body += f"\n\nPay online (bank transfer or card): {pay_url}\n"

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to_addr
    msg["Subject"] = subject
    _apply_email_body(msg, con, body)
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf",
                       filename=f"{inv['number']}.pdf")
    _smtp_send(con, msg)
