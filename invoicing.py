"""Invoices: queries, PDF rendering (fpdf2), and SMTP email delivery."""
import smtplib
from email.message import EmailMessage

from fpdf import FPDF

import db
from ledger import fmt_cents


def invoice_total(con, invoice_id):
    row = con.execute(
        "SELECT COALESCE(SUM(CAST(round(qty*unit_cents) AS INTEGER)),0) t FROM invoice_items WHERE invoice_id=?",
        (invoice_id,)).fetchone()
    return row["t"]


def invoice_payments_total(con, invoice_id):
    """Calculate the total matched payments for an invoice (integer cents)."""
    row = con.execute("SELECT paid_entry_id, matched_entry_id FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not row:
        return 0
    if row["paid_entry_id"]:
        val = con.execute(
            "SELECT SUM(abs(s.amount_cents)) FROM splits s "
            "JOIN accounts a ON a.id=s.account_id "
            "WHERE s.entry_id=? AND a.type='income'", (row["paid_entry_id"],)
        ).fetchone()[0]
        return val or 0
    
    val = con.execute(
        "SELECT SUM(abs(s.amount_cents)) FROM splits s "
        "JOIN accounts a ON a.id=s.account_id "
        "JOIN invoice_entry_links iel ON iel.entry_id=s.entry_id "
        "WHERE iel.invoice_id=? AND a.type='income'", (invoice_id,)
    ).fetchone()[0]
    
    if not val and row["matched_entry_id"]:
        val = con.execute(
            "SELECT SUM(abs(s.amount_cents)) FROM splits s "
            "JOIN accounts a ON a.id=s.account_id "
            "WHERE s.entry_id=? AND a.type='income'", (row["matched_entry_id"],)
        ).fetchone()[0]
        
    return val or 0


def get_invoice(con, invoice_id):
    inv = con.execute(
        "SELECT i.*, c.name customer, c.email customer_email, c.address customer_address "
        "FROM invoices i JOIN customers c ON c.id=i.customer_id WHERE i.id=?", (invoice_id,)).fetchone()
    if not inv:
        return None, [], 0
    items = con.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall()
    return inv, items, invoice_total(con, invoice_id)


def next_number(con):
    n = int(db.get_setting(con, "next_invoice_number", "1001"))
    db.set_setting(con, "next_invoice_number", str(n + 1))
    return f"INV-{n}"


def next_estimate_number(con):
    n = int(db.get_setting(con, "next_estimate_number", "1001"))
    db.set_setting(con, "next_estimate_number", str(n + 1))
    return f"EST-{n}"


def _kind(inv):
    """Document kind of an invoices row ('invoice' or 'estimate'), tolerant of rows without it."""
    try:
        return inv["kind"] or "invoice"
    except (KeyError, IndexError):
        return "invoice"


def _latin(s):
    return str(s or "").encode("latin-1", "replace").decode("latin-1")


def render_pdf(con, inv, items, total):
    """Build the invoice PDF; returns bytes."""
    biz = db.get_setting(con, "business_name", "My Business")
    addr = db.get_setting(con, "business_address", "")
    email = db.get_setting(con, "business_email", "")
    phone = db.get_setting(con, "business_phone", "")
    terms = db.get_setting(con, "invoice_terms", "")

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(True, margin=18)

    # 1. Header Section
    logo_path = db.REPO_DIR / "static" / "logo.png"
    has_logo = logo_path.exists()
    
    if has_logo:
        # Render logo top left (w=45mm)
        pdf.image(str(logo_path), x=12, y=12, w=45)
        # Render business details top right (right aligned)
        pdf.set_xy(120, 12)
        pdf.set_font("helvetica", "B", 12)
        pdf.set_text_color(36, 59, 47) # dark green
        pdf.cell(0, 5, _latin(biz), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "", 9)
        pdf.set_text_color(90, 90, 90)
        for line in [l for l in (addr.splitlines() + [email, phone]) if l.strip()]:
            pdf.set_x(120)
            pdf.cell(0, 4.5, _latin(line), align="R", new_x="LMARGIN", new_y="NEXT")
    else:
        # Fall back to text header
        pdf.set_font("helvetica", "B", 20)
        pdf.set_text_color(36, 59, 47)
        pdf.cell(0, 10, _latin(biz), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "", 10)
        pdf.set_text_color(90, 90, 90)
        for line in [l for l in (addr.splitlines() + [email, phone]) if l.strip()]:
            pdf.cell(0, 5, _latin(line), new_x="LMARGIN", new_y="NEXT")
            
    # Position below header
    pdf.set_y(max(38, pdf.get_y()))
    
    # 2. Divider line
    pdf.set_draw_color(227, 225, 216) # light beige line
    pdf.set_line_width(0.5)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)
    
    # 3. Bill To and Invoice Metadata Columns
    y_meta = pdf.get_y()
    
    # Left Column: Bill To
    pdf.set_font("helvetica", "B", 9)
    pdf.set_text_color(107, 114, 104) # var(--muted)
    pdf.cell(100, 5, "BILL TO:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "B", 10)
    pdf.set_text_color(31, 36, 33) # var(--ink)
    pdf.cell(100, 5.5, _latin(inv["customer"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 9.5)
    pdf.set_text_color(80, 80, 80)
    for line in (inv["customer_address"] or "").splitlines():
        pdf.cell(100, 5, _latin(line), new_x="LMARGIN", new_y="NEXT")
    if inv["customer_email"]:
        pdf.cell(100, 5, _latin(inv["customer_email"]), new_x="LMARGIN", new_y="NEXT")
        
    y_bill_end = pdf.get_y()
    
    # Right Column: Invoice metadata
    pdf.set_xy(120, y_meta)
    is_est = _kind(inv) == "estimate"
    pdf.set_font("helvetica", "B", 12)
    pdf.set_text_color(36, 59, 47)
    pdf.cell(80, 6, f"{'ESTIMATE' if is_est else 'INVOICE'} {inv['number']}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 9.5)
    pdf.set_text_color(80, 80, 80)
    pdf.set_x(120)
    pdf.cell(80, 5, f"{'Estimate' if is_est else 'Invoice'} Date: {inv['date']}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(120)
    pdf.cell(80, 5, f"{'Valid Until' if is_est else 'Payment Due'}: {inv['due_date']}", align="R", new_x="LMARGIN", new_y="NEXT")
    
    # Position below columns
    pdf.set_y(max(y_bill_end, pdf.get_y()) + 8)
    
    # 4. Itemized Table
    # Table Header
    pdf.set_fill_color(36, 59, 47) # dark forest green
    pdf.set_text_color(255, 255, 255) # white text
    pdf.set_font("helvetica", "B", 9.5)
    pdf.cell(110, 9, "  Description", fill=True)
    pdf.cell(20, 9, "Qty  ", fill=True, align="R")
    pdf.cell(30, 9, "Unit Price  ", fill=True, align="R")
    pdf.cell(30, 9, "Amount  ", fill=True, align="R", new_x="LMARGIN", new_y="NEXT")
    
    # Table Rows
    pdf.set_text_color(31, 36, 33)
    pdf.set_font("helvetica", "", 9.5)
    fill = False
    for it in items:
        # Subtle alternating light background
        pdf.set_fill_color(248, 247, 245)
        amt = round(it["qty"] * it["unit_cents"])
        qty = f"{it['qty']:g}"
        pdf.cell(110, 8, f"  {_latin(it['description'])[:70]}", fill=fill)
        pdf.cell(20, 8, f"{qty}  ", fill=fill, align="R")
        pdf.cell(30, 8, f"${fmt_cents(it['unit_cents'])}  ", fill=fill, align="R")
        pdf.cell(30, 8, f"${fmt_cents(amt)}  ", fill=fill, align="R", new_x="LMARGIN", new_y="NEXT")
        fill = not fill
        
    # Table Bottom Line
    pdf.ln(2)
    pdf.set_draw_color(227, 225, 216)
    pdf.set_line_width(0.5)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    
    # 5. Total Section
    pdf.ln(2)
    payments_total = 0
    if not is_est:
        payments_total = invoice_payments_total(con, inv["id"])

    if payments_total > 0:
        pdf.set_font("helvetica", "B", 10.5)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(150, 6, "Total:  ", align="R")
        pdf.set_font("helvetica", "", 10.5)
        pdf.cell(40, 6, f"${fmt_cents(total)}  ", align="R", new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("helvetica", "B", 10.5)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(150, 6, "Payments/Credits:  ", align="R")
        pdf.set_font("helvetica", "", 10.5)
        pdf.cell(40, 6, f"-${fmt_cents(payments_total)}  ", align="R", new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("helvetica", "B", 10.5)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(150, 8, "Remaining Balance Due:  ", align="R")
        pdf.set_font("helvetica", "B", 13)
        pdf.set_text_color(36, 59, 47)
        pdf.cell(40, 8, f"${fmt_cents(max(0, total - payments_total))}  ", align="R", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_font("helvetica", "B", 10.5)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(150, 10, ("Estimated Total:  " if is_est else "Total Due:  "), align="R")
        pdf.set_font("helvetica", "B", 14)
        pdf.set_text_color(36, 59, 47)
        pdf.cell(40, 10, f"${fmt_cents(total)}  ", align="R", new_x="LMARGIN", new_y="NEXT")
    
    # 6. Notes & Terms
    pdf.set_text_color(31, 36, 33)
    if inv["memo"]:
        pdf.ln(4)
        pdf.set_font("helvetica", "B", 9)
        pdf.set_text_color(107, 114, 104)
        pdf.cell(0, 5, "NOTES", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "", 9.5)
        pdf.set_text_color(80, 80, 80)
        pdf.multi_cell(0, 5, _latin(inv["memo"]))
        
    if terms:
        pdf.ln(4)
        pdf.set_font("helvetica", "B", 9)
        pdf.set_text_color(107, 114, 104)
        pdf.cell(0, 5, "TERMS & CONDITIONS", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "I", 9)
        pdf.set_text_color(110, 110, 110)
        pdf.multi_cell(0, 4.5, _latin(terms))

    return bytes(pdf.output())


def ar_aging(con, today=None):
    """Accounts-receivable aging: every open (sent, unpaid, or partially paid) invoice bucketed by how overdue it is.
    Deterministic — totals come straight from the line items. `current` = not yet past due."""
    from datetime import date, datetime
    today = today or date.today().isoformat()
    td = datetime.strptime(today, "%Y-%m-%d")
    rows = con.execute(
        "SELECT i.id, i.number, i.date, i.due_date, i.last_reminder_date, "
        "c.name customer, c.email customer_email "
        "FROM invoices i JOIN customers c ON c.id=i.customer_id "
        "WHERE i.kind='invoice' AND i.status IN ('sent', 'partially_paid') ORDER BY i.due_date").fetchall()
    buckets = {"current": 0, "1-30": 0, "31-60": 0, "61-90": 0, "90+": 0}
    out = []
    for r in rows:
        total = invoice_total(con, r["id"])
        payments_total = invoice_payments_total(con, r["id"])
        outstanding = max(0, total - payments_total)
        if outstanding <= 0:
            continue
        days = (td - datetime.strptime(r["due_date"], "%Y-%m-%d")).days
        b = ("current" if days <= 0 else "1-30" if days <= 30 else "31-60" if days <= 60
             else "61-90" if days <= 90 else "90+")
        buckets[b] += outstanding
        out.append({**dict(r), "total": total, "payments_total": payments_total, "outstanding": outstanding,
                    "days_overdue": max(days, 0), "bucket": b, "overdue": days > 0})
    total_out = sum(buckets.values())
    return {"rows": out, "buckets": buckets, "total": total_out,
            "overdue_total": total_out - buckets["current"],
            "overdue_count": sum(1 for r in out if r["overdue"]),
            "open_count": len(out)}


def email_configured(con):
    return bool(db.get_setting(con, "smtp_user", "") and db.get_setting(con, "smtp_password", ""))


def send_invoice_email(con, inv, total, pdf_bytes, to_addr, subject=None, body=None):
    """Send the invoice PDF over SMTP. Raises on failure with a readable message."""
    host = db.get_setting(con, "smtp_host", "smtp.gmail.com")
    port = int(db.get_setting(con, "smtp_port", "587"))
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

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf",
                       filename=f"{inv['number']}.pdf")
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)
