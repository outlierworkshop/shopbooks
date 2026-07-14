"""Check writing/printing and the payee list. Writing a check posts the payment to the ledger and
records the check; the PDF is laid out for pre-printed 'check on top' 8.5x11 stock."""
from datetime import date as date_cls
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

import checks
import db
import ledger
from webutil import categories, ctx, get_con, safe_redirect, templates

router = APIRouter()

CHECK_CATEGORY_TYPES = ("expense", "liability", "equity", "asset")   # what you'd write a check to


# ----------------------------------------------------------------- payees (mirror the customer form)
@router.get("/payees", response_class=HTMLResponse)
def payees_page(request: Request, msg: str = "", err: str = "", con=Depends(get_con)):
    rows = con.execute("SELECT * FROM payees ORDER BY name").fetchall()
    counts = {r["payee_id"]: r["n"] for r in con.execute(
        "SELECT payee_id, COUNT(*) n FROM checks WHERE payee_id IS NOT NULL GROUP BY payee_id").fetchall()}
    payees = [{**dict(r), "check_count": counts.get(r["id"], 0)} for r in rows]
    return templates.TemplateResponse(request, "payees.html", ctx(request, con, payees=payees, msg=msg, err=err))


@router.post("/payees")
def payee_add(name: str = Form(...), email: str = Form(""), address: str = Form(""),
              phone: str = Form(""), notes: str = Form(""), con=Depends(get_con)):
    if not name.strip():
        return safe_redirect("/payees", err="A payee needs a name.")
    con.execute("INSERT INTO payees(name,email,address,phone,notes) VALUES(?,?,?,?,?)",
                (name.strip(), email.strip(), address.strip(), phone.strip(), notes.strip()))
    con.commit()
    return safe_redirect("/payees", msg="Payee added.")


@router.post("/payees/update")
def payee_update(payee_id: int = Form(...), name: str = Form(...), email: str = Form(""),
                 address: str = Form(""), phone: str = Form(""), notes: str = Form(""), con=Depends(get_con)):
    con.execute("UPDATE payees SET name=?, email=?, address=?, phone=?, notes=? WHERE id=?",
                (name.strip(), email.strip(), address.strip(), phone.strip(), notes.strip(), payee_id))
    con.commit()
    return safe_redirect("/payees", msg="Payee updated.")


@router.post("/payees/{payee_id}/delete")
def payee_delete(payee_id: int, con=Depends(get_con)):
    con.execute("UPDATE checks SET payee_id=NULL WHERE payee_id=?", (payee_id,))  # keep the check's name snapshot
    con.execute("DELETE FROM payees WHERE id=?", (payee_id,))
    con.commit()
    return safe_redirect("/payees", msg="Payee deleted (any checks written to them are kept).")


# ----------------------------------------------------------------- checks
@router.get("/checks", response_class=HTMLResponse)
def checks_page(request: Request, msg: str = "", err: str = "", con=Depends(get_con)):
    return templates.TemplateResponse(request, "checks.html", ctx(
        request, con, checks=checks.list_checks(con), has_bank=bool(checks.bank_accounts(con)),
        msg=msg, err=err))


def _new_ctx(request, con, **over):
    accts = checks.bank_accounts(con)
    next_numbers = {a["id"]: (checks.next_check_number(con, a["id"]) or "") for a in accts}
    base = dict(accounts=accts, next_numbers=next_numbers,
                cats=categories(con, CHECK_CATEGORY_TYPES),
                payees=con.execute("SELECT id, name FROM payees ORDER BY name").fetchall(),
                offset_x=db.get_setting(con, "check_offset_x", "0"),
                offset_y=db.get_setting(con, "check_offset_y", "0"),
                today=date_cls.today().isoformat(), preview=False, form={}, error=None)
    base.update(over)
    return ctx(request, con, **base)


@router.get("/checks/new", response_class=HTMLResponse)
def check_new(request: Request, con=Depends(get_con)):
    if not checks.bank_accounts(con):
        return safe_redirect("/checks", err="Add a bank account first (Settings → Chart of Accounts).")
    return templates.TemplateResponse(request, "check_new.html", _new_ctx(request, con))


def _read_check_form(form):
    """Validate the shared check fields. Returns a dict; raises ValueError with a plain message."""
    account_id = int(form.get("account_id") or 0)
    if not account_id:
        raise ValueError("Choose the bank account to draw the check on.")
    category_id = int(form.get("category_id") or 0)
    if not category_id:
        raise ValueError("Choose what the payment is for (a category).")
    amount_cents = abs(ledger.parse_amount_to_cents(form.get("amount", "")))
    if amount_cents == 0:
        raise ValueError("Enter an amount greater than zero.")
    check_number = int(form.get("check_number") or 0)
    if not check_number:
        raise ValueError("Enter the check number (it's pre-printed on the check).")
    return {"account_id": account_id, "category_id": category_id, "amount_cents": amount_cents,
            "check_number": check_number, "date": ledger.normalize_date(form.get("date", "")),
            "memo": (form.get("memo") or "").strip()}


