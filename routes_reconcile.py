"""Statement reconciliation routes."""
from datetime import date as date_cls, datetime
from pathlib import Path
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

import ai
import db
import importer
import ledger
import reconcile
from webutil import categories, ctx, templates

router = APIRouter()

@router.get("/reconcile", response_class=HTMLResponse)
def reconcile_page(request: Request, msg: str = ""):
    con = db.connect()
    try:
        return templates.TemplateResponse(request, "reconcile.html", ctx(
            request, con, accounts=reconcile.status(con), msg=msg))
    finally:
        con.close()

@router.get("/reconcile/{account_id}", response_class=HTMLResponse)
def reconcile_account(request: Request, account_id: int, date: str = "", balance: str = "", msg: str = ""):
    con = db.connect()
    try:
        acct = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not acct or acct["kind"] not in ("bank", "card"):
            return RedirectResponse("/reconcile", status_code=303)
        last = reconcile.last_reconciliation(con, account_id)
        result = txns = dups = unreconciled = None
        cleared_begin = reconcile.cleared_balance(con, account_id)
        if date.strip() and balance.strip():  # preview a reconciliation (no save)
            try:
                sd = ledger.normalize_date(date)
                bal = ledger.parse_amount_to_cents(balance)
                result = reconcile.compute(con, account_id, sd, bal)
                after = last["statement_date"] if last else None
                txns = reconcile.period_transactions(con, account_id, after, sd)
                dups = reconcile.likely_duplicates(con, account_id, after, sd)
                unreconciled = reconcile.unreconciled_transactions(con, account_id, sd)
            except ValueError:
                result = None
        all_accounts = con.execute(
            "SELECT id, name, kind FROM accounts WHERE kind IN ('bank','card') AND active=1 ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "reconcile_account.html", ctx(
            request, con, acct=acct, last=last, history=reconcile.history(con, account_id),
            result=result, txns=txns, dups=dups, unreconciled=unreconciled, cleared_begin=cleared_begin,
            date=date, balance=balance, cats=categories(con), msg=msg, all_accounts=all_accounts))
    finally:
        con.close()

@router.post("/reconcile")
def reconcile_save(account_id: int = Form(...), statement_date: str = Form(...),
                   statement_balance: str = Form(...)):
    from urllib.parse import quote
    con = db.connect()
    try:
        sd = ledger.normalize_date(statement_date)
        bal = ledger.parse_amount_to_cents(statement_balance)
        r = reconcile.record(con, account_id, sd, bal)
        con.commit()
        note = ("Reconciled — books match the statement." if r["reconciled"]
                else f"Saved — off by ${ledger.fmt_cents(abs(r['difference']))}. See the transactions below to find it.")
        return RedirectResponse(f"/reconcile/{account_id}?msg=" + quote(note), status_code=303)
    except ValueError as e:
        return RedirectResponse(f"/reconcile/{account_id}?msg=" + quote(f"Couldn't read that: {e}"), status_code=303)
    finally:
        con.close()

@router.post("/reconcile/finish")
def reconcile_finish(account_id: int = Form(...), statement_date: str = Form(...),
                     statement_balance: str = Form(...), cleared: list[str] = Form(default=[])):
    """Phase 2: mark the ticked transactions cleared against the statement and record the checkpoint."""
    from urllib.parse import quote
    con = db.connect()
    try:
        sd = ledger.normalize_date(statement_date)
        bal = ledger.parse_amount_to_cents(statement_balance)
        r = reconcile.finish(con, account_id, sd, bal, cleared)
        con.commit()
        if r["reconciled"]:
            note = f"Reconciled — {r['cleared_count']} transaction(s) cleared and locked to the statement."
        else:
            note = (f"Saved with {r['cleared_count']} cleared, still off by "
                    f"${ledger.fmt_cents(abs(r['difference']))}. Tick the rest or square up the difference.")
        return RedirectResponse(f"/reconcile/{account_id}?msg=" + quote(note), status_code=303)
    except ValueError as e:
        return RedirectResponse(f"/reconcile/{account_id}?msg=" + quote(f"Couldn't finish: {e}"), status_code=303)
    finally:
        con.close()

@router.post("/reconcile/upload")
async def reconcile_upload(request: Request, file: UploadFile = File(...)):
    con = db.connect()
    try:
        raw = await file.read()
        name = (file.filename or "statement").lower()
        if not (name.endswith(".csv") or name.endswith(".pdf")):
            raise ValueError("Upload a .pdf or .csv file.")

        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        tmp = db.DOCS / f"temp_rec_{timestamp}_{Path(name).name}"
        db.DOCS.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(raw)

        # 1. Text extraction & Account Auto-detection:
        if name.endswith(".pdf"):
            text = importer.pdf_text(tmp)
        else:
            text = raw.decode("utf-8-sig", errors="replace")

        detected_account_id = importer.detect_account_id(con, file.filename or "", text)
        acct = con.execute("SELECT * FROM accounts WHERE id=?", (detected_account_id,)).fetchone()
        if not acct:
            raise ValueError("No active bank or card account detected for this statement.")

        # 2. Extract date and ending balance:
        statement_date = ""
        statement_balance = ""

        if name.endswith(".pdf") and ai.available(con):
            metadata = ai.extract_reconcile_metadata_pdf(con, str(tmp), acct["name"])
            if metadata:
                statement_date = metadata.get("statement_end_date", "")
                bal_val = metadata.get("ending_balance", 0.0)
                if bal_val:
                    statement_balance = f"{bal_val:.2f}"

        # Fallbacks:
        if not statement_date:
            txns = []
            if name.endswith(".csv"):
                txns = importer.parse_csv(raw)
            elif name.endswith(".pdf"):
                txns = importer.regex_parse_statement(text)
            
            dates = [t["date"] for t in txns if t.get("date")]
            if dates:
                statement_date = max(dates)
            else:
                statement_date = date_cls.today().isoformat()

        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass

        from urllib.parse import quote
        url = f"/reconcile/{detected_account_id}?date={quote(statement_date)}&balance={quote(statement_balance)}"
        return RedirectResponse(url, status_code=303)

    except ValueError as e:
        return templates.TemplateResponse(request, "reconcile.html", ctx(
            request, con, accounts=reconcile.status(con), msg=str(e)))
    finally:
        con.close()

@router.post("/reconcile/adjust")
def reconcile_adjust(
    account_id: int = Form(...),
    statement_date: str = Form(...),
    statement_balance: str = Form(...),
    difference: int = Form(...),
    offset_account_id: int = Form(...),
    payee: str = Form(...),
    memo: str = Form("")
):
    from urllib.parse import quote
    con = db.connect()
    try:
        sd = ledger.normalize_date(statement_date)
        bal = ledger.parse_amount_to_cents(statement_balance)
        
        acct = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not acct:
            raise ValueError("Target account not found.")
            
        acct_split_amount = -difference if acct["type"] in ("liability", "equity", "income") else difference
        
        ledger.post_entry(
            con,
            sd,
            payee,
            [(account_id, acct_split_amount), (offset_account_id, -acct_split_amount)],
            memo=memo
        )
        
        reconcile.record(con, account_id, sd, bal)
        con.commit()
        
        note = f"Adjustment posted and reconciled successfully! Difference of ${ledger.fmt_cents(abs(difference))} written off."
        return RedirectResponse(f"/reconcile/{account_id}?msg=" + quote(note), status_code=303)
        
    except ValueError as e:
        return RedirectResponse(f"/reconcile/{account_id}?msg=" + quote(f"Adjustment failed: {e}"), status_code=303)
    finally:
        con.close()
