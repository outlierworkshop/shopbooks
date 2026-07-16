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


def estimate_billed_subtotal(con, estimate_id):
    """Pre-tax subtotal already billed against an estimate by its progress invoices (void ones don't
    count). Progress billing tracks the PRE-TAX job amount so the portions always sum to the estimate
    exactly — tax is added per invoice by the normal engine."""
    rows = con.execute("SELECT id FROM invoices WHERE estimate_id=? AND kind='invoice' "
                       "AND status!='void'", (estimate_id,)).fetchall()
    return sum(invoice_subtotal(con, r["id"]) for r in rows)


def estimate_remaining_subtotal(con, estimate_id):
    """Pre-tax subtotal still un-billed on an estimate (0 once fully billed)."""
    return max(0, invoice_subtotal(con, estimate_id) - estimate_billed_subtotal(con, estimate_id))


def progress_info(con, invoice_id):
    """Progress-billing context for an invoice billed against an estimate, else None for an ordinary
    invoice. Carries the parent estimate's line items — the FULL job scope, shown for reference on the
    invoice/PDF/email but NOT charged here — plus this invoice's share and the running billed/remaining
    totals. One helper feeds the invoice page, the PDF and the email."""
    row = con.execute("SELECT estimate_id FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not row or not row["estimate_id"]:
        return None
    est = con.execute("SELECT id, number FROM invoices WHERE id=?", (row["estimate_id"],)).fetchone()
    if not est:
        return None
    job_sub = invoice_subtotal(con, est["id"])
    this_sub = invoice_subtotal(con, invoice_id)
    billed = estimate_billed_subtotal(con, est["id"])
    scope = con.execute(
        "SELECT ii.*, itm.name AS item_name FROM invoice_items ii "
        "LEFT JOIN items itm ON itm.id = ii.item_id WHERE ii.invoice_id=? ORDER BY ii.id",
        (est["id"],)).fetchall()
    return {"estimate_id": est["id"], "estimate_number": est["number"], "scope_items": scope,
            "job_subtotal": job_sub, "this_subtotal": this_sub, "billed_subtotal": billed,
            "remaining_subtotal": max(0, job_sub - billed),
            "percent": (round(this_sub * 100.0 / job_sub, 1) if job_sub else 0),
            # A full conversion already lists the whole job as its own lines — only a PARTIAL invoice
            # needs the scope block + progress note (it bills less than it shows).
            "is_partial": this_sub < job_sub}


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
    """Post a customer payment against an invoice: debit the deposit account (a bank account, or the
    Square clearing account for online payments), credit income, and split any collected sales tax to
    Sales Tax Payable. `tax_allocation` is proportional, so a PARTIAL payment splits its tax correctly.

    A payment covering the whole outstanding balance closes the invoice: status='paid' + paid_entry_id
    (the entry we own, so Undo payment can remove it) — today's behavior. Anything less is a partial:
    it's linked through invoice_entry_links (the same machinery statement-matched deposits use) and the
    invoice sits at 'partially_paid' until payments + credits cover the total. Both count, because
    invoice_payments_total sums the union of the two. Returns the entry id. Shared by the manual
    Record-Payment route and Square payment sync so the tax-split posting lives in exactly one place."""
    inv = con.execute("SELECT i.number, i.customer_id, c.name customer FROM invoices i "
                      "JOIN customers c ON c.id=i.customer_id WHERE i.id=?", (invoice_id,)).fetchone()
    outstanding = invoice_outstanding_balance(con, invoice_id)
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

    if amount_cents >= outstanding:
        con.execute("UPDATE invoices SET status='paid', paid_date=?, paid_entry_id=? WHERE id=?",
                    (d, entry_id, invoice_id))
        return entry_id

    con.execute("INSERT OR IGNORE INTO invoice_entry_links(invoice_id, entry_id) VALUES(?, ?)",
                (invoice_id, entry_id))
    covered = invoice_payments_total(con, invoice_id) + invoice_applied_credits(con, invoice_id)
    if covered >= invoice_total(con, invoice_id):
        con.execute("UPDATE invoices SET status='paid', paid_date=? WHERE id=?", (d, invoice_id))
    else:
        con.execute("UPDATE invoices SET status='partially_paid', paid_date=NULL WHERE id=?",
                    (invoice_id,))
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


def _payment_entry_ids(con, invoice_id, row=None):
    """Distinct ledger entries counting as payments on an invoice: the payment we posted
    (paid_entry_id) UNION every linked deposit (invoice_entry_links) — an invoice legitimately has
    both once a partial payment (or matched deposit) is followed by a final Record-Payment — falling
    back to the matched deposit when there are neither. Single source of truth for the payment set, so
    invoice_payments_total and invoice_payment_entries can never disagree."""
    if row is None:
        row = con.execute("SELECT paid_entry_id, matched_entry_id FROM invoices WHERE id=?",
                          (invoice_id,)).fetchone()
    if not row:
        return []
    eids = []
    if row["paid_entry_id"]:
        eids.append(row["paid_entry_id"])
    for r in con.execute("SELECT entry_id FROM invoice_entry_links WHERE invoice_id=?",
                         (invoice_id,)).fetchall():
        if r["entry_id"] not in eids:
            eids.append(r["entry_id"])
    if not eids and row["matched_entry_id"]:
        eids.append(row["matched_entry_id"])
    return eids


def invoice_payments_total(con, invoice_id):
    """Total payments matched to an invoice (integer cents), counting income + collected-sales-tax
    legs so tax-inclusive invoices reconcile. Sums the UNION of the payment set (see
    _payment_entry_ids): counting only paid_entry_id — as this used to — undercounted an invoice that
    had a linked partial/matched deposit AND a final posted payment."""
    eids = _payment_entry_ids(con, invoice_id)
    if not eids:
        return 0
    ph = ",".join("?" for _ in eids)
    cond, p = _payment_leg_filter(con)
    val = con.execute(
        f"SELECT SUM(abs(s.amount_cents)) FROM splits s JOIN accounts a ON a.id=s.account_id "
        f"WHERE s.entry_id IN ({ph}) AND {cond}", [*eids, *p]).fetchone()[0]
    return val or 0


def invoice_payment_entries(con, invoice_id):
    """Each payment posted against an invoice, as {entry_id, date, payee, amount_cents} (income legs,
    positive cents), oldest first — the same set invoice_payments_total sums, so the two reconcile.
    Callers that need per-payment rows (e.g. the customer statement) use this instead of re-deriving
    the set from paid_entry_id/matched_entry_id alone."""
    eids = _payment_entry_ids(con, invoice_id)
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
    # A logo shows iff company_logo is set. Prefer the raster (PNG) companion to draw it — fpdf2's
    # SVG rendering garbles script/complex marks — falling back to the original file, then nothing.
    if db.company_logo_path(con):
        for _lg in (db.company_logo_raster_path(con), db.company_logo_path(con)):
            if not _lg:
                continue
            try:
                pdf.image(str(_lg), x=L, y=15, h=13)  # height-constrained so the aspect ratio is kept
                name_y = 31
                break
            except Exception:
                continue  # a bad/unsupported logo image never breaks the invoice
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

    # 4b. Progress billing: the full job from the parent estimate, shown for the customer's reference.
    #     Only this invoice's portion (its own lines, above) is charged.
    prog = progress_info(con, inv["id"]) if not is_est and not is_credit_memo else None
    if prog and prog["is_partial"]:
        pdf.ln(9)
        pdf.set_font("helvetica", "", 8)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 5, _latin(f"FULL JOB - ESTIMATE {prog['estimate_number']}   "
                              "(for reference - only this invoice's portion is charged)"),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "", 7.5)   # column headers, matching the billed line-items table
        pdf.cell(102, 4.5, "DESCRIPTION")
        pdf.cell(16, 4.5, "QTY", align="R")
        pdf.cell(30, 4.5, "UNIT", align="R")
        pdf.cell(32, 4.5, "AMOUNT", align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*HAIR)
        pdf.set_line_width(0.15)
        pdf.line(L, pdf.get_y(), R, pdf.get_y())
        pdf.ln(1.5)
        pdf.set_font("helvetica", "", 9)
        for it in prog["scope_items"]:
            pdf.set_text_color(*GRAY)
            pdf.cell(102, 5, _latin(str(it["description"])[:64]))
            pdf.cell(16, 5, f"{it['qty']:g}", align="R")
            pdf.cell(30, 5, f"${fmt_cents(it['unit_cents'])}", align="R")
            pdf.cell(32, 5, f"${fmt_cents(round(it['qty'] * it['unit_cents']))}",
                     align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)
        pdf.set_font("helvetica", "", 8.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 5, _latin(
            f"Job total (before tax) ${fmt_cents(prog['job_subtotal'])}  -  this invoice "
            f"{prog['percent']:g}% (${fmt_cents(prog['this_subtotal'])})  -  billed to date "
            f"${fmt_cents(prog['billed_subtotal'])}  -  remaining ${fmt_cents(prog['remaining_subtotal'])}"),
            align="R", new_x="LMARGIN", new_y="NEXT")

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

    # 6. Footer, anchored near the bottom. Disable auto page-break first: set_y(-17) lands inside the
    #    break margin, so drawing the footer cell would otherwise spill onto a spurious 2nd page.
    pdf.set_auto_page_break(False)
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


