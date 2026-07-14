"""Invoice routes: CRUD, payments, credits, PDF/email, AR reminders."""
from datetime import date as date_cls
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse

import db
import invoicing
import ledger
import migrate
import square
from webutil import ctx, get_con, safe_redirect, templates

router = APIRouter()

def _invoice_rows(con):
    rows = con.execute(
        "SELECT i.*, c.name customer, c.email customer_email FROM invoices i "
        "JOIN customers c ON c.id=i.customer_id WHERE i.kind IN ('invoice', 'credit_memo') ORDER BY i.id DESC").fetchall()
    today = date_cls.today().isoformat()
    out = []
    for r in rows:
        total = invoicing.invoice_total(con, r["id"])
        pay_total = invoicing.invoice_payments_total(con, r["id"])
        applied_credits = invoicing.invoice_applied_credits(con, r["id"]) if r["kind"] == "invoice" else invoicing.invoice_credit_sources_total(con, r["id"])
        outstanding_balance = invoicing.invoice_outstanding_balance(con, r["id"])
        overdue = r["status"] in ("sent", "partially_paid") and r["due_date"] < today
        out.append({**dict(r), "total": total, "payments_total": pay_total, "applied_credits": applied_credits, "outstanding_balance": outstanding_balance, "overdue": overdue})
    return out

@router.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, msg: str = "", err: str = "", con=Depends(get_con)):
    customers_raw = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
    customers = []
    for c in customers_raw:
        # Query all open/partially paid invoices & credit memos for this customer
        invs = con.execute(
            "SELECT id FROM invoices WHERE customer_id=? AND kind IN ('invoice', 'credit_memo') AND status IN ('sent', 'partially_paid')",
            (c["id"],)
        ).fetchall()
        outstanding = 0
        for r in invs:
            outstanding += invoicing.invoice_outstanding_balance(con, r["id"])
        credit = invoicing.customer_available_credit(con, c["id"])
        customers.append({**dict(c), "outstanding": outstanding, "credit": credit})

    return templates.TemplateResponse(request, "invoices.html", ctx(
        request, con, invoices=_invoice_rows(con), customers=customers, msg=msg, err=err,
        aging=invoicing.ar_aging(con), email_on=invoicing.email_configured(con)))

@router.post("/invoices/import-qbo")
async def invoices_import_qbo(file: UploadFile = File(...), con=Depends(get_con)):
    try:
        parsed = migrate.parse_invoices(await file.read())
    except ValueError as e:
        return safe_redirect("/invoices", err=str(e))
    created, skipped = migrate.import_invoices(con, parsed)
    con.commit()
    note = (f"Imported {created} invoice(s) from QuickBooks ({skipped} already present). "
            "Records only - these don't post income to your books; income still comes from your "
            "deposit/statement imports.")
    return safe_redirect("/invoices", msg=note)

@router.get("/invoices/new", response_class=HTMLResponse)
def invoice_new(request: Request, con=Depends(get_con)):
    customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
    kind = request.query_params.get("kind", "invoice")
    standard_items = con.execute("SELECT * FROM items WHERE active=1 ORDER BY name").fetchall()
    return templates.TemplateResponse(request, "invoice_new.html", ctx(
        request, con, customers=customers, kind=kind, standard_items=standard_items, error=None))

def _active_items(con):
    """Active catalog products/services for invoice/estimate line dropdowns."""
    return con.execute("SELECT * FROM items WHERE active=1 ORDER BY name").fetchall()

def _parse_line_items(form):
    """Invoice/estimate line rows from the form as (desc, qty, unit_cents, item_id, taxable) tuples,
    skipping blank-description rows. item_id links a line back to the catalog item it was filled from;
    taxable rides a per-row hidden field (a checkbox alone wouldn't submit for unchecked rows and
    would misalign). Both lists are aligned row-for-row with the descriptions."""
    descs = form.getlist("item_desc")
    qtys = form.getlist("item_qty")
    prices = form.getlist("item_price")
    item_ids = form.getlist("item_id")
    taxables = form.getlist("item_taxable")
    if len(item_ids) != len(descs):        # no catalog on the page → no per-row item select posted
        item_ids = [""] * len(descs)
    if len(taxables) != len(descs):
        taxables = ["0"] * len(descs)
    out = []
    for d, q, p, iid, tx in zip(descs, qtys, prices, item_ids, taxables):
        if not d.strip():
            continue
        out.append((d.strip(), float(q or 1), ledger.parse_amount_to_cents(p),
                    int(iid) if (iid and iid.strip()) else None,
                    1 if str(tx).strip() in ("1", "on", "true", "True") else 0))
    return out

def _insert_line_items(con, invoice_id, items):
    """Insert parsed (desc, qty, unit_cents, item_id, taxable) rows for an invoice/estimate."""
    for d, q, u, iid, tx in items:
        con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,item_id,taxable) VALUES(?,?,?,?,?,?)",
                    (invoice_id, d, q, u, iid, tx))

