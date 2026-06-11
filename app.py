"""ShopBooks - local double-entry accounting for a one-person business."""
import io
from datetime import date as date_cls, datetime
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import zipfile

import ai
import backup
import db
import importer
import invoicing
import ledger
import migrate

BASE = Path(__file__).resolve().parent
app = FastAPI(title="ShopBooks")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["money"] = ledger.fmt_cents

db.init()
backup.snapshot()  # protect the books on every launch (local + cloud mirror)


def ctx(request, con, **kw):
    pending = con.execute("SELECT COUNT(*) c FROM staged WHERE status='pending'").fetchone()["c"]
    unmatched = con.execute("SELECT COUNT(*) c FROM documents WHERE status='unmatched'").fetchone()["c"]
    return {"request": request, "pending_count": pending, "unmatched_count": unmatched,
            "ai_on": ai.available(con), "today": date_cls.today().isoformat(),
            "business_name": db.get_setting(con, "business_name", "My Business"), **kw}


def categories(con, types=("expense", "income", "asset", "liability", "equity")):
    qmarks = ",".join("?" * len(types))
    return con.execute(f"SELECT * FROM accounts WHERE active=1 AND type IN ({qmarks}) ORDER BY type, name",
                       types).fetchall()


@app.get("/favicon.ico")
def favicon():
    return FileResponse(BASE / "static" / "favicon.ico")


# ---------- dashboard ----------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    con = db.connect()
    try:
        accounts = ledger.accounts_with_balances(con, kinds=("bank", "card"))
        year = date_cls.today().year
        p = ledger.pnl(con, f"{year}-01-01", f"{year}-12-31")
        recent = con.execute(
            "SELECT e.*, (SELECT GROUP_CONCAT(a.name, ' / ') FROM splits s JOIN accounts a ON a.id=s.account_id "
            " WHERE s.entry_id=e.id) accts, "
            "(SELECT MAX(abs(amount_cents)) FROM splits WHERE entry_id=e.id) amt "
            "FROM entries e ORDER BY e.date DESC, e.id DESC LIMIT 12").fetchall()
        return templates.TemplateResponse(request, "dashboard.html", ctx(
            request, con, accounts=accounts, pnl=p, recent=recent, year=year))
    finally:
        con.close()


# ---------- import & review ----------

@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    con = db.connect()
    try:
        sources = con.execute("SELECT * FROM accounts WHERE kind IN ('bank','card') AND active=1 ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "import.html", ctx(request, con, sources=sources, error=None))
    finally:
        con.close()


@app.post("/import")
async def do_import(request: Request, file: UploadFile = File(...), account_id: int = Form(...)):
    con = db.connect()
    try:
        raw = await file.read()
        name = (file.filename or "statement").lower()
        acct = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        txns, note = [], ""
        if name.endswith(".csv"):
            txns = importer.parse_csv(raw)
        elif name.endswith(".pdf"):
            tmp = db.DOCS / f"stmt_{datetime.now().strftime('%Y%m%d%H%M%S')}_{Path(name).name}"
            tmp.write_bytes(raw)
            text = importer.pdf_text(tmp)
            extracted = None
            if ai.available(con):
                extracted = (ai.extract_statement(con, text, acct["name"]) if text.strip()
                             else ai.extract_statement_pdf(con, str(tmp), acct["name"]))
            if extracted is not None:
                for t in extracted:
                    try:
                        txns.append({"date": ledger.normalize_date(t["date"]),
                                     "description": str(t["description"]).strip(),
                                     "amount_cents": round(float(t["amount"]) * 100)})
                    except (ValueError, KeyError):
                        continue
            else:
                txns = importer.regex_parse_statement(text)
                note = "Parsed without AI (no API key set) - double-check amounts and signs."
        else:
            raise ValueError("Upload a .pdf or .csv file.")
        if not txns:
            raise ValueError("No transactions found in that file.")

        cur = con.execute("INSERT INTO batches(filename,account_id) VALUES(?,?)", (file.filename, account_id))
        batch_id = cur.lastrowid
        cats = {a["id"]: a["name"] for a in categories(con, ("expense", "income"))}
        ai_cats = None
        uncategorized = [t for t in txns if importer.apply_rules(con, t["description"]) is None]
        if uncategorized and ai.available(con):
            ai_all = ai.categorize(con, [{"description": t["description"], "amount": t["amount_cents"]} for t in txns],
                                   list(cats.values()))
            ai_cats = ai_all
        importer.stage_transactions(con, batch_id, txns, account_id, cats, ai_cats)
        con.commit()
        from urllib.parse import quote
        dates = sorted(t["date"] for t in txns if t.get("date"))
        if dates:
            note = (note + f" Imported {len(txns)} transactions dated {dates[0]} to {dates[-1]} "
                    "- check the year looks right before posting.").strip()
        return RedirectResponse("/review?note=" + quote(note), status_code=303)
    except ValueError as e:
        sources = con.execute("SELECT * FROM accounts WHERE kind IN ('bank','card') AND active=1 ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "import.html", ctx(request, con, sources=sources, error=str(e)))
    finally:
        con.close()


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, note: str = ""):
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT st.*, b.filename, b.account_id source_id, a.name source_name "
            "FROM staged st JOIN batches b ON b.id=st.batch_id JOIN accounts a ON a.id=b.account_id "
            "WHERE st.status='pending' ORDER BY b.id DESC, st.date, st.id").fetchall()
        items = []
        for r in rows:
            items.append({**dict(r), "dup": importer.possible_duplicate(con, r["source_id"], r["date"], r["amount_cents"])})
        cats = categories(con)
        return templates.TemplateResponse(request, "review.html", ctx(request, con, items=items, cats=cats, note=note))
    finally:
        con.close()