def _payee_for_preview(con, form):
    """(name, address) for the preview PDF: an existing payee's stored address, or the address typed
    for a brand-new payee. Raises ValueError if no payee is chosen."""
    picked = (form.get("payee_id") or "").strip()
    if picked:
        p = con.execute("SELECT name, address FROM payees WHERE id=?", (int(picked),)).fetchone()
        if p:
            return p["name"], (p["address"] or "")
    name = (form.get("new_payee_name") or "").strip()
    if not name:
        raise ValueError("Pick a payee, or enter a new payee's name.")
    return name, (form.get("new_payee_address") or "").strip()


@router.post("/checks/preview", response_class=HTMLResponse)
async def check_preview(request: Request, con=Depends(get_con)):
    form = await request.form()
    raw = {k: form.get(k, "") for k in ("account_id", "payee_id", "new_payee_name", "new_payee_email",
                                        "new_payee_address", "date", "amount", "category_id", "memo",
                                        "check_number")}
    try:
        fields = _read_check_form(form)
        payee_name, payee_addr = _payee_for_preview(con, form)
    except ValueError as e:
        return templates.TemplateResponse(request, "check_new.html", _new_ctx(request, con, form=raw, error=str(e)))
    pdf_qs = urlencode({"account_id": fields["account_id"], "payee_name": payee_name,
                        "payee_addr": payee_addr,
                        "date": fields["date"], "amount_cents": fields["amount_cents"],
                        "memo": fields["memo"], "category_id": fields["category_id"],
                        "check_number": fields["check_number"]})
    return templates.TemplateResponse(request, "check_new.html", _new_ctx(
        request, con, form=raw, preview=True, pdf_qs=pdf_qs, preview_payee=payee_name,
        preview_amount=ledger.fmt_cents(fields["amount_cents"])))


@router.get("/checks/preview.pdf")
def check_preview_pdf(account_id: int, payee_name: str, date: str, amount_cents: int,
                      category_id: int = 0, memo: str = "", check_number: int = 0,
                      payee_addr: str = "", con=Depends(get_con)):
    chk = {"account_id": account_id, "payee_name": payee_name, "payee_addr": payee_addr, "date": date,
           "amount_cents": amount_cents, "memo": memo, "category_id": category_id or None,
           "check_number": check_number}
    return StreamingResponse(iter([checks.render_check_pdf(con, chk)]), media_type="application/pdf",
                             headers={"Content-Disposition": "inline; filename=check-preview.pdf"})


@router.post("/checks/print")
async def check_print(request: Request, con=Depends(get_con)):
    """The 'confirm — printed correctly' action: post the payment + record the check."""
    form = await request.form()
    try:
        fields = _read_check_form(form)
        payee_id, payee_name = checks.resolve_payee(con, form)
    except ValueError as e:
        raw = {k: form.get(k, "") for k in ("account_id", "payee_id", "new_payee_name", "new_payee_email",
                                            "date", "amount", "category_id", "memo", "check_number")}
        return templates.TemplateResponse(request, "check_new.html", _new_ctx(request, con, form=raw, error=str(e)))
    if con.execute("SELECT 1 FROM checks WHERE account_id=? AND check_number=? AND status='printed'",
                   (fields["account_id"], fields["check_number"])).fetchone():
        raw = {k: form.get(k, "") for k in ("account_id", "payee_id", "new_payee_name", "new_payee_email",
                                            "date", "amount", "category_id", "memo", "check_number")}
        return templates.TemplateResponse(request, "check_new.html", _new_ctx(
            request, con, form=raw, error=f"Check #{fields['check_number']} is already recorded on "
            "that account. Bump the number if the sheet jammed."))
    checks.create_and_post(con, payee_id=payee_id, payee_name=payee_name, **fields)
    con.commit()
    return safe_redirect("/checks", msg=(
        f"Check #{fields['check_number']} recorded and posted (${ledger.fmt_cents(fields['amount_cents'])} "
        f"to {payee_name}). The next check will number {fields['check_number'] + 1}."))


@router.get("/checks/{check_id}/pdf")
def check_pdf(check_id: int, con=Depends(get_con)):
    chk = checks.get_check(con, check_id)
    if not chk:
        return RedirectResponse("/checks", status_code=303)
    return StreamingResponse(iter([checks.render_check_pdf(con, chk)]), media_type="application/pdf",
                             headers={"Content-Disposition": f"inline; filename=check-{chk['check_number']}.pdf"})


@router.post("/checks/{check_id}/void")
def check_void(check_id: int, con=Depends(get_con)):
    try:
        checks.void_check(con, check_id)
        con.commit()
        return safe_redirect("/checks", msg="Check voided and its ledger entry removed.")
    except ledger.LockedPeriodError as e:
        return safe_redirect("/checks", err=str(e))


@router.post("/checks/align")
def check_align(offset_x: str = Form("0"), offset_y: str = Form("0"), con=Depends(get_con)):
    def _num(v):
        try:
            return str(float(v))
        except ValueError:
            return "0"
    db.set_setting(con, "check_offset_x", _num(offset_x))
    db.set_setting(con, "check_offset_y", _num(offset_y))
    con.commit()
    return safe_redirect("/checks/new", msg="Print alignment saved — preview again to check it.")
