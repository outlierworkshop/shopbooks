"""Square online-payment routes: test the connection, send an invoice for online payment (ACH +
card) via a Square-hosted page, and sync received payments into the ledger. Logic lives in square.py;
these handlers stay thin and reuse the invoice email + payment machinery in invoicing.py."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

import invoicing
import square
from routes_invoices import _email_preview_page
from webutil import get_con, safe_redirect

router = APIRouter()


@router.post("/square/test")
def square_test(con=Depends(get_con)):
    """Validate the saved Square token + location (Settings 'Test connection' button)."""
    try:
        return safe_redirect("/settings#square", msg=square.test_connection(con))
    except Exception as e:
        return safe_redirect("/settings#square", err=str(e))


@router.post("/invoices/{invoice_id}/square-send")
def square_send(invoice_id: int, con=Depends(get_con)):
    """Create + publish the Square payment page for this invoice (ACH + card). It does NOT email —
    the pay link only exists once the page is made, so the email gets its own preview step
    (square-email/preview) and nothing goes out unseen."""
    try:
        res = square.create_and_publish_invoice(con, invoice_id)
    except ValueError as e:
        return safe_redirect(f"/invoices/{invoice_id}", err=str(e))
    except Exception as e:
        return safe_redirect(f"/invoices/{invoice_id}", err=f"Square error: {e}")
    con.commit()
    return safe_redirect(f"/invoices/{invoice_id}", msg=(
        f"Payment page created — {res.get('public_url') or 'link ready'}. "
        "Use 'Preview & send email' to send the customer the pay link."))


@router.post("/invoices/{invoice_id}/square-email/preview", response_class=HTMLResponse)
def square_email_preview(request: Request, invoice_id: int, con=Depends(get_con)):
    """Preview the invoice email carrying the Square pay link, before sending it."""
    inv, _items, total = invoicing.get_invoice(con, invoice_id)
    m = square.get_mapping(con, invoice_id)
    if not inv or not m or not m["public_url"]:
        return safe_redirect(f"/invoices/{invoice_id}",
                             err="Create the payment page first (Collect online).")
    to = (inv["customer_email"] or "").strip()
    if not to:
        return safe_redirect(f"/invoices/{invoice_id}", err="This customer has no email address.")
    if not invoicing.email_configured(con):
        return safe_redirect(f"/invoices/{invoice_id}", err="Set up SMTP in Settings to send email.")
    return _email_preview_page(request, con, inv, total, to=to, subject="", body="",
                               pay_url=m["public_url"],
                               send_action=f"/invoices/{invoice_id}/square-email",
                               cancel_url=f"/invoices/{invoice_id}",
                               heading="Review the pay-link email before it goes out", hidden={})


@router.post("/invoices/{invoice_id}/square-email")
def square_email(invoice_id: int, con=Depends(get_con)):
    """Send the invoice email with the Square pay link (confirmed from the preview)."""
    inv, items, total = invoicing.get_invoice(con, invoice_id)
    m = square.get_mapping(con, invoice_id)
    if not inv or not m or not m["public_url"]:
        return safe_redirect(f"/invoices/{invoice_id}", err="Create the payment page first.")
    to = (inv["customer_email"] or "").strip()
    try:
        pdf = invoicing.render_pdf(con, inv, items, total)
        invoicing.send_invoice_email(con, inv, total, pdf, to, pay_url=m["public_url"])
    except Exception as e:
        return safe_redirect(f"/invoices/{invoice_id}", err=f"Email failed: {e}")
    con.execute("UPDATE invoices SET status='sent' WHERE id=? AND status='draft'", (invoice_id,))
    con.commit()
    return safe_redirect(f"/invoices/{invoice_id}", msg=f"Invoice with the pay link emailed to {to}.")


@router.post("/square/sync")
def square_sync(back: str = Form("/invoices"), con=Depends(get_con)):
    """Poll Square for payments and book any that came in (the manual 'Sync' button)."""
    dest = back if back.startswith("/") else "/invoices"
    try:
        res = square.sync_payments(con)
    except ValueError as e:
        return safe_redirect(dest, err=str(e))
    except Exception as e:
        return safe_redirect(dest, err=f"Square sync failed: {e}")
    if res["recorded"] or res["fees"]:
        return safe_redirect(dest, msg=f"Square sync — recorded {res['recorded']} payment(s) and "
                             f"booked {res['fees']} fee(s).")
    return safe_redirect(dest, msg="Square sync — no new payments.")
