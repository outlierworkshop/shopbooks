"""Estimate/quote routes (convert to invoices)."""
from datetime import date as date_cls
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

import db
import invoicing
import ledger
from webutil import ctx, templates
from routes_invoices import _active_items, _insert_line_items, _parse_line_items

router = APIRouter()

def _estimate_rows(con):
    rows = con.execute(
        "SELECT i.*, c.name customer, c.email customer_email FROM invoices i "
        "JOIN customers c ON c.id=i.customer_id WHERE i.kind='estimate' ORDER BY i.id DESC").fetchall()
    return [{**dict(r), "total": invoicing.invoice_total(con, r["id"])} for r in rows]

@router.get("/estimates", response_class=HTMLResponse)
def estimates_page(request: Request, msg: str = "", err: str = ""):
    con = db.connect()
    try:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "estimates.html", ctx(
            request, con, estimates=_estimate_rows(con), customers=customers, msg=msg, err=err,
            email_on=invoicing.email_configured(con)))
    finally:
        con.close()

@router.get("/estimates/new", response_class=HTMLResponse)
def estimate_new(request: Request):
    con = db.connect()
    try:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        standard_items = con.execute("SELECT * FROM items WHERE active=1 ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "estimate_new.html", ctx(
            request, con, customers=customers, standard_items=standard_items, error=None))
    finally:
        con.close()

@router.post("/estimates/new")
async def estimate_create(request: Request):
    form = await request.form()
    con = db.connect()
    try:
        customer_id = int(form["customer_id"])
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
    finally:
        con.close()

@router.get("/estimates/{estimate_id}/edit", response_class=HTMLResponse)
def estimate_edit(request: Request, estimate_id: int):
    con = db.connect()
    try:
        est, items, total = invoicing.get_invoice(con, estimate_id)
        if not est or est["kind"] != "estimate":
            return RedirectResponse("/estimates", status_code=303)
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "invoice_edit.html", ctx(
            request, con, inv=est, items=items, customers=customers, standard_items=_active_items(con), error=None))
    finally:
        con.close()

@router.post("/estimates/{estimate_id}/edit")
async def estimate_update(request: Request, estimate_id: int):
    form = await request.form()
    con = db.connect()
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
    finally:
        con.close()

@router.get("/estimates/{estimate_id}", response_class=HTMLResponse)
def estimate_view(request: Request, estimate_id: int, msg: str = "", err: str = ""):
    con = db.connect()
    try:
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
            email_on=invoicing.email_configured(con),
            biz_address=db.get_setting(con, "business_address", ""),
            biz_email=db.get_setting(con, "business_email", ""),
            biz_phone=db.get_setting(con, "business_phone", ""),
            terms=db.get_setting(con, "invoice_terms", "")))
    finally:
        con.close()

@router.post("/estimates/{estimate_id}/status")
def estimate_status(estimate_id: int, action: str = Form(...)):
    con = db.connect()
    try:
        if action == "delete":
            con.execute("DELETE FROM invoices WHERE id=? AND kind='estimate'", (estimate_id,))
            con.commit()
            return RedirectResponse("/estimates", status_code=303)
        if action in ("draft", "sent", "accepted", "declined"):
            con.execute("UPDATE invoices SET status=? WHERE id=? AND kind='estimate'", (action, estimate_id))
            con.commit()
        return RedirectResponse(f"/estimates/{estimate_id}", status_code=303)
    finally:
        con.close()

@router.get("/estimates/{estimate_id}/pdf")
def estimate_pdf(estimate_id: int):
    con = db.connect()
    try:
        est, items, total = invoicing.get_invoice(con, estimate_id)
        if not est or est["kind"] != "estimate":
            return RedirectResponse("/estimates", status_code=303)
        pdf = invoicing.render_pdf(con, est, items, total)
        return StreamingResponse(iter([pdf]), media_type="application/pdf",
                                 headers={"Content-Disposition": f"inline; filename={est['number']}.pdf"})
    finally:
        con.close()

@router.post("/estimates/{estimate_id}/email")
def estimate_email(estimate_id: int, to_addr: str = Form(...), subject: str = Form(""), body: str = Form("")):
    from urllib.parse import quote
    con = db.connect()
    try:
        est, items, total = invoicing.get_invoice(con, estimate_id)
        if not est or est["kind"] != "estimate":
            return RedirectResponse("/estimates", status_code=303)
        biz = db.get_setting(con, "business_name", "My Business")
        subj = subject.strip() or f"Estimate {est['number']} from {biz}"
        msg = body.strip() or (f"Hi {est['customer']},\n\nAttached is estimate {est['number']} for "
                               f"${ledger.fmt_cents(total)}, valid until {est['due_date']}. "
                               "Let me know if you'd like to proceed.\n\nThank you!")
        pdf = invoicing.render_pdf(con, est, items, total)
        try:
            invoicing.send_invoice_email(con, est, total, pdf, to_addr.strip(), subj, msg)
        except Exception as e:
            return RedirectResponse(f"/estimates/{estimate_id}?err=" + quote(f"Email failed: {e}"), status_code=303)
        con.execute("UPDATE invoices SET status='sent' WHERE id=? AND status='draft'", (estimate_id,))
        con.commit()
        return RedirectResponse(f"/estimates/{estimate_id}?msg=" + quote(f"Emailed to {to_addr}"), status_code=303)
    finally:
        con.close()

@router.post("/estimates/{estimate_id}/convert")
def estimate_convert(estimate_id: int):
    from urllib.parse import quote
    from datetime import timedelta
    con = db.connect()
    try:
        est, items, total = invoicing.get_invoice(con, estimate_id)
        if not est or est["kind"] != "estimate":
            return RedirectResponse("/estimates", status_code=303)
        if est["converted_invoice_id"]:  # already converted — go to the existing invoice
            return RedirectResponse(f"/invoices/{est['converted_invoice_id']}", status_code=303)
        today = date_cls.today()
        number = invoicing.next_number(con)
        cur = con.execute(
            "INSERT INTO invoices(number,customer_id,date,due_date,memo,kind) VALUES(?,?,?,?,?,'invoice')",
            (number, est["customer_id"], today.isoformat(), (today + timedelta(days=30)).isoformat(),
             est["memo"]))
        inv_id = cur.lastrowid
        for it in items:
            con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,item_id,taxable) VALUES(?,?,?,?,?,?)",
                        (inv_id, it["description"], it["qty"], it["unit_cents"], it["item_id"], it["taxable"]))
        con.execute("UPDATE invoices SET status='accepted', converted_invoice_id=? WHERE id=?",
                    (inv_id, estimate_id))
        con.commit()
        return RedirectResponse(f"/invoices/{inv_id}?msg=" + quote(
            f"Invoice {number} created from estimate {est['number']}."), status_code=303)
    finally:
        con.close()