def _post_staged(con, staged_id, category_id, remember=False):
    st = con.execute(
        "SELECT st.*, b.account_id source_id FROM staged st JOIN batches b ON b.id=st.batch_id WHERE st.id=?",
        (staged_id,)).fetchone()
    if not st or st["status"] != "pending" or not category_id:
        return
    entry_id = ledger.post_entry(con, st["date"], st["description"],
                                 [(category_id, st["amount_cents"]), (st["source_id"], -st["amount_cents"])])
    con.execute("UPDATE staged SET status='posted', entry_id=?, category_id=? WHERE id=?",
                (entry_id, category_id, staged_id))
    if remember:
        token = st["description"].upper().split("  ")[0].strip()[:40]
        if token and not con.execute("SELECT 1 FROM rules WHERE pattern=?", (token,)).fetchone():
            con.execute("INSERT INTO rules(pattern,account_id) VALUES(?,?)", (token, category_id))


@app.post("/review")
async def review_action(request: Request):
    form = await request.form()
    con = db.connect()
    try:
        def cat_for(sid):
            v = form.get(f"cat_{sid}", "")
            return int(v) if v else None

        if "post_one" in form:
            sid = int(form["post_one"])
            _post_staged(con, sid, cat_for(sid), remember=f"remember_{sid}" in form)
        elif "skip_one" in form:
            con.execute("UPDATE staged SET status='skipped' WHERE id=?", (int(form["skip_one"]),))
        elif "post_all" in form:
            ids = [int(k.split("_", 1)[1]) for k in form.keys() if k.startswith("cat_")]
            for sid in sorted(ids):
                cid = cat_for(sid)
                if cid:
                    _post_staged(con, sid, cid)
        elif "flip_batch" in form:
            con.execute("UPDATE staged SET amount_cents=-amount_cents WHERE batch_id=? AND status='pending'",
                        (int(form["flip_batch"]),))
        elif "discard_batch" in form:
            # drop the not-yet-posted rows of one import (e.g. to redo an import); posted rows untouched
            con.execute("DELETE FROM staged WHERE batch_id=? AND status='pending'", (int(form["discard_batch"]),))
        elif "ai_review" in form:
            return _ai_review_pending(con)
        con.commit()
        return RedirectResponse("/review", status_code=303)
    finally:
        con.close()


def _ai_review_pending(con):
    """Run rules + AI categorization over all pending staged rows. Suggestions only; nothing posts."""
    from urllib.parse import quote

    def back(note):
        return RedirectResponse("/review?note=" + quote(note), status_code=303)

    if not ai.available(con):
        return back("AI is off - add a Claude API key in Settings to use AI review.")
    pending = con.execute("SELECT * FROM staged WHERE status='pending'").fetchall()
    if not pending:
        return back("Nothing pending to review.")

    cats = {a["id"]: a["name"] for a in categories(con, ("expense", "income"))}
    name_to_id = {v: k for k, v in cats.items()}
    ruled, ai_targets = 0, []
    for s in pending:
        rid = importer.apply_rules(con, s["description"])
        if rid:
            con.execute("UPDATE staged SET category_id=? WHERE id=?", (rid, s["id"]))
            ruled += 1
        else:
            ai_targets.append(s)

    filled = 0
    if ai_targets:
        suggestions = ai.categorize(
            con, [{"description": s["description"], "amount": s["amount_cents"]} for s in ai_targets],
            list(cats.values()))
        if suggestions is None:
            con.commit()
            return back("AI couldn't categorize this batch - try again, or set categories manually.")
        for s, name in zip(ai_targets, suggestions):
            cid = name_to_id.get(name)
            if cid:
                con.execute("UPDATE staged SET category_id=? WHERE id=?", (cid, s["id"]))
                filled += 1
    con.commit()
    parts = [f"{filled} categorized by AI"]
    if ruled:
        parts.append(f"{ruled} matched a rule")
    return back("AI review done: " + ", ".join(parts) + ". Check the suggestions and post.")


# ---------- registers & entries ----------

@app.get("/register/{account_id}", response_class=HTMLResponse)
def register_view(request: Request, account_id: int):
    con = db.connect()
    try:
        acct, rows = ledger.register(con, account_id)
        bal = ledger.display_balance(acct["type"], ledger.raw_balance(con, account_id))
        return templates.TemplateResponse(request, "register.html", ctx(request, con, acct=acct, rows=rows, balance=bal))
    finally:
        con.close()


@app.post("/entry/delete/{entry_id}")
def entry_delete(entry_id: int, back: str = Form("/")):
    con = db.connect()
    try:
        ledger.delete_entry(con, entry_id)
        con.commit()
        return RedirectResponse(back, status_code=303)
    finally:
        con.close()


@app.get("/entry/new", response_class=HTMLResponse)
def entry_new(request: Request):
    con = db.connect()
    try:
        return templates.TemplateResponse(request, "entry.html", ctx(request, con, cats=categories(con), error=None))
    finally:
        con.close()


@app.post("/entry/new")
def entry_create(request: Request, date: str = Form(...), payee: str = Form(...),
                 amount: str = Form(...), to_account: int = Form(...), from_account: int = Form(...),
                 memo: str = Form("")):
    con = db.connect()
    try:
        cents = ledger.parse_amount_to_cents(amount)
        ledger.post_entry(con, ledger.normalize_date(date), payee,
                          [(to_account, cents), (from_account, -cents)], memo)
        con.commit()
        return RedirectResponse("/", status_code=303)
    except ValueError as e:
        return templates.TemplateResponse(request, "entry.html", ctx(request, con, cats=categories(con), error=str(e)))
    finally:
        con.close()


