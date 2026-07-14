"""Square online-payment routes: test the connection, send an invoice for online payment (ACH +
card) via a Square-hosted page, and sync received payments into the ledger. Logic lives in square.py;
these handlers stay thin and reuse the invoice email + payment machinery in invoicing.py."""
from fastapi import APIRouter, Depends, Form

import invoicing
import square
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
    """Create + publish a Square invoice for this invoice (ACH + card), then email the customer our
    invoice with the Square 'Pay online' link."""
    try:
        res = square.create_and_publish_invoice(con, invoice_id)
    except ValueError as e:
        return safe_redirect(f"/invoices/{invoice_id}", err=str(e))
    except Exception as e:
        return safe_redirect(f"/invoices/{invoice_id}", err=f"Square error: {e}")
    con.commit()

    inv, items, total = invoicing.get_invoice(con, invoice_id)
    pay_url = res.get("public_url") or ""
    to = (inv["customer_email"] or "").strip()
    if to and invoicing.email_configured(con):
        try:
            pdf = invoicing.render_pdf(con, inv, items, total)
            invoicing.send_invoice_email(con, inv, total, pdf, to, pay_url=pay_url)
            con.execute("UPDATE invoices SET status='sent' WHERE id=? AND status='draft'", (invoice_id,))
            con.commit()
            return safe_redirect(f"/invoices/{invoice_id}",
                                 msg=f"Online payment enabled and invoice emailed to {to}.")
        except Exception as e:
            return safe_redirect(f"/invoices/{invoice_id}",
                                 err=f"Payment page created ({pay_url}), but emailing failed: {e}")
    hint = " Add a customer email (and set up SMTP) to email it automatically." if not to else \
        " Set up SMTP in Settings to email it automatically."
    return safe_redirect(f"/invoices/{invoice_id}",
                         msg=f"Online payment page created — share this link: {pay_url}.{hint}")


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
