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
    pdf.set_font("helvetica", "B", 12)
    pdf.set_text_color(36, 59, 47)
    pdf.cell(80, 6, f"INVOICE {inv['number']}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 9.5)
    pdf.set_text_color(80, 80, 80)
    pdf.set_x(120)
    pdf.cell(80, 5, f"Invoice Date: {inv['date']}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(120)
    pdf.cell(80, 5, f"Payment Due: {inv['due_date']}", align="R", new_x="LMARGIN", new_y="NEXT")
    
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
    pdf.set_font("helvetica", "B", 10.5)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(150, 10, "Total Due:  ", align="R")
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