# ---------- receipts ----------

def receipt_candidates(con, doc):
    if not doc["amount_cents"]:
        return []
    q = ("SELECT DISTINCT e.id, e.date, e.payee, a.name acct FROM entries e "
         "JOIN splits s ON s.entry_id=e.id JOIN accounts a ON a.id=s.account_id "
         "WHERE s.amount_cents=? AND a.type IN ('expense','income') "
         "AND NOT EXISTS (SELECT 1 FROM documents d WHERE d.entry_id=e.id)")
    args = [doc["amount_cents"]]
    if doc["doc_date"]:
        q += " AND abs(julianday(e.date)-julianday(?))<=7"
        args.append(doc["doc_date"])
    q += " ORDER BY e.date DESC LIMIT 8"
    return con.execute(q, args).fetchall()


RECEIPT_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf"}


def _ingest_receipt(con, data: bytes, original_name: str):
    """Save one receipt, read it with AI, and auto-match. Returns
    'matched' | 'imported' | 'duplicate' | 'error'. Dedupes on file content (sha256)."""
    import hashlib
    sha = hashlib.sha256(data).hexdigest()
    if con.execute("SELECT 1 FROM documents WHERE sha256=?", (sha,)).fetchone():
        return "duplicate"
    safe = Path(original_name or "receipt.jpg").name
    db.DOCS.mkdir(parents=True, exist_ok=True)
    dest = db.DOCS / f"rcpt_{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{safe}"
    try:
        dest.write_bytes(data)
    except OSError:
        return "error"
    vendor, ddate, cents = "", "", None
    try:
        info = ai.extract_receipt(con, str(dest))
    except Exception:
        info = None
    if info:
        vendor = info.get("vendor", "")
        try:
            ddate = ledger.normalize_date(info.get("date", "")) if info.get("date") else ""
        except ValueError:
            ddate = ""
        total = info.get("total") or 0
        cents = round(float(total) * 100) if total else None
    cur = con.execute(
        "INSERT INTO documents(filename,path,vendor,doc_date,amount_cents,sha256) VALUES(?,?,?,?,?,?)",
        (safe, str(dest), vendor, ddate, cents, sha))
    doc = con.execute("SELECT * FROM documents WHERE id=?", (cur.lastrowid,)).fetchone()
    cands = receipt_candidates(con, doc)
    if len(cands) == 1:  # unambiguous - auto-match
        con.execute("UPDATE documents SET status='matched', entry_id=? WHERE id=?", (cands[0]["id"], doc["id"]))
        return "matched"
    return "imported"


@app.get("/receipts", response_class=HTMLResponse)
def receipts(request: Request, msg: str = "", err: str = ""):
    con = db.connect()
    try:
        docs = con.execute("SELECT * FROM documents WHERE kind='receipt' ORDER BY status DESC, uploaded_at DESC").fetchall()
        items = []
        for d in docs:
            cands = receipt_candidates(con, d) if d["status"] == "unmatched" else []
            entry = None
            if d["entry_id"]:
                entry = con.execute("SELECT * FROM entries WHERE id=?", (d["entry_id"],)).fetchone()
            items.append({"doc": d, "candidates": cands, "entry": entry})
        return templates.TemplateResponse(request, "receipts.html", ctx(request, con, items=items, msg=msg, err=err))
    finally:
        con.close()


@app.post("/receipts/upload")
async def receipts_upload(files: list[UploadFile] = File(...)):
    con = db.connect()
    try:
        for f in files:
            _ingest_receipt(con, await f.read(), f.filename or "receipt.jpg")
        con.commit()
        return RedirectResponse("/receipts", status_code=303)
    finally:
        con.close()


@app.post("/receipts/import-folder")
def receipts_import_folder(folder: str = Form(...), recursive: str = Form("")):
    from urllib.parse import quote
    con = db.connect()
    try:
        p = Path(folder.strip().strip('"'))
        if not p.is_dir():
            return RedirectResponse("/receipts?err=" + quote(f"Folder not found: {folder}"), status_code=303)
        files = sorted(p.rglob("*") if recursive else p.glob("*"))
        counts = {"matched": 0, "imported": 0, "duplicate": 0, "error": 0}
        scanned = 0
        for fp in files:
            if not fp.is_file() or fp.suffix.lower() not in RECEIPT_EXTS:
                continue
            scanned += 1
            try:
                res = _ingest_receipt(con, fp.read_bytes(), fp.name)
            except Exception:
                res = "error"
            counts[res] += 1
        con.commit()
        if scanned == 0:
            return RedirectResponse(
                "/receipts?err=" + quote(f"No receipt images found in {folder} (looked for jpg/png/gif/webp/pdf)."),
                status_code=303)
        note = (f"Folder import: {counts['matched']} matched, {counts['imported']} imported (need matching), "
                f"{counts['duplicate']} already imported"
                + (f", {counts['error']} unreadable" if counts['error'] else "") + ".")
        return RedirectResponse("/receipts?msg=" + quote(note), status_code=303)
    finally:
        con.close()