def _qty_str(q):
    q = float(q)
    return str(int(q)) if q == int(q) else f"{q:g}"


def resolve_email_text(con, inv, total, subject=None, body=None):
    """The subject + message that will actually go out: the given text, else the saved template, with
    {placeholders} filled in. Preview and send both call this, so what you review is what sends."""
    biz = db.get_setting(con, "business_name", "My Business")
    pay_total = invoice_payments_total(con, inv["id"])
    outstanding = max(0, total - pay_total)
    fields = {"number": inv["number"], "business": biz, "customer": inv["customer"],
              "total": fmt_cents(total), "due_date": inv["due_date"], "date": inv["date"],
              "payments_total": fmt_cents(pay_total), "outstanding": fmt_cents(outstanding)}
    subject = (subject or db.get_setting(con, "email_subject")).format(**fields)
    note = (body or db.get_setting(con, "email_body")).format(**fields)
    return subject, note


def invoice_email_html(con, inv, total, note, pay_url=None, logo_src=""):
    """The branded HTML email body that mirrors the PDF invoice: logo + business header, number/dates,
    bill-to, line items with totals, the owner's note, the full job scope for a progress invoice, and —
    when `pay_url` is set — a prominent 'Pay here' button. Email-safe: table layout, inline styles only.

    `logo_src` is whatever goes in the logo <img src>: a `cid:` reference when building a real message,
    or `/settings/logo` when rendering this same markup for the on-screen preview — so what you preview
    is literally what gets sent."""
    import html as _h

    biz = db.get_setting(con, "business_name", "My Business")
    addr = db.get_setting(con, "business_address", "")
    bemail = db.get_setting(con, "business_email", "")
    bphone = db.get_setting(con, "business_phone", "")
    terms = db.get_setting(con, "invoice_terms", "")
    _, items, _ = get_invoice(con, inv["id"])
    subtotal = invoice_subtotal(con, inv["id"])
    tax = invoice_tax(con, inv["id"])

    INK, GRAY, MUTED, HAIR, BG, ACCENT = "#1f2421", "#696969", "#929292", "#e4e2db", "#f4f3ee", "#2f9e44"
    e = _h.escape

    cards = db.get_setting(con, "square_enable_card", "1") == "1"
    methods_txt = "bank transfer (ACH) or card" if cards else "bank transfer (ACH)"

    logo_html = (f'<img src="{e(logo_src)}" alt="{e(biz)}" '
                 f'style="max-height:56px;max-width:220px;margin-bottom:12px">') if logo_src else ""

    contact = "<br>".join(e(x) for x in (addr.splitlines() + [bemail, bphone]) if x.strip())
    cust = [e(x) for x in (inv["customer_address"] or "").splitlines()]
    if inv["customer_email"]:
        cust.append(e(inv["customer_email"]))
    cust_html = ("<br>".join(cust)) if cust else ""

    rows = ""
    for it in items:
        amt = fmt_cents(round(it["qty"] * it["unit_cents"]))
        rows += (
            '<tr>'
            f'<td style="padding:8px 0;border-bottom:1px solid #efeee7;color:{INK}">{e(it["description"])}</td>'
            f'<td align="right" style="padding:8px 0 8px 10px;border-bottom:1px solid #efeee7;color:{GRAY};white-space:nowrap">{_qty_str(it["qty"])}</td>'
            f'<td align="right" style="padding:8px 0 8px 10px;border-bottom:1px solid #efeee7;color:{GRAY};white-space:nowrap">${fmt_cents(it["unit_cents"])}</td>'
            f'<td align="right" style="padding:8px 0 8px 14px;border-bottom:1px solid #efeee7;color:{INK};white-space:nowrap">${amt}</td>'
            '</tr>')

    def total_row(lbl, val, strong=False):
        w = "bold" if strong else "normal"
        sz = "15px" if strong else "13px"
        col = INK if strong else GRAY
        return (f'<tr><td align="right" style="padding:3px 14px 3px 0;color:{col};font-size:{sz};font-weight:{w}">{lbl}</td>'
                f'<td align="right" style="padding:3px 0;color:{col};font-size:{sz};font-weight:{w};white-space:nowrap">${val}</td></tr>')
    totals = total_row("Subtotal", fmt_cents(subtotal))
    if tax:
        totals += total_row("Sales tax", fmt_cents(tax))
    totals += total_row("Total due", fmt_cents(total), strong=True)

    # Progress billing: the full job from the parent estimate, for reference — not charged here.
    prog = progress_info(con, inv["id"])
    scope_html = ""
    if prog and prog["is_partial"]:
        rows_html = ""
        for it in prog["scope_items"]:
            cell = f'padding:5px 0;border-bottom:1px solid #efeee7;color:{GRAY};font-size:12px'
            rows_html += (
                '<tr>'
                f'<td style="{cell}">{e(it["description"])}</td>'
                f'<td align="right" style="{cell};padding-left:10px;white-space:nowrap">{_qty_str(it["qty"])}</td>'
                f'<td align="right" style="{cell};padding-left:10px;white-space:nowrap">${fmt_cents(it["unit_cents"])}</td>'
                f'<td align="right" style="{cell};padding-left:12px;white-space:nowrap">${fmt_cents(round(it["qty"] * it["unit_cents"]))}</td>'
                '</tr>')
        scope_html = (
            f'<tr><td style="padding:22px 30px 0">'
            f'<div style="font-size:11px;color:{MUTED};letter-spacing:1px">FULL JOB &mdash; ESTIMATE {e(prog["estimate_number"])}</div>'
            f'<div style="font-size:12px;color:{MUTED};padding:2px 0 6px">For reference &mdash; only this '
            f'invoice&rsquo;s portion is charged.</div>'
            '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
            'style="border-collapse:collapse">'
            f'<tr style="font-size:10px;color:{MUTED};letter-spacing:.5px">'
            f'<td style="padding:0 0 4px;border-bottom:1px solid {HAIR}">DESCRIPTION</td>'
            f'<td align="right" style="padding:0 0 4px 10px;border-bottom:1px solid {HAIR}">QTY</td>'
            f'<td align="right" style="padding:0 0 4px 10px;border-bottom:1px solid {HAIR}">UNIT</td>'
            f'<td align="right" style="padding:0 0 4px 12px;border-bottom:1px solid {HAIR}">AMOUNT</td></tr>'
            + rows_html +
            f'<tr><td colspan="3" style="padding:6px 0;color:{MUTED};font-size:12px">Job total (before tax)</td>'
            f'<td align="right" style="padding:6px 0;color:{INK};font-size:12px;white-space:nowrap">${fmt_cents(prog["job_subtotal"])}</td></tr>'
            '</table>'
            f'<div style="font-size:12px;color:{MUTED};padding-top:6px">This invoice '
            f'<strong>{prog["percent"]:g}%</strong> (${fmt_cents(prog["this_subtotal"])}) &middot; billed to date '
            f'${fmt_cents(prog["billed_subtotal"])} &middot; remaining ${fmt_cents(prog["remaining_subtotal"])}</div>'
            '</td></tr>')

    button = ""
    if pay_url:
        button = (
            f'<tr><td style="padding:22px 30px 6px">'
            f'<table cellpadding="0" cellspacing="0" role="presentation"><tr>'
            f'<td align="center" bgcolor="{ACCENT}" style="border-radius:8px">'
            f'<a href="{e(pay_url)}" target="_blank" '
            f'style="display:inline-block;padding:14px 34px;font-size:16px;font-weight:bold;'
            f'color:#ffffff;text-decoration:none;border-radius:8px">Pay here &nbsp;&#8226;&nbsp; ${fmt_cents(total)}</a>'
            f'</td></tr></table>'
            f'<div style="font-size:12px;color:{MUTED};padding-top:8px">Pay securely by {methods_txt} '
            f'via Square. The PDF invoice is attached.</div></td></tr>')

    note_html = e(note).replace("\n", "<br>")
    doc = (
        f'<div style="background:{BG};padding:24px 12px;font-family:Arial,Helvetica,sans-serif">'
        '<table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr><td align="center">'
        f'<table width="600" cellpadding="0" cellspacing="0" role="presentation" '
        f'style="max-width:600px;width:100%;background:#ffffff;border:1px solid {HAIR};border-radius:10px">'
        # header
        f'<tr><td style="padding:28px 30px 0">{logo_html}'
        f'<div style="font-size:15px;font-weight:bold;color:{INK}">{e(biz)}</div>'
        f'<div style="font-size:12px;color:{MUTED};line-height:1.5">{contact}</div></td></tr>'
        # doc title + dates
        '<tr><td style="padding:14px 30px 0"><table width="100%" role="presentation"><tr>'
        f'<td style="font-size:11px;color:{MUTED};letter-spacing:1px">INVOICE<br>'
        f'<span style="font-size:22px;color:{INK};font-weight:bold;letter-spacing:0">{e(inv["number"])}</span></td>'
        f'<td align="right" style="font-size:12px;color:{GRAY};line-height:1.7">Date: {e(inv["date"])}<br>'
        f'Due: {e(inv["due_date"])}</td></tr></table></td></tr>'
        f'{button}'
        # note
        f'<tr><td style="padding:18px 30px 0;font-size:14px;color:#23281f;line-height:1.55">{note_html}</td></tr>'
        # bill to
        f'<tr><td style="padding:18px 30px 0"><div style="font-size:11px;color:{MUTED};letter-spacing:1px">BILL TO</div>'
        f'<div style="font-size:14px;color:{INK};font-weight:bold;padding-top:3px">{e(inv["customer"])}</div>'
        f'<div style="font-size:12px;color:{GRAY};line-height:1.6">{cust_html}</div></td></tr>'
        # line items
        '<tr><td style="padding:14px 30px 0"><table width="100%" cellpadding="0" cellspacing="0" '
        'role="presentation" style="border-collapse:collapse;font-size:13px">'
        f'<tr style="font-size:11px;color:{MUTED};letter-spacing:.5px">'
        f'<td style="padding:0 0 6px;border-bottom:2px solid {HAIR}">DESCRIPTION</td>'
        f'<td align="right" style="padding:0 0 6px 10px;border-bottom:2px solid {HAIR}">QTY</td>'
        f'<td align="right" style="padding:0 0 6px 10px;border-bottom:2px solid {HAIR}">UNIT</td>'
        f'<td align="right" style="padding:0 0 6px 14px;border-bottom:2px solid {HAIR}">AMOUNT</td></tr>'
        f'{rows}</table></td></tr>'
        # totals
        '<tr><td style="padding:12px 30px 0"><table align="right" cellpadding="0" cellspacing="0" '
        f'role="presentation">{totals}</table></td></tr>'
        # full job scope (progress billing only)
        f'{scope_html}'
        # terms / footer
        f'<tr><td style="padding:26px 30px 26px"><div style="border-top:1px solid {HAIR};padding-top:12px;'
        f'font-size:11px;color:{MUTED};line-height:1.6">{e(terms)}</div></td></tr>'
        '</table></td></tr></table></div>')
    return doc


def _apply_invoice_email(msg, con, inv, total, note, plain_body, pay_url=None):
    """Set the plain-text fallback plus the branded HTML alternative (invoice_email_html), embedding
    the company logo inline via cid. The logo is best-effort and never blocks sending; the plain text
    stays the universal fallback."""
    from email.utils import make_msgid
    msg.set_content(plain_body)
    logo = db.company_logo_raster_path(con)   # emails need a raster; SVG uploads get a PNG companion
    cid = make_msgid() if logo else None
    msg.add_alternative(
        invoice_email_html(con, inv, total, note, pay_url,
                           logo_src=(f"cid:{cid[1:-1]}" if logo else "")),
        subtype="html")
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
    subject, note = resolve_email_text(con, inv, total, subject, body)
    plain = note + (f"\n\nPay online (bank transfer or card): {pay_url}\n" if pay_url else "")

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to_addr
    msg["Subject"] = subject
    _apply_invoice_email(msg, con, inv, total, note, plain, pay_url)
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf",
                       filename=f"{inv['number']}.pdf")
    _smtp_send(con, msg)
