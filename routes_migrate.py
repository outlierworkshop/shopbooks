"""QuickBooks Online migration routes."""
from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

import importer
import ledger
import migrate
from webutil import ctx, get_con, templates

router = APIRouter()

@router.get("/migrate", response_class=HTMLResponse)
def migrate_page(request: Request, msg: str = "", err: str = "", con=Depends(get_con)):
    counts = {
        "accounts": con.execute("SELECT COUNT(*) c FROM accounts WHERE active=1").fetchone()["c"],
        "staged": con.execute("SELECT COUNT(*) c FROM staged WHERE status='pending'").fetchone()["c"],
        "posted": con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"],
        "customers": con.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"],
        "mileage": con.execute("SELECT COUNT(*) c FROM mileage").fetchone()["c"],
    }
    real_accounts = []
    for a in con.execute("SELECT * FROM accounts WHERE kind IN ('bank','card') AND active=1 ORDER BY kind, name"):
        bal = ledger.display_balance(a["type"], ledger.raw_balance(con, a["id"]))
        real_accounts.append({"id": a["id"], "name": a["name"], "kind": a["kind"], "balance": bal})
    return templates.TemplateResponse(request, "migrate.html", ctx(
        request, con, counts=counts, real_accounts=real_accounts, msg=msg, err=err))

def _migrate_redirect(msg="", err=""):
    from urllib.parse import quote
    return RedirectResponse(f"/migrate?msg={quote(msg)}&err={quote(err)}", status_code=303)

@router.post("/migrate/accounts")
async def migrate_accounts(file: UploadFile = File(...), con=Depends(get_con)):
    try:
        created, matched = migrate.import_accounts(con, migrate.parse_accounts(await file.read()))
        con.commit()
        return _migrate_redirect(msg=f"Accounts: {created} created, {matched} already existed.")
    except ValueError as e:
        return _migrate_redirect(err=str(e))

@router.post("/migrate/transactions")
async def migrate_transactions(file: UploadFile = File(...), con=Depends(get_con)):
    try:
        by_source, skipped = migrate.parse_transactions(con, await file.read())
        staged = migrate.import_transactions(con, by_source, file.filename or "transactions.csv")
        pairs = importer.rescan_transfers(con)
        con.commit()
        return _migrate_redirect(msg=(
            f"{staged} transactions staged for Review across {len(by_source)} account(s). "
            f"({skipped['not_bank_card']} rows on category accounts skipped - those are the "
            f"same transactions seen from the other side.) {pairs} transfer pair(s) auto-matched."))
    except ValueError as e:
        return _migrate_redirect(err=str(e))

@router.post("/migrate/customers")
async def migrate_customers(file: UploadFile = File(...), con=Depends(get_con)):
    try:
        created = migrate.import_customers(con, migrate.parse_customers(await file.read()))
        con.commit()
        return _migrate_redirect(msg=f"{created} customers imported.")
    except ValueError as e:
        return _migrate_redirect(err=str(e))

@router.post("/migrate/mileage")
async def migrate_mileage(file: UploadFile = File(...), con=Depends(get_con)):
    try:
        created = migrate.import_mileage(con, migrate.parse_mileage(await file.read()))
        con.commit()
        return _migrate_redirect(msg=f"{created} trips imported.")
    except ValueError as e:
        return _migrate_redirect(err=str(e))

@router.post("/migrate/opening")
async def migrate_opening(request: Request, con=Depends(get_con)):
    form = await request.form()
    try:
        as_of = ledger.normalize_date(form.get("as_of", ""))
        equity = con.execute("SELECT id FROM accounts WHERE lower(name)=lower('Owner''s Equity')").fetchone()
        if not equity:
            cur = con.execute("INSERT INTO accounts(name,type,kind) VALUES('Owner''s Equity','equity','category')")
            equity_id = cur.lastrowid
        else:
            equity_id = equity["id"]
        posted = []
        for key, val in form.items():
            if not key.startswith("bal_") or not str(val).strip():
                continue
            acct_id = int(key[4:])
            acct = con.execute("SELECT * FROM accounts WHERE id=?", (acct_id,)).fetchone()
            if not acct:
                continue
            cents = ledger.parse_amount_to_cents(str(val))
            if cents == 0:
                continue
            # user enters natural balances: bank = money in the account, card = amount owed
            raw = cents if acct["type"] == "asset" else -cents
            ledger.post_entry(con, as_of, f"Opening balance - {acct['name']}",
                              [(acct_id, raw), (equity_id, -raw)], memo="QBO migration opening balance")
            posted.append(acct["name"])
        con.commit()
        if posted:
            return _migrate_redirect(msg=f"Opening balances posted for: {', '.join(posted)} (as of {as_of}).")
        return _migrate_redirect(err="No balances entered.")
    except ValueError as e:
        return _migrate_redirect(err=str(e))