@app.post("/receipts/rematch")
def receipts_rematch():
    from urllib.parse import quote
    con = db.connect()
    try:
        matched = 0
        for d in con.execute("SELECT * FROM documents WHERE status='unmatched' AND amount_cents IS NOT NULL").fetchall():
            cands = receipt_candidates(con, d)
            if len(cands) == 1:
                con.execute("UPDATE documents SET status='matched', entry_id=? WHERE id=?", (cands[0]["id"], d["id"]))
                matched += 1
        con.commit()
        return RedirectResponse("/receipts?msg=" + quote(f"Re-checked matches: {matched} newly matched."), status_code=303)
    finally:
        con.close()


@app.post("/receipts/match")
def receipts_match(doc_id: int = Form(...), entry_id: int = Form(...)):
    con = db.connect()
    try:
        con.execute("UPDATE documents SET status='matched', entry_id=? WHERE id=?", (entry_id, doc_id))
        con.commit()
        return RedirectResponse("/receipts", status_code=303)
    finally:
        con.close()


@app.post("/receipts/update")
def receipts_update(doc_id: int = Form(...), vendor: str = Form(""), doc_date: str = Form(""), amount: str = Form("")):
    con = db.connect()
    try:
        cents = ledger.parse_amount_to_cents(amount) if amount.strip() else None
        ddate = ledger.normalize_date(doc_date) if doc_date.strip() else ""
        con.execute("UPDATE documents SET vendor=?, doc_date=?, amount_cents=? WHERE id=?",
                    (vendor, ddate, cents, doc_id))
        con.commit()
        return RedirectResponse("/receipts", status_code=303)
    finally:
        con.close()


@app.post("/receipts/unmatch")
def receipts_unmatch(doc_id: int = Form(...)):
    con = db.connect()
    try:
        con.execute("UPDATE documents SET status='unmatched', entry_id=NULL WHERE id=?", (doc_id,))
        con.commit()
        return RedirectResponse("/receipts", status_code=303)
    finally:
        con.close()


@app.post("/receipts/delete")
def receipts_delete(doc_id: int = Form(...)):
    con = db.connect()
    try:
        row = con.execute("SELECT path FROM documents WHERE id=?", (doc_id,)).fetchone()
        if row:
            Path(row["path"]).unlink(missing_ok=True)
            con.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        con.commit()
        return RedirectResponse("/receipts", status_code=303)
    finally:
        con.close()


@app.get("/doc/{doc_id}")
def doc_file(doc_id: int):
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return RedirectResponse("/receipts", status_code=303)
        return FileResponse(row["path"], filename=row["filename"])
    finally:
        con.close()


# ---------- mileage ----------

@app.get("/mileage", response_class=HTMLResponse)
def mileage(request: Request):
    con = db.connect()
    try:
        year = date_cls.today().year
        trips = con.execute("SELECT * FROM mileage ORDER BY date DESC, id DESC").fetchall()
        rate = float(db.get_setting(con, "mileage_rate", "0.70"))
        ytd = con.execute("SELECT COALESCE(SUM(miles),0) m FROM mileage WHERE date LIKE ?",
                          (f"{year}%",)).fetchone()["m"]
        return templates.TemplateResponse(request, "mileage.html", ctx(
            request, con, trips=trips, rate=rate, ytd=ytd, year=year,
            deduction_cents=round(ytd * rate * 100)))
    finally:
        con.close()


@app.post("/mileage")
def mileage_add(date: str = Form(...), miles: float = Form(...), purpose: str = Form(""),
                from_loc: str = Form(""), to_loc: str = Form("")):
    con = db.connect()
    try:
        con.execute("INSERT INTO mileage(date,miles,purpose,from_loc,to_loc) VALUES(?,?,?,?,?)",
                    (ledger.normalize_date(date), miles, purpose, from_loc, to_loc))
        con.commit()
        return RedirectResponse("/mileage", status_code=303)
    finally:
        con.close()


@app.post("/mileage/delete")
def mileage_delete(trip_id: int = Form(...)):
    con = db.connect()
    try:
        con.execute("DELETE FROM mileage WHERE id=?", (trip_id,))
        con.commit()
        return RedirectResponse("/mileage", status_code=303)
    finally:
        con.close()


# ---------- reports ----------

@app.get("/reports", response_class=HTMLResponse)
def reports(request: Request, start: str = "", end: str = ""):
    con = db.connect()
    try:
        year = date_cls.today().year
        start = start or f"{year}-01-01"
        end = end or f"{year}-12-31"
        p = ledger.pnl(con, start, end)
        bs = ledger.balance_sheet(con, end)
        rate = float(db.get_setting(con, "mileage_rate", "0.70"))
        miles = con.execute("SELECT COALESCE(SUM(miles),0) m FROM mileage WHERE date BETWEEN ? AND ?",
                            (start, end)).fetchone()["m"]
        return templates.TemplateResponse(request, "reports.html", ctx(
            request, con, pnl=p, bs=bs, start=start, end=end, miles=miles, rate=rate,
            mileage_deduction=round(miles * rate * 100)))
    finally:
        con.close()


@app.get("/reports/pnl.csv")
def pnl_csv(start: str, end: str):
    con = db.connect()
    try:
        p = ledger.pnl(con, start, end)
        buf = io.StringIO()
        w = __import__("csv").writer(buf)
        w.writerow(["Profit & Loss", f"{start} to {end}"])
        w.writerow([])
        w.writerow(["INCOME"])
        for i in p["income"]:
            w.writerow([i["name"], f"{i['amount']/100:.2f}"])
        w.writerow(["Total Income", f"{p['total_income']/100:.2f}"])
        w.writerow([])
        w.writerow(["EXPENSES"])
        for x in p["expenses"]:
            w.writerow([x["name"], f"{x['amount']/100:.2f}"])
        w.writerow(["Total Expenses", f"{p['total_expenses']/100:.2f}"])
        w.writerow([])
        w.writerow(["Net Profit", f"{p['net']/100:.2f}"])
        buf.seek(0)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f"attachment; filename=pnl_{start}_{end}.csv"})
    finally:
        con.close()


