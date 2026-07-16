"""Estimate/quote routes (convert to invoices)."""
from datetime import date as date_cls
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

import db
import invoicing
import ledger
from webutil import ctx, get_con, safe_redirect, templates
from routes_invoices import _active_items, _email_preview_page, _insert_line_items, _parse_line_items

router = APIRouter()

def _estimate_rows(con):
    rows = con.execute(
        "SELECT i.*, c.name customer, c.email customer_email FROM invoices i "
        "JOIN customers c ON c.id=i.customer_id WHERE i.kind='estimate' ORDER BY i.id DESC").fetchall()
    return [{**dict(r), "total": invoicing.invoice_total(con, r["id"])} for r in rows]

@router.get("/estimates", response_class=HTMLResponse)
def estimates_page(request: Request, msg: str = "", err: str = "", con=Depends(get_con)):
    customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
    return templates.TemplateResponse(request, "estimates.html", ctx(
        request, con, estimates=_estimate_rows(con), customers=customers, msg=msg, err=err,
        email_on=invoicing.email_configured(con)))

@router.get("/estimates/new", response_class=HTMLResponse)
def estimate_new(request: Request, con=Depends(get_con)):
    customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
    standard_items = con.execute("SELECT * FROM items WHERE active=1 ORDER BY name").fetchall()
    return templates.TemplateResponse(request, "estimate_new.html", ctx(
        request, con, customers=customers, standard_items=standard_items, error=None))

@router.post("/estimates/new")
async def estimate_create(request: Request, con=Depends(get_con)):
    form = await request.form()
    try:
        customer_id = invoicing.resolve_customer_id(con, form)
        est_date = ledger.normalize_date(form["date"])
        valid_until = ledger.normalize_date(form["valid_until"])
        items = _parse_line_items(form)
        if not items:
            raise ValueError("Add at least one line item.")
        number = invoicing.next_estimate_number(con)
        cur = con.execute(
            "INSERT INTO invoices(number,customer_id,date,due_date,memo,kind) VALUES(?,?,?,?,?,'estimate')",
            (number, customer_id, est_date, valid_until, form.get("memo", "").strip()))
        est_id = cur.lastrowid
        _insert_line_items(con, est_id, items)
        con.commit()
        return RedirectResponse(f"/estimates/{est_id}", status_code=303)
    except (ValueError, KeyError) as e:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "estimate_new.html", ctx(
            request, con, customers=customers, standard_items=_active_items(con), error=str(e)))

@router.get("/estimates/{estimate_id}/edit", response_class=HTMLResponse)
def estimate_edit(request: Request, estimate_id: int, con=Depends(get_con)):
    est, items, total = invoicing.get_invoice(con, estimate_id)
    if not est or est["kind"] != "estimate":
        return RedirectResponse("/estimates", status_code=303)
    customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
    return templates.TemplateResponse(request, "invoice_edit.html", ctx(
        request, con, inv=est, items=items, customers=customers, standard_items=_active_items(con), error=None))

@router.post("/estimates/{estimate_id}/edit")
async def estimate_update(request: Request, estimate_id: int, con=Depends(get_con)):
    form = await request.form()
    try:
        est, _, _ = invoicing.get_invoice(con, estimate_id)
        if not est or est["kind"] != "estimate":
            return RedirectResponse("/estimates", status_code=303)
        customer_id = int(form["customer_id"])
        est_date = ledger.normalize_date(form["date"])
        due_date = ledger.normalize_date(form["due_date"])
        items = _parse_line_items(form)
        if not items:
            raise ValueError("Add at least one line item.")

        con.execute(
            "UPDATE invoices SET customer_id=?, date=?, due_date=?, memo=? WHERE id=?",
            (customer_id, est_date, due_date, form.get("memo", "").strip(), estimate_id))
        con.execute("DELETE FROM invoice_items WHERE invoice_id=?", (estimate_id,))
        _insert_line_items(con, estimate_id, items)
        con.commit()
        return RedirectResponse(f"/estimates/{estimate_id}", status_code=303)
    except (ValueError, KeyError) as e:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        est, items, _ = invoicing.get_invoice(con, estimate_id)
        return templates.TemplateResponse(request, "invoice_edit.html", ctx(
            request, con, inv=est, items=items, customers=customers, standard_items=_active_items(con), error=str(e)))

def _progress_invoices(con, estimate_id):
    """The invoices billed against this estimate, oldest first, each with its own total."""
    rows = con.execute("SELECT id, number, date, status FROM invoices WHERE estimate_id=? "
                       "AND kind='invoice' ORDER BY id", (estimate_id,)).fetchall()
    return [{**dict(r), "total": invoicing.invoice_total(con, r["id"]),
             "subtotal": invoicing.invoice_subtotal(con, r["id"])} for r in rows]


