"""Customer pages: profiles, files, notes, statement reports."""
import mimetypes
import os
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse

import db
import invoicing
from webutil import _INLINE_MEDIA, ctx, templates

router = APIRouter()

@router.post("/customers")
def customer_add(name: str = Form(...), email: str = Form(""), address: str = Form(""),
                 phone: str = Form(""), notes: str = Form("")):
    con = db.connect()
    try:
        con.execute("INSERT INTO customers(name,email,address,phone,notes) VALUES(?,?,?,?,?)",
                    (name.strip(), email.strip(), address.strip(), phone.strip(), notes.strip()))
        con.commit()
        return RedirectResponse("/customers", status_code=303)
    finally:
        con.close()

@router.post("/customers/update")
def customer_update(customer_id: int = Form(...), name: str = Form(...), email: str = Form(""),
                    address: str = Form(""), phone: str = Form(""), notes: str = Form("")):
    con = db.connect()
    try:
        con.execute("UPDATE customers SET name=?, email=?, address=?, phone=?, notes=? WHERE id=?",
                    (name.strip(), email.strip(), address.strip(), phone.strip(), notes.strip(), customer_id))
        con.commit()
        return RedirectResponse(f"/customers/{customer_id}", status_code=303)
    finally:
        con.close()

@router.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request):
    con = db.connect()
    try:
        err = request.query_params.get("err", "")
        msg = request.query_params.get("msg", "")
        
        # Calculate summary metrics
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        summary = []
        ar_total = 0
        credits_total = 0
        
        for c in customers:
            invs = con.execute("SELECT id FROM invoices WHERE customer_id=? AND kind='invoice' AND status!='void'", (c["id"],)).fetchall()
            total_sales = 0
            total_open = 0
            for inv in invs:
                total_sales += invoicing.invoice_total(con, inv["id"])
                total_open += invoicing.invoice_outstanding_balance(con, inv["id"])
            
            credit = invoicing.customer_available_credit(con, c["id"])
            ar_total += total_open
            credits_total += credit
            
            summary.append({
                "id": c["id"],
                "name": c["name"],
                "email": c["email"],
                "phone": c["phone"],
                "address": c["address"],
                "notes": c["notes"],
                "total_sales": total_sales,
                "total_open": total_open,
                "credit": credit
            })
            
        return templates.TemplateResponse(request, "customers.html", ctx(
            request, con,
            customers=summary,
            ar_total=ar_total,
            credits_total=credits_total,
            err=err,
            msg=msg
        ))
    finally:
        con.close()

@router.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_detail(customer_id: int, request: Request):
    con = db.connect()
    try:
        err = request.query_params.get("err", "")
        msg = request.query_params.get("msg", "")
        
        customer = con.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not customer:
            return RedirectResponse("/customers?err=Customer+not+found", status_code=303)
            
        # KPI calculations
        invs = con.execute("SELECT id FROM invoices WHERE customer_id=? AND kind='invoice' AND status!='void'", (customer_id,)).fetchall()
        total_sales = 0
        total_open = 0
        for inv in invs:
            total_sales += invoicing.invoice_total(con, inv["id"])
            total_open += invoicing.invoice_outstanding_balance(con, inv["id"])
            
        credits_avail = invoicing.customer_available_credit(con, customer_id)
        
        # Document files (tax forms, etc.)
        files = con.execute("SELECT * FROM customer_files WHERE customer_id=? ORDER BY uploaded_at DESC", (customer_id,)).fetchall()
        
        # Chronological notes
        notes = con.execute("SELECT * FROM customer_notes WHERE customer_id=? ORDER BY created_at DESC", (customer_id,)).fetchall()
        
        # Invoice and estimates history
        history = con.execute("SELECT * FROM invoices WHERE customer_id=? ORDER BY date DESC, number DESC", (customer_id,)).fetchall()
        
        history_summary = []
        for h in history:
            total = invoicing.invoice_total(con, h["id"])
            open_bal = invoicing.invoice_outstanding_balance(con, h["id"])
            history_summary.append({
                "id": h["id"],
                "number": h["number"],
                "date": h["date"],
                "due_date": h["due_date"],
                "kind": h["kind"],
                "status": h["status"],
                "total": total,
                "outstanding": open_bal
            })
            
        return templates.TemplateResponse(request, "customer_detail.html", ctx(
            request, con,
            customer=customer,
            total_sales=total_sales,
            total_open=total_open,
            credits_avail=credits_avail,
            files=files,
            notes=notes,
            history=history_summary,
            err=err,
            msg=msg
        ))
    finally:
        con.close()