@app.get("/reports/transactions.csv")
def transactions_csv(start: str, end: str):
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT e.date, e.payee, e.memo, a.name account, s.amount_cents "
            "FROM entries e JOIN splits s ON s.entry_id=e.id JOIN accounts a ON a.id=s.account_id "
            "WHERE e.date BETWEEN ? AND ? ORDER BY e.date, e.id", (start, end)).fetchall()
        buf = io.StringIO()
        w = __import__("csv").writer(buf)
        w.writerow(["Date", "Payee", "Memo", "Account", "Amount"])
        for r in rows:
            w.writerow([r["date"], r["payee"], r["memo"], r["account"], f"{r['amount_cents']/100:.2f}"])
        buf.seek(0)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f"attachment; filename=transactions_{start}_{end}.csv"})
    finally:
        con.close()


# ---------- QuickBooks migration ----------

@app.get("/migrate", response_class=HTMLResponse)
def migrate_page(request: Request, msg: str = "", err: str = ""):
    con = db.connect()
    try:
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
    finally:
        con.close()


def _migrate_redirect(msg="", err=""):
    from urllib.parse import quote
    return RedirectResponse(f"/migrate?msg={quote(msg)}&err={quote(err)}", status_code=303)


@app.post("/migrate/accounts")
async def migrate_accounts(file: UploadFile = File(...)):
    con = db.connect()
    try:
        created, matched = migrate.import_accounts(con, migrate.parse_accounts(await file.read()))
        con.commit()
        return _migrate_redirect(msg=f"Accounts: {created} created, {matched} already existed.")
    except ValueError as e:
        return _migrate_redirect(err=str(e))
    finally:
        con.close()


@app.post("/migrate/transactions")
async def migrate_transactions(file: UploadFile = File(...)):
    con = db.connect()
    try:
        by_source, skipped = migrate.parse_transactions(con, await file.read())
        staged = migrate.import_transactions(con, by_source, file.filename or "transactions.csv")
        con.commit()
        return _migrate_redirect(msg=(
            f"{staged} transactions staged for Review across {len(by_source)} account(s). "
            f"({skipped['not_bank_card']} rows on category accounts skipped - those are the "
            "same transactions seen from the other side.)"))
    except ValueError as e:
        return _migrate_redirect(err=str(e))
    finally:
        con.close()


@app.post("/migrate/customers")
async def migrate_customers(file: UploadFile = File(...)):
    con = db.connect()
    try:
        created = migrate.import_customers(con, migrate.parse_customers(await file.read()))
        con.commit()
        return _migrate_redirect(msg=f"{created} customers imported.")
    except ValueError as e:
        return _migrate_redirect(err=str(e))
    finally:
        con.close()


@app.post("/migrate/mileage")
async def migrate_mileage(file: UploadFile = File(...)):
    con = db.connect()
    try:
        created = migrate.import_mileage(con, migrate.parse_mileage(await file.read()))
        con.commit()
        return _migrate_redirect(msg=f"{created} trips imported.")
    except ValueError as e:
        return _migrate_redirect(err=str(e))
    finally:
        con.close()


@app.post("/migrate/opening")
async def migrate_opening(request: Request):
    form = await request.form()
    con = db.connect()
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
    finally:
        con.close()


# ---------- customers & invoices ----------

def _invoice_rows(con):
    rows = con.execute(
        "SELECT i.*, c.name customer, c.email customer_email FROM invoices i "
        "JOIN customers c ON c.id=i.customer_id ORDER BY i.id DESC").fetchall()
    today = date_cls.today().isoformat()
    out = []
    for r in rows:
        total = invoicing.invoice_total(con, r["id"])
        overdue = r["status"] == "sent" and r["due_date"] < today
        out.append({**dict(r), "total": total, "overdue": overdue})
    return out


@app.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, msg: str = "", err: str = ""):
    con = db.connect()
    try:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "invoices.html", ctx(
            request, con, invoices=_invoice_rows(con), customers=customers, msg=msg, err=err,
            email_on=invoicing.email_configured(con)))
    finally:
        con.close()


@app.post("/customers")
def customer_add(name: str = Form(...), email: str = Form(""), address: str = Form(""),
                 phone: str = Form("")):
    con = db.connect()
    try:
        con.execute("INSERT INTO customers(name,email,address,phone) VALUES(?,?,?,?)",
                    (name.strip(), email.strip(), address.strip(), phone.strip()))
        con.commit()
        return RedirectResponse("/invoices", status_code=303)
    finally:
        con.close()


@app.post("/customers/update")
def customer_update(customer_id: int = Form(...), name: str = Form(...), email: str = Form(""),
                    address: str = Form(""), phone: str = Form("")):
    con = db.connect()
    try:
        con.execute("UPDATE customers SET name=?, email=?, address=?, phone=? WHERE id=?",
                    (name.strip(), email.strip(), address.strip(), phone.strip(), customer_id))
        con.commit()
        return RedirectResponse("/invoices", status_code=303)
    finally:
        con.close()


@app.get("/invoices/new", response_class=HTMLResponse)
def invoice_new(request: Request):
    con = db.connect()
    try:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "invoice_new.html", ctx(
            request, con, customers=customers, error=None))
    finally:
        con.close()