@router.post("/invoices/new")
async def invoice_create(request: Request, con=Depends(get_con)):
    form = await request.form()
    try:
        customer_id = invoicing.resolve_customer_id(con, form)
        inv_date = ledger.normalize_date(form["date"])
        due_date = ledger.normalize_date(form["due_date"])
        kind = form.get("kind", "invoice")
        items = _parse_line_items(form)
        if not items:
            raise ValueError("Add at least one line item.")

        if kind == "credit_memo":
            number = invoicing.next_credit_memo_number(con)
        else:
            number = invoicing.next_number(con)

        cur = con.execute(
            "INSERT INTO invoices(number,customer_id,date,due_date,memo,kind) VALUES(?,?,?,?,?,?)",
            (number, customer_id, inv_date, due_date, form.get("memo", "").strip(), kind))
        inv_id = cur.lastrowid
        _insert_line_items(con, inv_id, items)
        con.commit()
        return RedirectResponse(f"/invoices/{inv_id}", status_code=303)
    except (ValueError, KeyError) as e:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        kind = form.get("kind", "invoice")
        return templates.TemplateResponse(request, "invoice_new.html", ctx(
            request, con, customers=customers, kind=kind, standard_items=_active_items(con), error=str(e)))

@router.get("/invoices/{invoice_id}/edit", response_class=HTMLResponse)
def invoice_edit(request: Request, invoice_id: int, con=Depends(get_con)):
    inv, items, total = invoicing.get_invoice(con, invoice_id)
    if not inv:
        return RedirectResponse("/invoices", status_code=303)
    customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
    standard_items = con.execute("SELECT * FROM items WHERE active=1 ORDER BY name").fetchall()
    return templates.TemplateResponse(request, "invoice_edit.html", ctx(
        request, con, inv=inv, items=items, customers=customers, standard_items=standard_items, error=None))

@router.post("/invoices/{invoice_id}/edit")
async def invoice_update(request: Request, invoice_id: int, con=Depends(get_con)):
    form = await request.form()
    try:
        inv, _, _ = invoicing.get_invoice(con, invoice_id)
        if not inv:
            return RedirectResponse("/invoices", status_code=303)
        customer_id = int(form["customer_id"])
        inv_date = ledger.normalize_date(form["date"])
        due_date = ledger.normalize_date(form["due_date"])
        items = _parse_line_items(form)
        if not items:
            raise ValueError("Add at least one line item.")

        con.execute(
            "UPDATE invoices SET customer_id=?, date=?, due_date=?, memo=? WHERE id=?",
            (customer_id, inv_date, due_date, form.get("memo", "").strip(), invoice_id))
        con.execute("DELETE FROM invoice_items WHERE invoice_id=?", (invoice_id,))
        _insert_line_items(con, invoice_id, items)

        _update_document_status(con, invoice_id)

        _update_entry_customers_for_invoice(con, invoice_id)
        _cleanup_entry_customers(con)
        con.commit()
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    except (ValueError, KeyError) as e:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        inv, items, _ = invoicing.get_invoice(con, invoice_id)
        return templates.TemplateResponse(request, "invoice_edit.html", ctx(
            request, con, inv=inv, items=items, customers=customers, standard_items=_active_items(con), error=str(e)))