@router.post("/customers/{customer_id}/upload-file")
async def customer_upload_file(customer_id: int, file: UploadFile = File(...), kind: str = Form("tax_form")):
    con = db.connect()
    try:
        customer = con.execute("SELECT name FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not customer:
            return RedirectResponse("/customers?err=Customer+not+found", status_code=303)
            
        cust_dir = db.DOCS / "customer_files"
        cust_dir.mkdir(parents=True, exist_ok=True)
        
        safe_name = "".join(c for c in file.filename if c.isalnum() or c in (".", "-", "_")).strip()
        if not safe_name:
            safe_name = "file"
        
        import uuid
        unique_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
        dest_path = cust_dir / unique_name
        
        with open(dest_path, "wb") as f:
            f.write(await file.read())
            
        con.execute(
            "INSERT INTO customer_files(customer_id, filename, path, kind) VALUES(?,?,?,?)",
            (customer_id, file.filename, str(dest_path.resolve()), kind)
        )
        con.commit()
        
        return RedirectResponse(f"/customers/{customer_id}?msg=File+uploaded+successfully", status_code=303)
    finally:
        con.close()

@router.get("/customers/file/{file_id}")
def customer_download_file(file_id: int):
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM customer_files WHERE id=?", (file_id,)).fetchone()
        if not row:
            return RedirectResponse("/customers?err=File+not+found", status_code=303)
            
        path = row["path"]
        if not os.path.exists(path):
            return PlainTextResponse("File does not exist on disk.", status_code=404)
            
        ext = os.path.splitext(path)[1].lower()
        media = _INLINE_MEDIA.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        
        return FileResponse(path, media_type=media, filename=row["filename"], content_disposition_type="inline")
    finally:
        con.close()

@router.post("/customers/file/{file_id}/delete")
def customer_delete_file(file_id: int):
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM customer_files WHERE id=?", (file_id,)).fetchone()
        if not row:
            return RedirectResponse("/customers?err=File+not+found", status_code=303)
            
        if os.path.exists(row["path"]):
            os.remove(row["path"])
            
        con.execute("DELETE FROM customer_files WHERE id=?", (file_id,))
        con.commit()
        
        return RedirectResponse(f"/customers/{row['customer_id']}?msg=File+deleted", status_code=303)
    finally:
        con.close()

@router.post("/customers/{customer_id}/add-note")
def customer_add_note(customer_id: int, note: str = Form(...)):
    con = db.connect()
    try:
        if not note.strip():
            return RedirectResponse(f"/customers/{customer_id}?err=Note+cannot+be+empty", status_code=303)
            
        con.execute("INSERT INTO customer_notes(customer_id, note) VALUES(?,?)", (customer_id, note.strip()))
        con.commit()
        return RedirectResponse(f"/customers/{customer_id}?msg=Note+added", status_code=303)
    finally:
        con.close()

@router.post("/customers/note/{note_id}/delete")
def customer_delete_note(note_id: int):
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM customer_notes WHERE id=?", (note_id,)).fetchone()
        if not row:
            return RedirectResponse("/customers?err=Note+not+found", status_code=303)
            
        con.execute("DELETE FROM customer_notes WHERE id=?", (note_id,))
        con.commit()
        return RedirectResponse(f"/customers/{row['customer_id']}?msg=Note+deleted", status_code=303)
    finally:
        con.close()

@router.get("/customers/{customer_id}/report", response_class=HTMLResponse)
def customer_report(customer_id: int, request: Request):
    con = db.connect()
    try:
        customer = con.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not customer:
            return RedirectResponse("/customers?err=Customer+not+found", status_code=303)
            
        invoices = con.execute(
            "SELECT id, number, date, kind, status FROM invoices "
            "WHERE customer_id=? AND status!='void' ORDER BY date, id", (customer_id,)
        ).fetchall()
        
        txns = []
        for inv in invoices:
            tot = invoicing.invoice_total(con, inv["id"])
            if inv["kind"] == "credit_memo":
                txns.append({
                    "date": inv["date"],
                    "number": inv["number"],
                    "type": "Credit Memo",
                    "debit_cents": 0,
                    "credit_cents": abs(tot),
                    "id": inv["id"]
                })
            else:
                txns.append({
                    "date": inv["date"],
                    "number": inv["number"],
                    "type": "Invoice",
                    "debit_cents": tot,
                    "credit_cents": 0,
                    "id": inv["id"]
                })
                
                # All payments against this invoice — including multi-payment invoices tracked via
                # invoice_entry_links (invoicing.invoice_payment_entries mirrors invoice_payments_total,
                # so the statement reconciles with the invoice's outstanding balance).
                for p in invoicing.invoice_payment_entries(con, inv["id"]):
                    txns.append({
                        "date": p["date"],
                        "number": f"PMT-{p['entry_id']}",
                        "type": "Payment",
                        "debit_cents": 0,
                        "credit_cents": p["amount_cents"],
                        "id": p["entry_id"]
                    })
                                
        txns.sort(key=lambda x: (x["date"], x["type"] != "Invoice"))
        
        running_bal = 0
        ledger_rows = []
        total_invoiced = 0
        total_payments = 0
        
        for tx in txns:
            running_bal += tx["debit_cents"] - tx["credit_cents"]
            total_invoiced += tx["debit_cents"]
            total_payments += tx["credit_cents"]
            ledger_rows.append({
                "date": tx["date"],
                "number": tx["number"],
                "type": tx["type"],
                "debit": tx["debit_cents"],
                "credit": tx["credit_cents"],
                "balance": running_bal
            })
            
        business_address = db.get_setting(con, "business_address", "")
        business_email = db.get_setting(con, "business_email", "")
        business_phone = db.get_setting(con, "business_phone", "")
        return templates.TemplateResponse(request, "customer_report.html", ctx(
            request, con,
            customer=customer,
            ledger=ledger_rows,
            total_invoiced=total_invoiced,
            total_payments=total_payments,
            ending_balance=running_bal,
            business_address=business_address,
            business_email=business_email,
            business_phone=business_phone
        ))
    finally:
        con.close()