@router.get("/estimates/{estimate_id}", response_class=HTMLResponse)
def estimate_view(request: Request, estimate_id: int, msg: str = "", err: str = "", con=Depends(get_con)):
    est, items, total = invoicing.get_invoice(con, estimate_id)
    if not est or est["kind"] != "estimate":
        return RedirectResponse("/estimates", status_code=303)
    converted = None
    if est["converted_invoice_id"]:
        converted = con.execute("SELECT id, number FROM invoices WHERE id=?",
                                (est["converted_invoice_id"],)).fetchone()
    return templates.TemplateResponse(request, "estimate_view.html", ctx(
        request, con, inv=est, items=items, total=total, converted=converted, msg=msg, err=err,
        subtotal=invoicing.invoice_subtotal(con, estimate_id), tax=invoicing.invoice_tax(con, estimate_id),
        billed_subtotal=invoicing.estimate_billed_subtotal(con, estimate_id),
        remaining_subtotal=invoicing.estimate_remaining_subtotal(con, estimate_id),
        progress_invoices=_progress_invoices(con, estimate_id),
        email_on=invoicing.email_configured(con),
        biz_address=db.get_setting(con, "business_address", ""),
        biz_email=db.get_setting(con, "business_email", ""),
        biz_phone=db.get_setting(con, "business_phone", ""),
        terms=db.get_setting(con, "invoice_terms", "")))

@router.post("/estimates/{estimate_id}/status")
def estimate_status(estimate_id: int, action: str = Form(...), con=Depends(get_con)):
    if action == "delete":
        con.execute("DELETE FROM invoices WHERE id=? AND kind='estimate'", (estimate_id,))
        con.commit()
        return RedirectResponse("/estimates", status_code=303)
    if action in ("draft", "sent", "accepted", "declined"):
        con.execute("UPDATE invoices SET status=? WHERE id=? AND kind='estimate'", (action, estimate_id))
        con.commit()
    return RedirectResponse(f"/estimates/{estimate_id}", status_code=303)

@router.get("/estimates/{estimate_id}/pdf")
def estimate_pdf(estimate_id: int, con=Depends(get_con)):
    est, items, total = invoicing.get_invoice(con, estimate_id)
    if not est or est["kind"] != "estimate":
        return RedirectResponse("/estimates", status_code=303)
    pdf = invoicing.render_pdf(con, est, items, total)
    return StreamingResponse(iter([pdf]), media_type="application/pdf",
                             headers={"Content-Disposition": f"inline; filename={est['number']}.pdf"})

def _estimate_email_text(con, est, total, subject, body):
    """The quote's subject/message: what was typed, else the estimate defaults. Shared by the preview
    and the send so they can't drift apart."""
    biz = db.get_setting(con, "business_name", "My Business")
    subj = (subject or "").strip() or f"Estimate {est['number']} from {biz}"
    msg = (body or "").strip() or (f"Hi {est['customer']},\n\nAttached is estimate {est['number']} for "
                                   f"${ledger.fmt_cents(total)}, valid until {est['due_date']}. "
                                   "Let me know if you'd like to proceed.\n\nThank you!")
    return subj, msg


@router.post("/estimates/{estimate_id}/email/preview", response_class=HTMLResponse)
def estimate_email_preview(request: Request, estimate_id: int, to_addr: str = Form(...),
                           subject: str = Form(""), body: str = Form(""), con=Depends(get_con)):
    est, _items, total = invoicing.get_invoice(con, estimate_id)
    if not est or est["kind"] != "estimate":
        return RedirectResponse("/estimates", status_code=303)
    subj, msg = _estimate_email_text(con, est, total, subject, body)
    return _email_preview_page(request, con, est, total, to=to_addr.strip(), subject=subj, body=msg,
                               send_action=f"/estimates/{estimate_id}/email",
                               cancel_url=f"/estimates/{estimate_id}",
                               heading="Review this quote email before it goes out")


@router.post("/estimates/{estimate_id}/email")
def estimate_email(estimate_id: int, to_addr: str = Form(...), subject: str = Form(""), body: str = Form(""),
                   con=Depends(get_con)):
    est, items, total = invoicing.get_invoice(con, estimate_id)
    if not est or est["kind"] != "estimate":
        return RedirectResponse("/estimates", status_code=303)
    subj, msg = _estimate_email_text(con, est, total, subject, body)
    pdf = invoicing.render_pdf(con, est, items, total)
    try:
        invoicing.send_invoice_email(con, est, total, pdf, to_addr.strip(), subj, msg)
    except Exception as e:
        return safe_redirect(f"/estimates/{estimate_id}", err=f"Email failed: {e}")
    con.execute("UPDATE invoices SET status='sent' WHERE id=? AND status='draft'", (estimate_id,))
    con.commit()
    return safe_redirect(f"/estimates/{estimate_id}", msg=f"Emailed to {to_addr}")

def _billed_lines(portion, job_sub, items):
    """Split a portion of the job's PRE-TAX subtotal into billed line(s) — one per tax treatment the
    estimate uses — proportional to its taxable vs non-taxable mix, so the tax the engine adds is
    right. The rounding remainder goes to the last line, so the lines sum to `portion` exactly.
    Returns [(amount_cents, taxable), ...]."""
    taxable_sub = sum(round(it["qty"] * it["unit_cents"]) for it in items if it["taxable"])
    nontax_sub = job_sub - taxable_sub
    if taxable_sub and nontax_sub:                      # mixed estimate → two lines
        tax_part = round(portion * taxable_sub / job_sub)
        return [(tax_part, 1), (portion - tax_part, 0)]
    return [(portion, 1 if taxable_sub else 0)]         # all taxable, or none