@app.post("/invoices/new")
async def invoice_create(request: Request):
    form = await request.form()
    con = db.connect()
    try:
        customer_id = int(form["customer_id"])
        inv_date = ledger.normalize_date(form["date"])
        due_date = ledger.normalize_date(form["due_date"])
        descs = form.getlist("item_desc")
        qtys = form.getlist("item_qty")
        prices = form.getlist("item_price")
        items = []
        for d, q, p in zip(descs, qtys, prices):
            if not d.strip():
                continue
            items.append((d.strip(), float(q or 1), ledger.parse_amount_to_cents(p)))
        if not items:
            raise ValueError("Add at least one line item.")
        number = invoicing.next_number(con)
        cur = con.execute(
            "INSERT INTO invoices(number,customer_id,date,due_date,memo) VALUES(?,?,?,?,?)",
            (number, customer_id, inv_date, due_date, form.get("memo", "").strip()))
        inv_id = cur.lastrowid
        for d, q, u in items:
            con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,?,?)",
                        (inv_id, d, q, u))
        con.commit()
        return RedirectResponse(f"/invoices/{inv_id}", status_code=303)
    except (ValueError, KeyError) as e:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "invoice_new.html", ctx(
            request, con, customers=customers, error=str(e)))
    finally:
        con.close()


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_view(request: Request, invoice_id: int, msg: str = "", err: str = ""):
    con = db.connect()
    try:
        inv, items, total = invoicing.get_invoice(con, invoice_id)
        if not inv:
            return RedirectResponse("/invoices", status_code=303)
        banks = con.execute("SELECT * FROM accounts WHERE kind='bank' AND active=1").fetchall()
        income = con.execute("SELECT * FROM accounts WHERE type='income' AND active=1 ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "invoice_view.html", ctx(
            request, con, inv=inv, items=items, total=total, banks=banks, income=income,
            msg=msg, err=err, email_on=invoicing.email_configured(con),
            biz_address=db.get_setting(con, "business_address", ""),
            biz_email=db.get_setting(con, "business_email", ""),
            biz_phone=db.get_setting(con, "business_phone", ""),
            terms=db.get_setting(con, "invoice_terms", "")))
    finally:
        con.close()


@app.get("/invoices/{invoice_id}/pdf")
def invoice_pdf(invoice_id: int):
    con = db.connect()
    try:
        inv, items, total = invoicing.get_invoice(con, invoice_id)
        if not inv:
            return RedirectResponse("/invoices", status_code=303)
        pdf = invoicing.render_pdf(con, inv, items, total)
        return StreamingResponse(iter([pdf]), media_type="application/pdf",
                                 headers={"Content-Disposition": f"inline; filename={inv['number']}.pdf"})
    finally:
        con.close()


@app.post("/invoices/{invoice_id}/status")
def invoice_status(invoice_id: int, action: str = Form(...)):
    con = db.connect()
    try:
        if action == "sent":
            con.execute("UPDATE invoices SET status='sent' WHERE id=? AND status='draft'", (invoice_id,))
        elif action == "void":
            con.execute("UPDATE invoices SET status='void' WHERE id=? AND status!='paid'", (invoice_id,))
        elif action == "draft":
            con.execute("UPDATE invoices SET status='draft' WHERE id=? AND status IN ('sent','void')", (invoice_id,))
        elif action == "delete":
            con.execute("DELETE FROM invoices WHERE id=? AND status IN ('draft','void')", (invoice_id,))
            con.commit()
            return RedirectResponse("/invoices", status_code=303)
        con.commit()
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    finally:
        con.close()


@app.post("/invoices/{invoice_id}/pay")
def invoice_pay(invoice_id: int, paid_date: str = Form(...), bank_id: int = Form(...),
                income_id: int = Form(...)):
    con = db.connect()
    try:
        inv, items, total = invoicing.get_invoice(con, invoice_id)
        if not inv or inv["status"] == "paid" or total <= 0:
            return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
        d = ledger.normalize_date(paid_date)
        entry_id = ledger.post_entry(con, d, f"Invoice {inv['number']} - {inv['customer']}",
                                     [(bank_id, total), (income_id, -total)],
                                     memo=f"invoice #{inv['number']}")
        con.execute("UPDATE invoices SET status='paid', paid_date=?, paid_entry_id=? WHERE id=?",
                    (d, entry_id, invoice_id))
        con.commit()
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    finally:
        con.close()


@app.post("/invoices/{invoice_id}/unpay")
def invoice_unpay(invoice_id: int):
    con = db.connect()
    try:
        inv = con.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if inv and inv["paid_entry_id"]:
            ledger.delete_entry(con, inv["paid_entry_id"])
        con.execute("UPDATE invoices SET status='sent', paid_date=NULL, paid_entry_id=NULL WHERE id=?",
                    (invoice_id,))
        con.commit()
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    finally:
        con.close()


@app.post("/invoices/{invoice_id}/email")
def invoice_email(invoice_id: int, to_addr: str = Form(...), subject: str = Form(""), body: str = Form("")):
    con = db.connect()
    try:
        inv, items, total = invoicing.get_invoice(con, invoice_id)
        if not inv:
            return RedirectResponse("/invoices", status_code=303)
        pdf = invoicing.render_pdf(con, inv, items, total)
        try:
            invoicing.send_invoice_email(con, inv, total, pdf, to_addr.strip(),
                                         subject.strip() or None, body.strip() or None)
        except Exception as e:
            return RedirectResponse(f"/invoices/{invoice_id}?err=Email failed: {e}", status_code=303)
        con.execute("UPDATE invoices SET status='sent' WHERE id=? AND status='draft'", (invoice_id,))
        con.commit()
        return RedirectResponse(f"/invoices/{invoice_id}?msg=Emailed to {to_addr}", status_code=303)
    finally:
        con.close()


