"""Recurring-transaction routes."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import ledger
import recurring
from webutil import categories, ctx, get_con, safe_redirect, templates

router = APIRouter()

@router.get("/recurring", response_class=HTMLResponse)
def recurring_page(request: Request, msg: str = "", err: str = "", con=Depends(get_con)):
    banks = con.execute("SELECT id, name FROM accounts WHERE kind IN ('bank','card') AND active=1 "
                        "ORDER BY type, name").fetchall()
    return templates.TemplateResponse(request, "recurring.html", ctx(
        request, con, items=recurring.list_all(con), banks=banks,
        cats=categories(con, ("expense", "income")),
        suggestions=recurring.detect_candidates(con), msg=msg, err=err))

@router.post("/recurring")
def recurring_create(name: str = Form(...), amount: str = Form(...), flow: str = Form("expense"),
                     account_id: int = Form(...), category_id: int = Form(...),
                     frequency: str = Form("monthly"), next_date: str = Form(...), memo: str = Form(""),
                     con=Depends(get_con)):
    try:
        cents = abs(ledger.parse_amount_to_cents(amount))
        nd = ledger.normalize_date(next_date)
    except ValueError as e:
        return safe_redirect("/recurring", err=f"Couldn't read that: {e}")
    flow = "income" if flow == "income" else "expense"
    freq = frequency if frequency in ("weekly", "monthly", "yearly") else "monthly"
    con.execute(
        "INSERT INTO recurring(name,amount_cents,flow,account_id,category_id,frequency,next_date,memo) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (name.strip(), cents, flow, account_id, category_id, freq, nd, memo.strip()))
    con.commit()
    return safe_redirect("/recurring", msg=f"Added '{name.strip()}'.")

@router.post("/recurring/post-all")
def recurring_post_all(con=Depends(get_con)):
    posted = locked = 0
    for r in recurring.due(con):
        try:
            recurring.post_occurrence(con, r["id"])
            posted += 1
        except ledger.LockedPeriodError:
            locked += 1
        except ValueError:
            pass
    con.commit()
    parts = [f"Posted {posted} due item(s)"]
    if locked:
        parts.append(f"{locked} skipped (in a closed period)")
    return safe_redirect("/recurring", msg="; ".join(parts) + ".")

@router.post("/recurring/{rid}/post")
def recurring_post(rid: int, con=Depends(get_con)):
    try:
        recurring.post_occurrence(con, rid)
        con.commit()
        return safe_redirect("/recurring", msg="Posted to the ledger and advanced to the next date.")
    except ValueError as e:
        return safe_redirect("/recurring", err=str(e))

@router.post("/recurring/{rid}/skip")
def recurring_skip(rid: int, con=Depends(get_con)):
    recurring.skip_occurrence(con, rid)
    con.commit()
    return safe_redirect("/recurring", msg="Skipped — advanced to the next date without posting.")

@router.post("/recurring/{rid}/toggle")
def recurring_toggle(rid: int, con=Depends(get_con)):
    con.execute("UPDATE recurring SET active = 1 - active WHERE id=?", (rid,))
    con.commit()
    return RedirectResponse("/recurring", status_code=303)

@router.post("/recurring/{rid}/delete")
def recurring_delete(rid: int, con=Depends(get_con)):
    con.execute("DELETE FROM recurring WHERE id=?", (rid,))
    con.commit()
    return RedirectResponse("/recurring", status_code=303)