@router.post("/estimates/{estimate_id}/bill")
def estimate_bill(estimate_id: int, portion_kind: str = Form("percent"), portion_value: str = Form(""),
                  con=Depends(get_con)):
    """Progress-bill part of an estimate: create an invoice for a percentage OR a dollar amount of the
    job's PRE-TAX subtotal (tax is then added per invoice by the normal engine, so the portions sum to
    the estimate exactly). The invoice's line(s) ARE the portion, so it's worth exactly what it bills;
    the estimate keeps the full scope and tracks billed-to-date."""
    from datetime import timedelta
    est, items, _ = invoicing.get_invoice(con, estimate_id)
    if not est or est["kind"] != "estimate":
        return RedirectResponse("/estimates", status_code=303)
    job_sub = invoicing.invoice_subtotal(con, estimate_id)
    remaining = invoicing.estimate_remaining_subtotal(con, estimate_id)
    if job_sub <= 0:
        return safe_redirect(f"/estimates/{estimate_id}", err="This estimate has nothing to bill.")
    if remaining <= 0:
        return safe_redirect(f"/estimates/{estimate_id}", err="This estimate is already fully billed.")
    try:
        if portion_kind == "percent":
            portion = round(job_sub * float(portion_value) / 100.0)
        else:
            portion = ledger.parse_amount_to_cents(portion_value)
    except (ValueError, TypeError):
        return safe_redirect(f"/estimates/{estimate_id}",
                             err="Enter a percentage or a dollar amount to bill.")
    if portion <= 0:
        return safe_redirect(f"/estimates/{estimate_id}", err="Enter an amount greater than zero.")
    portion = min(portion, remaining)   # never bill past the job

    today = date_cls.today()
    number = invoicing.next_number(con)
    cur = con.execute(
        "INSERT INTO invoices(number,customer_id,date,due_date,memo,kind,estimate_id) "
        "VALUES(?,?,?,?,?,'invoice',?)",
        (number, est["customer_id"], today.isoformat(), (today + timedelta(days=30)).isoformat(),
         est["memo"], estimate_id))
    inv_id = cur.lastrowid
    lines = _billed_lines(portion, job_sub, items)
    pct = round(portion * 100.0 / job_sub, 1)
    pct_txt = f"{pct:g}%"
    for amt, taxable in lines:
        if amt <= 0:
            continue
        desc = f"{pct_txt} of estimate {est['number']}"
        if len(lines) > 1:
            desc += " (taxable)" if taxable else " (non-taxable)"
        con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,item_id,taxable) "
                    "VALUES(?,?,1,?,NULL,?)", (inv_id, desc, amt, taxable))
    # Accepted once anything is billed; converted_invoice_id keeps pointing at the FIRST invoice so the
    # existing "View invoice" link on the estimate still works.
    if est["converted_invoice_id"]:
        con.execute("UPDATE invoices SET status='accepted' WHERE id=?", (estimate_id,))
    else:
        con.execute("UPDATE invoices SET status='accepted', converted_invoice_id=? WHERE id=?",
                    (inv_id, estimate_id))
    con.commit()
    return safe_redirect(f"/invoices/{inv_id}", msg=(
        f"Invoice {number} bills {pct_txt} of estimate {est['number']} "
        f"(${ledger.fmt_cents(portion)} of ${ledger.fmt_cents(job_sub)} before tax)."))


@router.post("/estimates/{estimate_id}/convert")
def estimate_convert(estimate_id: int, con=Depends(get_con)):
    from datetime import timedelta
    est, items, total = invoicing.get_invoice(con, estimate_id)
    if not est or est["kind"] != "estimate":
        return RedirectResponse("/estimates", status_code=303)
    if est["converted_invoice_id"]:  # already converted — go to the existing invoice
        return RedirectResponse(f"/invoices/{est['converted_invoice_id']}", status_code=303)
    today = date_cls.today()
    number = invoicing.next_number(con)
    # estimate_id links it back to the job so billed-to-date is accurate (a full convert bills 100%,
    # which correctly leaves nothing to progress-bill).
    cur = con.execute(
        "INSERT INTO invoices(number,customer_id,date,due_date,memo,kind,estimate_id) "
        "VALUES(?,?,?,?,?,'invoice',?)",
        (number, est["customer_id"], today.isoformat(), (today + timedelta(days=30)).isoformat(),
         est["memo"], estimate_id))
    inv_id = cur.lastrowid
    for it in items:
        con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,item_id,taxable) VALUES(?,?,?,?,?,?)",
                    (inv_id, it["description"], it["qty"], it["unit_cents"], it["item_id"], it["taxable"]))
    con.execute("UPDATE invoices SET status='accepted', converted_invoice_id=? WHERE id=?",
                (inv_id, estimate_id))
    con.commit()
    return safe_redirect(f"/invoices/{inv_id}", msg=f"Invoice {number} created from estimate {est['number']}.")
