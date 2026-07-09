"""Register views, manual entries, entry edit/split/delete, duplicates."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import duplicates
import ledger
from webutil import _active_jobs, _entry_sources, categories, ctx, get_con, safe_redirect, templates

router = APIRouter()

@router.get("/register/{account_id}", response_class=HTMLResponse)
def register_view(request: Request, account_id: int, msg: str = "", err: str = "", con=Depends(get_con)):
    acct, rows = ledger.register(con, account_id)
    # Attach each entry's category legs (the non-register side) + its money-in/out direction so
    # the register can offer an inline "Split across categories" editor prefilled with them.
    for r in rows:
        legs = ledger.entry_legs(con, r["entry_id"])
        reg_leg = next((l for l in legs if l["account_id"] == account_id), None)
        r["cat_legs"] = [{"account_id": l["account_id"], "name": l["name"],
                          "magnitude": abs(l["amount_cents"])}
                         for l in legs if l["account_id"] != account_id]
        r["direction"] = "in" if (reg_leg and reg_leg["amount_cents"] > 0) else "out"
    bal = ledger.display_balance(acct["type"], ledger.raw_balance(con, account_id))
    customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
    return templates.TemplateResponse(request, "register.html", ctx(
        request, con, acct=acct, rows=rows, balance=bal, jobs=_active_jobs(con),
        customers=customers, cats=categories(con),
        bank_cards=ledger.accounts_with_balances(con, kinds=('bank', 'card')), msg=msg, err=err))

@router.post("/entry/edit/{entry_id}")
def entry_edit(entry_id: int,
               date: str = Form(...),
               payee: str = Form(...),
               memo: str = Form(""),
               account_id: str = Form(None),
               category_id: str = Form(None),
               job_id: str = Form(""),
               customer_id: str = Form(""),
               register_account_id: int = Form(None),
               back: str = Form("/"),
               con=Depends(get_con)):
    try:
        norm_date = ledger.normalize_date(date)
        cat_id = int(category_id) if category_id and category_id.strip() else None
        new_reg_acct_id = int(account_id) if account_id and account_id.strip() else None
        job_val = int(job_id) if job_id and job_id.strip() else None
        cust_val = int(customer_id) if customer_id and customer_id.strip() else None

        ledger.update_entry_fields(con, entry_id, payee, memo, cat_id, job_val, norm_date, register_account_id, new_reg_acct_id, customer_id=cust_val)
        con.commit()
        return safe_redirect(back)
    except ValueError as e:
        return safe_redirect(back, err=str(e))

@router.post("/entry/{entry_id}/splits")
async def entry_splits_save(entry_id: int, request: Request, con=Depends(get_con)):
    """Re-allocate a posted entry across one-or-more categories (turn a simple entry into a split,
    or edit an existing split) from the register. Anchored to register_account_id; same field shape
    as /entry/new (direction + scat[]/samt[])."""
    form = await request.form()
    back = str(form.get("back", "/"))
    try:
        anchor = int(form["register_account_id"])
        direction = form.get("direction", "out")
        pairs = []
        for a, m in zip(form.getlist("scat"), form.getlist("samt")):
            a, m = (a or "").strip(), (m or "").strip()
            if not a and not m:
                continue
            if not (a and m):
                raise ValueError("Each split needs both a category and an amount.")
            pairs.append((int(a), abs(ledger.parse_amount_to_cents(m))))
        ledger.rewrite_entry_splits(con, entry_id, anchor, pairs, direction)
        con.commit()
        return safe_redirect(back)
    except ValueError as e:
        return safe_redirect(back, err=str(e))

@router.post("/entry/delete/{entry_id}")
def entry_delete(entry_id: int, back: str = Form("/"), con=Depends(get_con)):
    try:
        ledger.delete_entry(con, entry_id)
        con.commit()
        return safe_redirect(back)
    except ValueError as e:
        return safe_redirect(back, err=str(e))

@router.post("/register/{account_id}/bulk-delete")
def register_bulk_delete(account_id: int, entry_ids: list[int] = Form(default=[]), back: str = Form("/"),
                         con=Depends(get_con)):
    """Delete several posted entries at once (e.g. a batch that posted with the wrong sign). Each
    delete goes through ledger.delete_entry, so staged/document/invoice links revert exactly as a
    single delete would; a locked period skips that entry rather than aborting the whole selection."""
    deleted = locked = 0
    for eid in entry_ids:
        try:
            ledger.delete_entry(con, eid)
            deleted += 1
        except ledger.LockedPeriodError:
            locked += 1
    con.commit()
    note = f"Deleted {deleted} entry(ies)." if deleted else "Nothing deleted."
    if locked:
        note += f" {locked} skipped (in a closed period)."
    return safe_redirect(back, msg=note)

@router.post("/register/{account_id}/bulk-category")
def register_bulk_category(account_id: int, entry_ids: list[int] = Form(default=[]),
                           category_id: str = Form(""), back: str = Form("/"), con=Depends(get_con)):
    """Set the category on several posted 2-split entries at once, without touching date/payee/memo/
    job/customer. Reuses ledger.update_entry_fields with each entry's own current values so only the
    category changes; split (>2-leg) entries and locked-period entries are skipped, not aborted."""
    if not category_id.strip():
        return safe_redirect(back, err="Pick a category first.")
    category_id = int(category_id)
    updated = locked = 0
    for eid in entry_ids:
        row = con.execute("SELECT date, payee, memo, job_id, customer_id FROM entries WHERE id=?",
                          (eid,)).fetchone()
        if not row:
            continue
        try:
            ledger.update_entry_fields(con, eid, row["payee"], row["memo"], category_id,
                                       row["job_id"], row["date"], account_id, None,
                                       customer_id=row["customer_id"])
            updated += 1
        except ledger.LockedPeriodError:
            locked += 1
    con.commit()
    note = f"Updated category on {updated} entry(ies)." if updated else "Nothing updated."
    if locked:
        note += f" {locked} skipped (in a closed period)."
    return safe_redirect(back, msg=note)

@router.get("/duplicates", response_class=HTMLResponse)
def duplicates_page(request: Request, msg: str = "", err: str = "", con=Depends(get_con)):
    groups = duplicates.find_duplicate_groups(con)
    return templates.TemplateResponse(request, "duplicates.html", ctx(
        request, con, groups=groups, window=duplicates.WINDOW_DAYS, msg=msg, err=err))

@router.post("/duplicates/delete")
def duplicates_delete(entry_ids: list[int] = Form(default=[]), con=Depends(get_con)):
    """Delete the entries the owner checked as duplicates. Reuses ledger.delete_entry (so staged rows
    revert to pending, receipts/invoices unlink) per id; a locked-period entry is skipped, not aborted."""
    deleted = locked = 0
    for eid in entry_ids:
        try:
            ledger.delete_entry(con, eid)
            deleted += 1
        except ledger.LockedPeriodError:
            locked += 1
    con.commit()
    note = f"Deleted {deleted} duplicate entry(ies)." if deleted else "Nothing deleted."
    if locked:
        note += f" {locked} skipped (in a closed period)."
    return safe_redirect("/duplicates", msg=note)

@router.get("/entry/new", response_class=HTMLResponse)
def entry_new(request: Request, con=Depends(get_con)):
    return templates.TemplateResponse(request, "entry.html", ctx(
        request, con, cats=categories(con), sources=_entry_sources(con),
        jobs=_active_jobs(con), error=None))

@router.post("/entry/new")
async def entry_create(request: Request, con=Depends(get_con)):
    """Manual entry with one money account (source) and one-or-more category splits.
    direction 'out' = money leaves the source (categories are debited, e.g. an expense or a card
    payment); 'in' = money arrives (categories credited, e.g. income). Category legs carry the
    magnitude with the direction's sign; the source leg balances the total. Splitting = more than
    one category row, each summing into the source."""
    form = await request.form()

    def rerender(error):
        return templates.TemplateResponse(request, "entry.html", ctx(
            request, con, cats=categories(con), sources=_entry_sources(con),
            jobs=_active_jobs(con), error=error))
    try:
        date = str(form.get("date", "")).strip()
        payee = str(form.get("payee", "")).strip()
        memo = str(form.get("memo", "")).strip()
        job_id = str(form.get("job_id", "")).strip()
        direction = form.get("direction", "out")
        source = form.get("source_account", "")
        if not date or not payee:
            return rerender("Date and payee are required.")
        if not source:
            return rerender("Choose the account the money moves through.")
        source_id = int(source)

        legs, total = [], 0
        for c, a in zip(form.getlist("scat"), form.getlist("samt")):
            c, a = (c or "").strip(), (a or "").strip()
            if not c and not a:
                continue
            if not (c and a):
                return rerender("Each split needs both a category and an amount.")
            mag = abs(ledger.parse_amount_to_cents(a))
            if mag == 0:
                continue
            signed = -mag if direction == "in" else mag
            legs.append((int(c), signed))
            total += signed
        if not legs:
            return rerender("Add at least one category and amount.")
        if any(cid == source_id for cid, _ in legs):
            return rerender("A category can't be the same account the money moves through.")

        ledger.post_entry(con, ledger.normalize_date(date), payee,
                          legs + [(source_id, -total)], memo,
                          job_id=int(job_id) if job_id else None)
        con.commit()
        return RedirectResponse("/", status_code=303)
    except ValueError as e:
        return rerender(str(e))

@router.post("/entry/{entry_id}/job")
def entry_set_job(entry_id: int, job_id: str = Form(""), back: str = Form("/"), con=Depends(get_con)):
    ledger.set_entry_job(con, entry_id, int(job_id) if job_id.strip() else None)
    con.commit()
    return safe_redirect(back)

@router.post("/entry/{entry_id}/customer")
def entry_set_customer(entry_id: int, customer_id: str = Form(""), back: str = Form("/"), con=Depends(get_con)):
    ledger.set_entry_customer(con, entry_id, int(customer_id) if customer_id.strip() else None)
    con.commit()
    return safe_redirect(back)