# ---------- tax package ----------

@app.get("/taxes", response_class=HTMLResponse)
def taxes_page(request: Request, year: int = 0):
    con = db.connect()
    try:
        year = year or date_cls.today().year
        start, end = f"{year}-01-01", f"{year}-12-31"
        p = ledger.pnl(con, start, end)
        rate = float(db.get_setting(con, "mileage_rate", "0.70"))
        miles = con.execute("SELECT COALESCE(SUM(miles),0) m FROM mileage WHERE date BETWEEN ? AND ?",
                            (start, end)).fetchone()["m"]
        uncat = con.execute(
            "SELECT COUNT(DISTINCT e.id) c FROM entries e JOIN splits s ON s.entry_id=e.id "
            "JOIN accounts a ON a.id=s.account_id WHERE a.name='Uncategorized Expense' "
            "AND e.date BETWEEN ? AND ?", (start, end)).fetchone()["c"]
        pending = con.execute("SELECT COUNT(*) c FROM staged WHERE status='pending'").fetchone()["c"]
        receipts_matched = con.execute(
            "SELECT COUNT(*) c FROM documents d JOIN entries e ON e.id=d.entry_id "
            "WHERE d.status='matched' AND e.date BETWEEN ? AND ?", (start, end)).fetchone()["c"]
        receipts_unmatched = con.execute("SELECT COUNT(*) c FROM documents WHERE status='unmatched'").fetchone()["c"]
        return templates.TemplateResponse(request, "taxes.html", ctx(
            request, con, year=year, pnl=p, miles=miles, rate=rate,
            mileage_deduction=round(miles * rate * 100), uncat=uncat, pending=pending,
            receipts_matched=receipts_matched, receipts_unmatched=receipts_unmatched))
    finally:
        con.close()


@app.get("/taxes/package.zip")
def tax_package(year: int):
    con = db.connect()
    try:
        start, end = f"{year}-01-01", f"{year}-12-31"
        csvmod = __import__("csv")

        def make_csv(write_rows):
            buf = io.StringIO()
            write_rows(csvmod.writer(buf))
            return buf.getvalue()

        p = ledger.pnl(con, start, end)
        bs = ledger.balance_sheet(con, end)
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
            def pnl_rows(w):
                w.writerow(["Profit & Loss", f"{start} to {end}"]); w.writerow([])
                w.writerow(["INCOME"])
                for i in p["income"]: w.writerow([i["name"], f"{i['amount']/100:.2f}"])
                w.writerow(["Total Income", f"{p['total_income']/100:.2f}"]); w.writerow([])
                w.writerow(["EXPENSES"])
                for x in p["expenses"]: w.writerow([x["name"], f"{x['amount']/100:.2f}"])
                w.writerow(["Total Expenses", f"{p['total_expenses']/100:.2f}"]); w.writerow([])
                w.writerow(["Net Profit", f"{p['net']/100:.2f}"])
            z.writestr(f"{year}_profit_and_loss.csv", make_csv(pnl_rows))

            def bs_rows(w):
                w.writerow(["Balance Sheet", f"as of {end}"]); w.writerow([])
                for section, items_, tot in (("ASSETS", bs["assets"], bs["total_assets"]),
                                             ("LIABILITIES", bs["liabilities"], bs["total_liabilities"]),
                                             ("EQUITY", bs["equity"], bs["total_equity"])):
                    w.writerow([section])
                    for i in items_: w.writerow([i["name"], f"{i['amount']/100:.2f}"])
                    w.writerow([f"Total {section.title()}", f"{tot/100:.2f}"]); w.writerow([])
            z.writestr(f"{year}_balance_sheet.csv", make_csv(bs_rows))

            def txn_rows(w):
                w.writerow(["Date", "Payee", "Memo", "Account", "Amount", "Receipt file"])
                for r in con.execute(
                        "SELECT e.id eid, e.date, e.payee, e.memo, a.name account, s.amount_cents "
                        "FROM entries e JOIN splits s ON s.entry_id=e.id JOIN accounts a ON a.id=s.account_id "
                        "WHERE e.date BETWEEN ? AND ? ORDER BY e.date, e.id", (start, end)):
                    doc = con.execute("SELECT filename FROM documents WHERE entry_id=?", (r["eid"],)).fetchone()
                    w.writerow([r["date"], r["payee"], r["memo"], r["account"],
                                f"{r['amount_cents']/100:.2f}", f"receipts/{doc['filename']}" if doc else ""])
            z.writestr(f"{year}_transactions.csv", make_csv(txn_rows))

            def mile_rows(w):
                rate = float(db.get_setting(con, "mileage_rate", "0.70"))
                w.writerow(["Date", "Miles", "Purpose", "From", "To"])
                tot = 0.0
                for t in con.execute("SELECT * FROM mileage WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)):
                    w.writerow([t["date"], t["miles"], t["purpose"], t["from_loc"], t["to_loc"]])
                    tot += t["miles"]
                w.writerow([]); w.writerow(["Total miles", f"{tot:.1f}"])
                w.writerow(["Rate", f"{rate:.2f}"]); w.writerow(["Deduction", f"{tot*rate:.2f}"])
            z.writestr(f"{year}_mileage.csv", make_csv(mile_rows))

            for d in con.execute(
                    "SELECT d.* FROM documents d LEFT JOIN entries e ON e.id=d.entry_id "
                    "WHERE d.entry_id IS NULL OR e.date BETWEEN ? AND ?", (start, end)):
                fp = Path(d["path"])
                if fp.exists():
                    z.write(fp, f"receipts/{d['filename']}")
        zbuf.seek(0)
        return StreamingResponse(iter([zbuf.getvalue()]), media_type="application/zip",
                                 headers={"Content-Disposition": f"attachment; filename=tax_package_{year}.zip"})
    finally:
        con.close()


# ---------- accounts, rules, settings ----------

@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request):
    con = db.connect()
    try:
        accounts = ledger.accounts_with_balances(con)
        return templates.TemplateResponse(request, "accounts.html", ctx(request, con, accounts=accounts))
    finally:
        con.close()


