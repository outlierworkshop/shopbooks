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

    pdf.set_font("helvetica", "B", 20)
    pdf.set_text_color(36, 59, 47)
    pdf.cell(0, 10, _latin(biz), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(90, 90, 90)
    for line in [l for l in (addr.splitlines() + [email, phone]) if l.strip()]:
        pdf.cell(0, 5, _latin(line), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("helvetica", "B", 16)
    pdf.cell(0, 9, f"INVOICE {_latin(inv['number'])}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 5, f"Date: {inv['date']}     Due: {inv['due_date']}", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_font("helvetica", "B", 10)
    pdf.cell(0, 5, "Bill to:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 5, _latin(inv["customer"]), new_x="LMARGIN", new_y="NEXT")
    for line in (inv["customer_address"] or "").splitlines():
        pdf.cell(0, 5, _latin(line), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)
    pdf.set_fill_color(239, 238, 232)
    pdf.set_font("helvetica", "B", 10)
    pdf.cell(110, 8, "Description", fill=True)
    pdf.cell(20, 8, "Qty", fill=True, align="R")
    pdf.cell(30, 8, "Unit", fill=True, align="R")
    pdf.cell(30, 8, "Amount", fill=True, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 10)
    for it in items:
        amt = round(it["qty"] * it["unit_cents"])
        qty = f"{it['qty']:g}"
        pdf.cell(110, 7, _latin(it["description"])[:70])
        pdf.cell(20, 7, qty, align="R")
        pdf.cell(30, 7, f"${fmt_cents(it['unit_cents'])}", align="R")
        pdf.cell(30, 7, f"${fmt_cents(amt)}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "B", 12)
    pdf.cell(160, 10, "Total", align="R")
    pdf.cell(30, 10, f"${fmt_cents(total)}", align="R", new_x="LMARGIN", new_y="NEXT")

    if inv["memo"]:
        pdf.ln(2)
        pdf.set_font("helvetica", "", 10)
        pdf.multi_cell(0, 5, _latin(inv["memo"]))
    if terms:
        pdf.ln(4)
        pdf.set_font("helvetica", "I", 9)
        pdf.set_text_color(110, 110, 110)
        pdf.multi_cell(0, 5, _latin(terms))

    return bytes(pdf.output())


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
    fields = {"number": inv["number"], "business": biz, "customer": inv["customer"],
              "total": fmt_cents(total), "due_date": inv["due_date"], "date": inv["date"]}
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