def _update_document_status(con, invoice_id):
    inv = con.execute("SELECT kind, status, paid_entry_id FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv or inv["status"] == "void":
        return

    if inv["kind"] == "credit_memo":
        total = abs(invoicing.invoice_total(con, invoice_id))
        applied = con.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM credit_applications WHERE credit_invoice_id=?", (invoice_id,)).fetchone()[0]
        if applied >= total:
            status = "paid"
        elif applied > 0:
            status = "partially_paid"
        else:
            status = "sent"
        con.execute("UPDATE invoices SET status=? WHERE id=?", (status, invoice_id))

    elif inv["kind"] == "invoice":
        total = invoicing.invoice_total(con, invoice_id)
        payments = invoicing.invoice_payments_total(con, invoice_id)
        applied = con.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM credit_applications WHERE invoice_id=?", (invoice_id,)).fetchone()[0]

        total_credited = payments + applied
        if total_credited >= total:
            status = "paid"
            dates = []
            if inv["paid_entry_id"]:
                dates.append(con.execute("SELECT date FROM entries WHERE id=?", (inv["paid_entry_id"],)).fetchone()["date"])
            eids = [r["entry_id"] for r in con.execute("SELECT entry_id FROM invoice_entry_links WHERE invoice_id=?", (invoice_id,)).fetchall()]
            for eid in eids:
                dates.append(con.execute("SELECT date FROM entries WHERE id=?", (eid,)).fetchone()["date"])
            credit_dates = con.execute("SELECT date FROM credit_applications WHERE invoice_id=?", (invoice_id,)).fetchall()
            for cd in credit_dates:
                dates.append(cd["date"])

            paid_date = max(dates) if dates else date_cls.today().isoformat()
            con.execute("UPDATE invoices SET status=?, paid_date=? WHERE id=?", (status, paid_date, invoice_id))
        elif total_credited > 0:
            con.execute("UPDATE invoices SET status='partially_paid', paid_date=NULL WHERE id=?", (invoice_id,))
        else:
            con.execute("UPDATE invoices SET status='sent', paid_date=NULL WHERE id=?", (invoice_id,))

def get_available_credits_for_customer(con, customer_id):
    return invoicing.available_credits_for_customer(con, customer_id)

def _apply_credit_core(con, invoice_id, credit_invoice_id, amount_cents, d):
    """Apply `amount_cents` of credit from credit_invoice_id onto invoice_id. Caps at BOTH the
    source's available credit and the target invoice's remaining balance, so no credit is wasted.
    Updates both documents' statuses. Returns the amount actually applied. Raises ValueError."""
    inv, _, _ = invoicing.get_invoice(con, invoice_id)
    if not inv or inv["kind"] != "invoice" or inv["status"] == "void":
        raise ValueError("Invalid target invoice")
    credit_inv, _, _ = invoicing.get_invoice(con, credit_invoice_id)
    if not credit_inv or credit_inv["customer_id"] != inv["customer_id"]:
        raise ValueError("The credit must belong to the same customer")
    applied = con.execute("SELECT COALESCE(SUM(amount_cents),0) FROM credit_applications "
                          "WHERE credit_invoice_id=?", (credit_invoice_id,)).fetchone()[0]
    if credit_inv["kind"] == "credit_memo":
        avail = abs(invoicing.invoice_total(con, credit_invoice_id)) - applied
    else:
        avail = invoicing.invoice_payments_total(con, credit_invoice_id) - \
            invoicing.invoice_total(con, credit_invoice_id) - applied
    target_outstanding = invoicing.invoice_outstanding_balance(con, invoice_id)
    if target_outstanding <= 0:
        raise ValueError("This invoice has no remaining balance")
    amount_cents = min(amount_cents, avail, target_outstanding)
    if amount_cents <= 0:
        raise ValueError("No available credit on the source")
    con.execute("INSERT INTO credit_applications(credit_invoice_id, invoice_id, amount_cents, date) VALUES(?,?,?,?)",
                (credit_invoice_id, invoice_id, amount_cents, d))
    _update_document_status(con, invoice_id)
    _update_document_status(con, credit_invoice_id)
    return amount_cents

def invoice_deposit_candidates(con, inv, total):
    """Existing income deposits on the books that could be this invoice's payment: an income-leg
    split equal to the invoice total, near the invoice date, not already linked to an invoice."""
    if total <= 0:
        return []
    return con.execute(
        "SELECT DISTINCT e.id, e.date, e.payee, a.name acct FROM entries e "
        "JOIN splits s ON s.entry_id=e.id JOIN accounts a ON a.id=s.account_id "
        "WHERE a.type='income' AND s.amount_cents=? "
        "AND e.id NOT IN (SELECT matched_entry_id FROM invoices WHERE matched_entry_id IS NOT NULL) "
        "AND e.id NOT IN (SELECT paid_entry_id FROM invoices WHERE paid_entry_id IS NOT NULL) "
        "AND e.date BETWEEN date(?, '-5 day') AND date(?, '+120 day') "
        "ORDER BY e.date LIMIT 8", (-total, inv["date"], inv["date"])).fetchall()

def _update_entry_customers_for_invoice(con, invoice_id):
    # Find the customer_id for this invoice
    inv = con.execute("SELECT customer_id FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        return
    cust_id = inv["customer_id"]

    # Find all entries currently linked to this invoice
    eids = [r["entry_id"] for r in con.execute("SELECT entry_id FROM invoice_entry_links WHERE invoice_id=?", (invoice_id,)).fetchall()]
    row = con.execute("SELECT paid_entry_id, matched_entry_id FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if row:
        if row["paid_entry_id"]:
            eids.append(row["paid_entry_id"])
        if row["matched_entry_id"]:
            eids.append(row["matched_entry_id"])

    for eid in set(eids):
        con.execute("UPDATE entries SET customer_id=? WHERE id=?", (cust_id, eid))

def _cleanup_entry_customers(con):
    # Set customer_id to NULL for any entries that are no longer linked to any invoices
    con.execute("""
    UPDATE entries SET customer_id = NULL
    WHERE customer_id IS NOT NULL
      AND id NOT IN (SELECT entry_id FROM invoice_entry_links)
      AND id NOT IN (SELECT paid_entry_id FROM invoices WHERE paid_entry_id IS NOT NULL)
      AND id NOT IN (SELECT matched_entry_id FROM invoices WHERE matched_entry_id IS NOT NULL)
    """)

def _match_invoice_to_entry(con, invoice_id, entry_id):
    """Link an invoice to an existing deposit entry (records-only: no ledger posting)."""
    e = con.execute("SELECT date FROM entries WHERE id=?", (entry_id,)).fetchone()
    if not e:
        return False
    con.execute("INSERT OR IGNORE INTO invoice_entry_links(invoice_id, entry_id) VALUES(?, ?)",
                (invoice_id, entry_id))

    _, _, total = invoicing.get_invoice(con, invoice_id)
    payments_total = invoicing.invoice_payments_total(con, invoice_id)
    status = 'paid' if payments_total >= total else 'partially_paid'

    con.execute("UPDATE invoices SET status=?, paid_date=?, matched_entry_id=? WHERE id=?",
                (status, e["date"], entry_id, invoice_id))
    _update_entry_customers_for_invoice(con, invoice_id)
    return True

@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_view(request: Request, invoice_id: int, msg: str = "", err: str = "", con=Depends(get_con)):
    inv, items, total = invoicing.get_invoice(con, invoice_id)
    if not inv:
        return RedirectResponse("/invoices", status_code=303)
    if inv["kind"] == "estimate":
        return RedirectResponse(f"/estimates/{invoice_id}", status_code=303)
    banks = con.execute("SELECT * FROM accounts WHERE kind='bank' AND active=1").fetchall()
    income = con.execute("SELECT * FROM accounts WHERE type='income' AND active=1 ORDER BY name").fetchall()

    # Get all matched entries for this invoice via invoice_entry_links
    matched_entries = con.execute(
        "SELECT DISTINCT e.id, e.date, e.payee, abs(s.amount_cents) amount_cents, a.name acct "
        "FROM entries e "
        "JOIN splits s ON s.entry_id=e.id "
        "JOIN accounts a ON a.id=s.account_id "
        "JOIN invoice_entry_links iel ON iel.entry_id=e.id "
        "WHERE iel.invoice_id=? AND a.type='income'", (invoice_id,)
    ).fetchall()

    # Support fallback legacy matched_entry_id
    if not matched_entries and inv["matched_entry_id"]:
        row = con.execute(
            "SELECT DISTINCT e.id, e.date, e.payee, abs(s.amount_cents) amount_cents, a.name acct "
            "FROM entries e "
            "JOIN splits s ON s.entry_id=e.id "
            "JOIN accounts a ON a.id=s.account_id "
            "WHERE e.id=? AND a.type='income'", (inv["matched_entry_id"],)
        ).fetchone()
        if row:
            matched_entries = [row]

    matched_entry_ids = {m["id"] for m in matched_entries}

    # Query available deposits
    available_deposits = con.execute(
        "SELECT DISTINCT e.id, e.date, e.payee, abs(s.amount_cents) amount_cents, a.name acct "
        "FROM entries e "
        "JOIN splits s ON s.entry_id=e.id "
        "JOIN accounts a ON a.id=s.account_id "
        "WHERE a.type='income' AND s.amount_cents < 0 "
        "AND e.id NOT IN (SELECT entry_id FROM invoice_entry_links WHERE invoice_id != ?) "
        "AND e.id NOT IN (SELECT matched_entry_id FROM invoices WHERE matched_entry_id IS NOT NULL AND id != ?) "
        "AND e.id NOT IN (SELECT paid_entry_id FROM invoices WHERE paid_entry_id IS NOT NULL AND id != ?) "
        "ORDER BY e.date DESC, e.id DESC "
        "LIMIT 100", (invoice_id, invoice_id, invoice_id)
    ).fetchall()
    available_deposits = list(available_deposits)

    # Ensure currently matched entries are always in the list even if old
    for m in matched_entries:
        if not any(a["id"] == m["id"] for a in available_deposits):
            available_deposits.append(m)
    available_deposits.sort(key=lambda x: (x["date"], x["id"]), reverse=True)

    candidates = None
    matched = matched_entries[0] if matched_entries else None
    if not inv["paid_entry_id"] and inv["status"] != "void" and not matched_entries:
        # sent invoices, and QBO-imported 'paid' ones not yet linked to a deposit
        candidates = invoice_deposit_candidates(con, inv, total)

    payments_total = invoicing.invoice_payments_total(con, invoice_id)

    applied_credits_list = con.execute(
        "SELECT ca.id, ca.amount_cents, ca.date, i.number, i.id credit_invoice_id FROM credit_applications ca "
        "JOIN invoices i ON i.id=ca.credit_invoice_id "
        "WHERE ca.invoice_id=?", (invoice_id,)
    ).fetchall()
    applied_credits_total = invoicing.invoice_applied_credits(con, invoice_id)

    credit_applications_list = con.execute(
        "SELECT ca.id, ca.amount_cents, ca.date, i.number, i.id invoice_id FROM credit_applications ca "
        "JOIN invoices i ON i.id=ca.invoice_id "
        "WHERE ca.credit_invoice_id=?", (invoice_id,)
    ).fetchall()
    credit_applications_total = invoicing.invoice_credit_sources_total(con, invoice_id)

    outstanding_balance = invoicing.invoice_outstanding_balance(con, invoice_id)

    available_credits = []
    if inv["kind"] == "invoice" and outstanding_balance > 0:
        available_credits = get_available_credits_for_customer(con, inv["customer_id"])

    remaining_credit = 0
    if inv["kind"] == "credit_memo":
        remaining_credit = abs(outstanding_balance)
    elif inv["kind"] == "invoice":
        remaining_credit = max(0, payments_total - total - credit_applications_total)

    # For a credit memo with credit left, the open invoices it can be applied to (feature #2)
    applicable_invoices = []
    if inv["kind"] == "credit_memo" and remaining_credit > 0:
        for r in con.execute("SELECT id, number, due_date FROM invoices WHERE customer_id=? AND kind='invoice' "
                             "AND status IN ('sent','partially_paid')", (inv["customer_id"],)).fetchall():
            ob = invoicing.invoice_outstanding_balance(con, r["id"])
            if ob > 0:
                applicable_invoices.append({"id": r["id"], "number": r["number"],
                                            "due_date": r["due_date"], "outstanding": ob})

    return templates.TemplateResponse(request, "invoice_view.html", ctx(
        request, con, inv=inv, items=items, total=total, banks=banks, income=income,
        subtotal=invoicing.invoice_subtotal(con, invoice_id), tax=invoicing.invoice_tax(con, invoice_id),
        candidates=candidates, matched=matched, matched_entries=matched_entries,
        matched_entry_ids=matched_entry_ids, available_deposits=available_deposits,
        payments_total=payments_total, outstanding_balance=outstanding_balance,
        applied_credits_list=applied_credits_list, applied_credits_total=applied_credits_total,
        credit_applications_list=credit_applications_list, credit_applications_total=credit_applications_total,
        available_credits=available_credits, remaining_credit=remaining_credit,
        applicable_invoices=applicable_invoices,
        msg=msg, err=err, email_on=invoicing.email_configured(con),
        square_on=square.configured(con), square_map=square.get_mapping(con, invoice_id),
        biz_address=db.get_setting(con, "business_address", ""),
        biz_email=db.get_setting(con, "business_email", ""),
        biz_phone=db.get_setting(con, "business_phone", ""),
        terms=db.get_setting(con, "invoice_terms", "")))

@router.post("/invoices/{invoice_id}/match")
def invoice_match(invoice_id: int, entry_id: int = Form(...), con=Depends(get_con)):
    _match_invoice_to_entry(con, invoice_id, entry_id)
    con.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

@router.post("/invoices/{invoice_id}/unmatch")
def invoice_unmatch(invoice_id: int, con=Depends(get_con)):
    # only clears the link + paid status; never deletes the deposit entry
    con.execute("UPDATE invoices SET status='sent', paid_date=NULL, matched_entry_id=NULL WHERE id=?",
                (invoice_id,))
    con.execute("DELETE FROM invoice_entry_links WHERE invoice_id=?", (invoice_id,))
    _cleanup_entry_customers(con)
    con.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

@router.post("/invoices/{invoice_id}/save-matches")
async def invoice_save_matches(invoice_id: int, request: Request, con=Depends(get_con)):
    form = await request.form()
    entry_ids = [int(x) for x in form.getlist("entry_ids")]
    con.execute("DELETE FROM invoice_entry_links WHERE invoice_id=?", (invoice_id,))
    for eid in entry_ids:
        con.execute("INSERT OR IGNORE INTO invoice_entry_links(invoice_id, entry_id) VALUES(?, ?)", (invoice_id, eid))

    if entry_ids:
        dates = []
        for eid in entry_ids:
            e = con.execute("SELECT date FROM entries WHERE id=?", (eid,)).fetchone()
            if e:
                dates.append(e["date"])
        latest_date = max(dates) if dates else date_cls.today().isoformat()

        _, _, total = invoicing.get_invoice(con, invoice_id)
        payments_total = invoicing.invoice_payments_total(con, invoice_id)
        status = 'paid' if payments_total >= total else 'partially_paid'

        con.execute(
            "UPDATE invoices SET status=?, paid_date=?, matched_entry_id=? WHERE id=?",
            (status, latest_date, entry_ids[0], invoice_id)
        )
        _update_entry_customers_for_invoice(con, invoice_id)
    else:
        con.execute(
            "UPDATE invoices SET status='sent', paid_date=NULL, matched_entry_id=NULL WHERE id=?",
            (invoice_id,)
        )
    _cleanup_entry_customers(con)
    con.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

@router.post("/invoices/match-all")
def invoices_match_all(con=Depends(get_con)):
    matched = 0
    rows = con.execute("SELECT id FROM invoices WHERE kind='invoice' AND status != 'void' "
                       "AND matched_entry_id IS NULL AND paid_entry_id IS NULL").fetchall()
    for r in rows:
        inv, _, total = invoicing.get_invoice(con, r["id"])
        cands = invoice_deposit_candidates(con, inv, total)
        if len(cands) == 1:
            _match_invoice_to_entry(con, r["id"], cands[0]["id"])
            matched += 1
    con.commit()
    return safe_redirect("/invoices", msg=
        f"Matched {matched} invoice(s) to deposits already on your books (no new entries created).")

@router.get("/invoices/{invoice_id}/pdf")
def invoice_pdf(invoice_id: int, con=Depends(get_con)):
    inv, items, total = invoicing.get_invoice(con, invoice_id)
    if not inv:
        return RedirectResponse("/invoices", status_code=303)
    pdf = invoicing.render_pdf(con, inv, items, total)
    return StreamingResponse(iter([pdf]), media_type="application/pdf",
                             headers={"Content-Disposition": f"inline; filename={inv['number']}.pdf"})

@router.get("/invoices/{invoice_id}/summary")
def invoice_summary(invoice_id: int, con=Depends(get_con)):
    inv, items, total = invoicing.get_invoice(con, invoice_id)
    if not inv:
        return PlainTextResponse("Invoice not found", status_code=404)
    lines = [
        f"Invoice: {inv['number']}",
        f"Customer: {inv['customer']}",
        f"Date: {inv['date']}",
        f"Due: {inv['due_date']}",
        f"Status: {inv['status'].upper()}",
        f"Total: ${ledger.fmt_cents(total)}",
        "",
        "Items:"
    ]
    for it in items:
        amt = round(it["qty"] * it["unit_cents"])
        lines.append(f" - {it['description']} (x{it['qty']:g}): ${ledger.fmt_cents(amt)}")
    if inv["memo"]:
        lines.append("")
        lines.append(f"Memo: {inv['memo']}")
    return PlainTextResponse("\n".join(lines))

@router.post("/invoices/{invoice_id}/status")
def invoice_status(invoice_id: int, action: str = Form(...), con=Depends(get_con)):
    if action == "sent":
        con.execute("UPDATE invoices SET status='sent' WHERE id=? AND status='draft'", (invoice_id,))
    elif action == "void":
        linked = con.execute(
            "SELECT credit_invoice_id FROM credit_applications WHERE invoice_id=? "
            "UNION "
            "SELECT invoice_id FROM credit_applications WHERE credit_invoice_id=?",
            (invoice_id, invoice_id)
        ).fetchall()
        con.execute("DELETE FROM credit_applications WHERE invoice_id=? OR credit_invoice_id=?", (invoice_id, invoice_id))
        con.execute("UPDATE invoices SET status='void' WHERE id=? AND status!='paid'", (invoice_id,))
        for r in linked:
            _update_document_status(con, r[0])
    elif action == "draft":
        con.execute("UPDATE invoices SET status='draft' WHERE id=? AND status IN ('sent','void')", (invoice_id,))
    elif action == "delete":
        linked = con.execute(
            "SELECT credit_invoice_id FROM credit_applications WHERE invoice_id=? "
            "UNION "
            "SELECT invoice_id FROM credit_applications WHERE credit_invoice_id=?",
            (invoice_id, invoice_id)
        ).fetchall()
        con.execute("DELETE FROM invoices WHERE id=? AND status IN ('draft','void')", (invoice_id,))
        for r in linked:
            _update_document_status(con, r[0])
        con.commit()
        return RedirectResponse("/invoices", status_code=303)
    con.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

@router.post("/invoices/{invoice_id}/apply-credit")
def invoice_apply_credit(invoice_id: int, credit_invoice_id: int = Form(...), amount: float = Form(...), apply_date: str = Form(...),
                         con=Depends(get_con)):
    """Apply a credit source (memo or overpaid invoice) ONTO this invoice — from the invoice's side."""
    try:
        amt = ledger.parse_amount_to_cents(str(amount))
        if amt <= 0:
            raise ValueError("Amount must be greater than 0")
        _apply_credit_core(con, invoice_id, credit_invoice_id, amt, ledger.normalize_date(apply_date))
        con.commit()
        return safe_redirect(f"/invoices/{invoice_id}", msg="Credit applied")
    except ValueError as e:
        return safe_redirect(f"/invoices/{invoice_id}", err=str(e))

@router.post("/credit-memos/{credit_id}/apply")
def credit_memo_apply(credit_id: int, invoice_id: int = Form(...), amount: float = Form(...), apply_date: str = Form(...),
                      con=Depends(get_con)):
    """Apply THIS credit (memo or overpaid invoice) to a chosen invoice — from the credit's side (#2)."""
    try:
        amt = ledger.parse_amount_to_cents(str(amount))
        if amt <= 0:
            raise ValueError("Amount must be greater than 0")
        _apply_credit_core(con, invoice_id, credit_id, amt, ledger.normalize_date(apply_date))
        con.commit()
        return safe_redirect(f"/invoices/{credit_id}", msg="Credit applied")
    except ValueError as e:
        return safe_redirect(f"/invoices/{credit_id}", err=str(e))

@router.post("/invoices/{invoice_id}/to-credit-memo")
def invoice_overpayment_to_credit(invoice_id: int, con=Depends(get_con)):
    """Turn an invoice's overpayment excess into a standalone credit memo in one click (#4). The
    excess is moved out of the invoice (recorded as an application onto the new memo), so it is never
    double-counted as available credit."""
    inv, _, total = invoicing.get_invoice(con, invoice_id)
    if not inv or inv["kind"] != "invoice":
        return RedirectResponse("/invoices", status_code=303)
    applied_as_source = con.execute("SELECT COALESCE(SUM(amount_cents),0) FROM credit_applications "
                                    "WHERE credit_invoice_id=?", (invoice_id,)).fetchone()[0]
    excess = invoicing.invoice_payments_total(con, invoice_id) - total - applied_as_source
    if excess <= 0:
        return safe_redirect(f"/invoices/{invoice_id}", err="No overpayment to convert")
    today = date_cls.today().isoformat()
    number = invoicing.next_credit_memo_number(con)
    cm_id = con.execute(
        "INSERT INTO invoices(number,customer_id,date,due_date,memo,kind) VALUES(?,?,?,?,?,'credit_memo')",
        (number, inv["customer_id"], today, today, f"Credit from overpayment on {inv['number']}")).lastrowid
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,?)",
                (cm_id, f"Overpayment credit from {inv['number']}", excess))
    # consume the invoice's excess (source = overpaid invoice, target = the new memo)
    con.execute("INSERT INTO credit_applications(credit_invoice_id, invoice_id, amount_cents, date) VALUES(?,?,?,?)",
                (invoice_id, cm_id, excess, today))
    _update_document_status(con, invoice_id)
    _update_document_status(con, cm_id)
    con.commit()
    return safe_redirect(f"/invoices/{cm_id}", msg=f"Created credit memo {number} from the ${ledger.fmt_cents(excess)} overpayment.")

@router.post("/credit-applications/{application_id}/delete")
def credit_application_delete(application_id: int, back: str = Form(...), con=Depends(get_con)):
    row = con.execute("SELECT credit_invoice_id, invoice_id FROM credit_applications WHERE id=?", (application_id,)).fetchone()
    if row:
        con.execute("DELETE FROM credit_applications WHERE id=?", (application_id,))
        _update_document_status(con, row["invoice_id"])
        _update_document_status(con, row["credit_invoice_id"])
        con.commit()
    return safe_redirect(back)

@router.post("/invoices/{invoice_id}/pay")
def invoice_pay(invoice_id: int, paid_date: str = Form(...), bank_id: int = Form(...),
                income_id: int = Form(...), con=Depends(get_con)):
    inv, items, total = invoicing.get_invoice(con, invoice_id)
    if not inv or inv["status"] == "paid" or total <= 0:
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    payments_total = invoicing.invoice_payments_total(con, invoice_id)
    outstanding = max(0, total - payments_total)
    if outstanding <= 0:
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    # Split the payment so collected sales tax lands in the Sales Tax Payable liability, not income.
    invoicing.record_invoice_payment(con, invoice_id, into_account_id=bank_id, income_id=income_id,
                                     amount_cents=outstanding, date=paid_date)
    _update_entry_customers_for_invoice(con, invoice_id)
    con.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

@router.post("/invoices/{invoice_id}/unpay")
def invoice_unpay(invoice_id: int, con=Depends(get_con)):
    inv = con.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if inv and inv["paid_entry_id"]:
        ledger.delete_entry(con, inv["paid_entry_id"])
    con.execute("UPDATE invoices SET paid_entry_id=NULL WHERE id=?", (invoice_id,))

    # Re-evaluate status based on remaining matches in invoice_entry_links
    rem = con.execute(
        "SELECT e.id, e.date FROM entries e "
        "JOIN invoice_entry_links iel ON iel.entry_id=e.id "
        "WHERE iel.invoice_id=? ORDER BY e.date DESC", (invoice_id,)
    ).fetchall()
    if rem:
        total_payments = 0
        for row_rem in rem:
            eid = row_rem["id"]
            val = con.execute(
                "SELECT SUM(abs(s.amount_cents)) FROM splits s "
                "JOIN accounts a ON a.id=s.account_id "
                "WHERE s.entry_id=? AND a.type='income'", (eid,)
            ).fetchone()[0]
            if val:
                total_payments += val

        _, _, total = invoicing.get_invoice(con, invoice_id)
        status = 'paid' if total_payments >= total else 'partially_paid'
        con.execute(
            "UPDATE invoices SET status=?, paid_date=?, matched_entry_id=? WHERE id=?",
            (status, rem[0]["date"], rem[0]["id"], invoice_id)
        )
    else:
        con.execute(
            "UPDATE invoices SET status='sent', paid_date=NULL, matched_entry_id=NULL WHERE id=?",
            (invoice_id,)
        )
    _cleanup_entry_customers(con)
    con.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

@router.post("/invoices/{invoice_id}/email")
def invoice_email(invoice_id: int, to_addr: str = Form(...), subject: str = Form(""), body: str = Form(""),
                  con=Depends(get_con)):
    inv, items, total = invoicing.get_invoice(con, invoice_id)
    if not inv:
        return RedirectResponse("/invoices", status_code=303)
    pdf = invoicing.render_pdf(con, inv, items, total)
    try:
        invoicing.send_invoice_email(con, inv, total, pdf, to_addr.strip(),
                                     subject.strip() or None, body.strip() or None)
    except Exception as e:
        return safe_redirect(f"/invoices/{invoice_id}", err=f"Email failed: {e}")
    con.execute("UPDATE invoices SET status='sent' WHERE id=? AND status='draft'", (invoice_id,))
    con.commit()
    return safe_redirect(f"/invoices/{invoice_id}", msg=f"Emailed to {to_addr}")

@router.post("/invoices/{invoice_id}/remind")
def invoice_remind(invoice_id: int, con=Depends(get_con)):
    if not invoicing.email_configured(con):
        return safe_redirect(f"/invoices/{invoice_id}", err="Set up SMTP in Settings to send reminders.")
    try:
        res = _reminder_send(con, invoice_id, today=date_cls.today().isoformat())
    except Exception as e:
        return safe_redirect(f"/invoices/{invoice_id}", err=f"Reminder failed: {e}")
    con.commit()
    if res == "sent":
        return safe_redirect(f"/invoices/{invoice_id}", msg="Reminder emailed.")
    msg = ("That customer has no email address — add one on the Invoices page."
           if res == "no_email" else "Nothing to remind — the invoice isn't open.")
    return safe_redirect(f"/invoices/{invoice_id}", err=msg)

@router.post("/invoices/remind-all")
def invoices_remind_all(con=Depends(get_con)):
    if not invoicing.email_configured(con):
        return safe_redirect("/invoices", err="Set up SMTP in Settings to send reminders.")
    today = date_cls.today().isoformat()
    overdue = [r for r in invoicing.ar_aging(con, today)["rows"] if r["overdue"]]
    sent = no_email = skipped = failed = 0
    for r in overdue:
        try:
            res = _reminder_send(con, r["id"], skip_days=7, today=today)
        except Exception:
            failed += 1
            continue
        sent += res == "sent"
        no_email += res == "no_email"
        skipped += res == "skipped"
    con.commit()
    parts = [f"{sent} reminder(s) sent"]
    if skipped:
        parts.append(f"{skipped} skipped (already reminded within 7 days)")
    if no_email:
        parts.append(f"{no_email} with no email on file")
    if failed:
        parts.append(f"{failed} failed to send")
    return safe_redirect("/invoices", msg="; ".join(parts) + ".")

def _reminder_send(con, inv_id, skip_days=0, today=None):
    """Send one overdue reminder. Returns 'sent' | 'no_email' | 'skipped'. Raises on SMTP error."""
    from datetime import datetime
    today = today or date_cls.today().isoformat()
    inv, items, total = invoicing.get_invoice(con, inv_id)
    if not inv or inv["kind"] != "invoice" or inv["status"] not in ("sent", "partially_paid") or total <= 0:
        return "skipped"
    if skip_days and inv["last_reminder_date"]:
        last = datetime.strptime(inv["last_reminder_date"], "%Y-%m-%d")
        if (datetime.strptime(today, "%Y-%m-%d") - last).days < skip_days:
            return "skipped"
    to = (inv["customer_email"] or "").strip()
    if not to:
        return "no_email"
    subj = db.get_setting(con, "reminder_subject", "") or None
    body = db.get_setting(con, "reminder_body", "") or None
    pdf = invoicing.render_pdf(con, inv, items, total)
    invoicing.send_invoice_email(con, inv, total, pdf, to, subj, body)
    con.execute("UPDATE invoices SET last_reminder_date=? WHERE id=?", (today, inv_id))
    return "sent"