@app.post("/accounts")
def accounts_add(name: str = Form(...), type: str = Form(...), kind: str = Form("category")):
    con = db.connect()
    try:
        con.execute("INSERT OR IGNORE INTO accounts(name,type,kind) VALUES(?,?,?)", (name.strip(), type, kind))
        con.commit()
        return RedirectResponse("/accounts", status_code=303)
    finally:
        con.close()


@app.post("/accounts/rename")
def accounts_rename(account_id: int = Form(...), name: str = Form(...)):
    con = db.connect()
    try:
        con.execute("UPDATE accounts SET name=? WHERE id=?", (name.strip(), account_id))
        con.commit()
        return RedirectResponse("/accounts", status_code=303)
    finally:
        con.close()


@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    con = db.connect()
    try:
        rules = con.execute(
            "SELECT r.*, a.name account FROM rules r JOIN accounts a ON a.id=r.account_id ORDER BY r.pattern").fetchall()
        return templates.TemplateResponse(request, "rules.html", ctx(request, con, rules=rules, cats=categories(con)))
    finally:
        con.close()


@app.post("/rules")
def rules_add(pattern: str = Form(...), account_id: int = Form(...)):
    con = db.connect()
    try:
        con.execute("INSERT INTO rules(pattern,account_id) VALUES(?,?)", (pattern.strip(), account_id))
        con.commit()
        return RedirectResponse("/rules", status_code=303)
    finally:
        con.close()


@app.post("/rules/delete")
def rules_delete(rule_id: int = Form(...)):
    con = db.connect()
    try:
        con.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        con.commit()
        return RedirectResponse("/rules", status_code=303)
    finally:
        con.close()


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = "", err: str = ""):
    con = db.connect()
    try:
        key = ai.api_key(con)
        s = {k: db.get_setting(con, k, v) for k, v in db.DEFAULT_SETTINGS.items()}
        return templates.TemplateResponse(request, "settings.html", ctx(
            request, con, s=s, key_set=bool(key),
            smtp_set=bool(db.get_setting(con, "smtp_password", "")),
            backup=backup.status(), msg=msg, err=err))
    finally:
        con.close()


@app.get("/backup.zip")
def backup_zip():
    data = backup.zip_bytes()
    ts = date_cls.today().isoformat()
    return StreamingResponse(iter([data]), media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename=shopbooks_backup_{ts}.zip"})


@app.post("/backup/now")
def backup_now():
    backup.snapshot()
    return RedirectResponse("/settings", status_code=303)


@app.post("/ollama/test")
def ollama_test():
    from urllib.parse import quote
    con = db.connect()
    try:
        st = ai.ollama_status(con)
        if not st["reachable"]:
            return RedirectResponse(
                "/settings?err=" + quote(f"Can't reach Ollama at {ai.ollama_url(con)} - is it running? ({st.get('error','')})"),
                status_code=303)
        if not st["model_present"]:
            have = ", ".join(st["models"]) or "none"
            return RedirectResponse(
                "/settings?err=" + quote(f"Ollama is running but model '{st['model']}' isn't installed. "
                                         f"Run:  ollama pull {st['model']}   (installed: {have})"),
                status_code=303)
        return RedirectResponse(
            "/settings?msg=" + quote(f"Ollama OK - reached {ai.ollama_url(con)}, model '{st['model']}' is ready."),
            status_code=303)
    finally:
        con.close()


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    con = db.connect()
    try:
        plain = ("mileage_rate", "ai_backend", "ai_model", "ollama_url", "ollama_model",
                 "business_name", "backup_dir", "business_address", "business_email",
                 "business_phone", "invoice_terms", "smtp_host", "smtp_port", "smtp_user",
                 "email_subject", "email_body")
        for k in plain:
            if k in form:
                db.set_setting(con, k, str(form[k]).strip())
        # secrets: blank = keep current, "CLEAR" = remove
        for k in ("anthropic_api_key", "smtp_password"):
            v = str(form.get(k, "")).strip()
            if v == "CLEAR":
                db.set_setting(con, k, "")
            elif v:
                db.set_setting(con, k, v)
        con.commit()
        # validate the backup folder if one was given, and seed it with a snapshot
        from urllib.parse import quote
        new_dir = str(form.get("backup_dir", "")).strip()
        if new_dir:
            if backup.check_writable(new_dir):
                backup.snapshot()
                return RedirectResponse(
                    f"/settings?msg={quote('Settings saved. Backup folder set and a backup was written there.')}",
                    status_code=303)
            return RedirectResponse(
                f"/settings?err={quote('Settings saved, but that backup folder is not writable - check the path. Falling back to auto-detect.')}",
                status_code=303)
        return RedirectResponse(f"/settings?msg={quote('Settings saved.')}", status_code=303)
    finally:
        con.close()
