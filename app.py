"""ShopBooks - local double-entry accounting for a one-person business."""
import io
import mimetypes
import os
import sqlite3
from datetime import date as date_cls, datetime
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import zipfile

import ai
import backup
import chat
import db
import importer
import insights
import invoicing
import ledger
import migrate
import reconcile
import sync
import timetracking

BASE = Path(__file__).resolve().parent
app = FastAPI(title="ShopBooks")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["money"] = ledger.fmt_cents


def _static_v():
    """Cache-busting token = newest mtime of any static file. Appended to /static asset URLs so a
    browser always re-fetches CSS/JS after it changes (instead of serving a stale cached copy)."""
    try:
        return str(int(max(f.stat().st_mtime for f in (BASE / "static").glob("*"))))
    except Exception:
        return "0"


templates.env.globals["static_v"] = _static_v  # usable in any template as {{ static_v() }}

db.init()
sync.import_on_boot()  # if cloud sync is on: fast-forward from the other machine (never clobbers)
backup.snapshot()      # protect the books on every launch (local + cloud mirror)


@app.on_event("shutdown")
def _sync_on_close():
    sync.export_on_close()  # push this machine's books to the cloud copy on a clean exit


def ctx(request, con, **kw):
    pending = con.execute("SELECT COUNT(*) c FROM staged WHERE status='pending'").fetchone()["c"]
    unmatched = con.execute("SELECT COUNT(*) c FROM documents WHERE status='unmatched'").fetchone()["c"]
    return {"request": request, "pending_count": pending, "unmatched_count": unmatched,
            "ai_on": ai.available(con), "today": date_cls.today().isoformat(),
            "reset_suspected": backup.reset_suspected(),
            "sync_alert": sync.last_alert(),
            "business_name": db.get_setting(con, "business_name", "My Business"), **kw}


def categories(con, types=("expense", "income", "asset", "liability", "equity")):
    """Account options in tree order, each as a dict with a hierarchical `label`
    ('Parent : Child' for sub-accounts) for use in <select> menus."""
    qmarks = ",".join("?" * len(types))
    rows = con.execute(f"SELECT * FROM accounts WHERE active=1 AND type IN ({qmarks})", types).fetchall()
    names = {r["id"]: r["name"] for r in rows}
    tops = sorted((r for r in rows if not r["parent_id"]), key=lambda r: (r["type"], r["name"]))
    out, placed = [], set()

    def add(r, label):
        out.append({"id": r["id"], "name": r["name"], "type": r["type"], "label": label})
        placed.add(r["id"])

    for p in tops:
        add(p, p["name"])
        for c in sorted((r for r in rows if r["parent_id"] == p["id"]), key=lambda r: r["name"]):
            add(c, f"{p['name']} : {c['name']}")
    for r in rows:  # sub-accounts whose parent was filtered out by `types`
        if r["id"] not in placed:
            label = f"{names.get(r['parent_id'], '')} : {r['name']}".lstrip(" :") if r["parent_id"] else r["name"]
            add(r, label)
    return out


def _write_account_section(w, items):
    """Write a P&L / balance-sheet section to a CSV writer, sub-accounts indented under parents."""
    for it in items:
        if it.get("children"):
            w.writerow([it["name"], ""])
            if it.get("own"):
                w.writerow([f"  {it['name']} (direct)", f"{it['own'] / 100:.2f}"])
            for c in it["children"]:
                w.writerow([f"  {c['name']}", f"{c['amount'] / 100:.2f}"])
            w.writerow([f"  Total {it['name']}", f"{it['amount'] / 100:.2f}"])
        else:
            w.writerow([it["name"], f"{it['amount'] / 100:.2f}"])


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
        _categorize_from_receipts(con)
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
        receipt_matches = staged_receipt_matches(con)
        invoice_matches = staged_invoice_matches(con)
        items = []
        for r in rows:
            booked = importer.find_posted_transfer(con, r["source_id"], r["amount_cents"], r["date"])
            catrow = con.execute("SELECT name, kind FROM accounts WHERE id=?", (r["category_id"],)).fetchone() \
                if r["category_id"] else None
            transfer_to = catrow["name"] if catrow and catrow["kind"] in ("bank", "card") else None
            dup = (importer.possible_duplicate(con, r["source_id"], r["date"], r["amount_cents"])
                   and transfer_to is None and booked is None)
            rdoc = receipt_matches.get(r["id"])
            rinv = invoice_matches.get(r["id"])
            items.append({**dict(r), "dup": dup, "transfer_to": transfer_to, "transfer_booked": booked is not None,
                          "receipt_vendor": rdoc["vendor"] if rdoc else None,
                          "receipt_id": rdoc["id"] if rdoc else None,
                          "invoice_number": rinv["number"] if rinv else None,
                          "invoice_customer": rinv["customer"] if rinv else None,
                          "invoice_id": rinv["id"] if rinv else None})
        cats = categories(con)
        return templates.TemplateResponse(request, "review.html", ctx(request, con, items=items, cats=cats, note=note))
    finally:
        con.close()


def _link_staged_receipt(con, staged_id, entry_id):
    """Find the receipt(s) matched to a staged transaction and link them directly to the posted entry."""
    matches = staged_receipt_matches(con)
    doc = matches.get(staged_id)
    if not doc:
        return
    
    # Check if this was a combined Amazon match
    if "Amazon (" in (doc["vendor"] or ""):
        st = con.execute("SELECT * FROM staged WHERE id=?", (staged_id,)).fetchone()
        if not st:
            return
        from datetime import date as dt_date, timedelta
        target = st["amount_cents"]
        try:
            ed = dt_date.fromisoformat(st["date"])
        except Exception:
            return
        docs = con.execute(
            "SELECT * FROM documents WHERE status='unmatched' AND vendor='Amazon' AND amount_cents IS NOT NULL"
        ).fetchall()
        window_docs = []
        for d in docs:
            if not d["doc_date"]:
                continue
            try:
                dd = dt_date.fromisoformat(d["doc_date"])
                if ed - timedelta(days=4) <= dd <= ed + timedelta(days=1):
                    window_docs.append(d)
            except Exception:
                continue
        if len(window_docs) >= 2:
            n = len(window_docs)
            matching_subsets = []
            for i in range(1 << n):
                subset = [window_docs[j] for j in range(n) if (i & (1 << j))]
                if len(subset) >= 2 and sum(d["amount_cents"] for d in subset) == target:
                    matching_subsets.append(subset)
            if len(matching_subsets) == 1:
                for d in matching_subsets[0]:
                    con.execute("UPDATE documents SET status='matched', entry_id=? WHERE id=?", (entry_id, d["id"]))
    else:
        con.execute("UPDATE documents SET status='matched', entry_id=? WHERE id=?", (entry_id, doc["id"]))


def _post_staged(con, staged_id, category_id, remember=False):
    st = con.execute(
        "SELECT st.*, b.account_id source_id FROM staged st JOIN batches b ON b.id=st.batch_id WHERE st.id=?",
        (staged_id,)).fetchone()
    if not st or st["status"] != "pending" or not category_id:
        return
    # post-once for transfers: if the category is one of your own accounts (a transfer) and the
    # very same transfer is already booked from the other statement, skip instead of double-counting.
    cat = con.execute("SELECT kind FROM accounts WHERE id=?", (category_id,)).fetchone()
    if cat and cat["kind"] in ("bank", "card") and \
            importer.find_posted_transfer(con, st["source_id"], st["amount_cents"], st["date"]) is not None:
        con.execute("UPDATE staged SET status='skipped' WHERE id=?", (staged_id,))
        return
    entry_id = ledger.post_entry(con, st["date"], st["description"],
                                 [(category_id, st["amount_cents"]), (st["source_id"], -st["amount_cents"])],
                                 memo=st["memo"])
    _link_staged_receipt(con, staged_id, entry_id)
    if category_id:
        cat_type = con.execute("SELECT type FROM accounts WHERE id=?", (category_id,)).fetchone()
        if cat_type and cat_type["type"] == "income":
            inv_matches = staged_invoice_matches(con)
            matched_inv = inv_matches.get(staged_id)
            if matched_inv:
                con.execute("UPDATE invoices SET status='paid', paid_date=?, matched_entry_id=? WHERE id=?",
                            (st["date"], entry_id, matched_inv["id"]))
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

        # Persist any typed memos first, so they survive a reload and are carried onto the entry
        # when the row is posted (memo flows through _post_staged -> ledger.post_entry).
        for k in form.keys():
            if k.startswith("memo_"):
                try:
                    msid = int(k.split("_", 1)[1])
                except ValueError:
                    continue
                con.execute("UPDATE staged SET memo=? WHERE id=? AND status='pending'",
                            (str(form[k]).strip(), msid))

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
            val = form.get("flip_batch")
            if val and val.isdigit():
                bid = int(val)
            else:
                bid_val = form.get("batch_id")
                bid = int(bid_val) if (bid_val and bid_val.isdigit()) else 0
            if bid:
                con.execute("UPDATE staged SET amount_cents=-amount_cents WHERE batch_id=? AND status='pending'", (bid,))
        elif "discard_batch" in form:
            # drop the not-yet-posted rows of one import (e.g. to redo an import); posted rows untouched
            val = form.get("discard_batch")
            if val and val.isdigit():
                bid = int(val)
            else:
                bid_val = form.get("batch_id")
                bid = int(bid_val) if (bid_val and bid_val.isdigit()) else 0
            if bid:
                con.execute("DELETE FROM staged WHERE batch_id=? AND status='pending'", (bid,))
        elif "ai_review" in form:
            return _ai_review_pending(con)
        elif "find_transfers" in form:
            from urllib.parse import quote
            pairs = importer.rescan_transfers(con)
            con.commit()
            note = (f"Matched {pairs} transfer pair(s) - each is categorized to the other account and "
                    "will post once. Review the ↔ rows, then Post all." if pairs else
                    "No new transfers found (need an equal, opposite amount in another of your "
                    "accounts within 7 days).")
            return RedirectResponse("/review?note=" + quote(note), status_code=303)
        elif "receipt_categorize" in form:
            return _receipt_review_pending(con)
        elif "invoice_categorize" in form:
            return _invoice_review_pending(con)
        # Auto-match unmatched documents/receipts (including combined ones) to the newly posted entries
        for d in con.execute("SELECT * FROM documents WHERE status='unmatched' AND amount_cents IS NOT NULL").fetchall():
            match_entry_id = resolve_receipt_match(con, d)
            if match_entry_id:
                con.execute("UPDATE documents SET status='matched', entry_id=? WHERE id=?", (match_entry_id, d["id"]))
        match_combined_amazon_receipts(con)
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

    # Internal transfers (credit-card payments, bank<->bank moves) are not expenses; detect them
    # first so neither a rule nor the AI forces them into an expense category. rescan points each
    # matched side at the partner account; we then leave those rows untouched below.
    transfers = importer.rescan_transfers(con)
    transfer_ids = {r["id"] for r in con.execute(
        "SELECT st.id FROM staged st JOIN accounts a ON a.id=st.category_id "
        "WHERE st.status='pending' AND a.kind IN ('bank','card')").fetchall()}

    cats = {a["id"]: a["name"] for a in categories(con, ("expense", "income"))}
    name_to_id = {v: k for k, v in cats.items()}
    hist = importer.history_map(con)
    ruled, from_history, ai_targets = 0, 0, []
    for s in pending:
        if s["id"] in transfer_ids:
            continue  # a detected transfer - leave it pointed at the other account
        rid = importer.apply_rules(con, s["description"])
        hid = hist.get(importer.payee_key(s["description"]))
        if rid:
            con.execute("UPDATE staged SET category_id=? WHERE id=?", (rid, s["id"]))
            ruled += 1
        elif hid in cats:  # the user's own past categorization for this vendor
            con.execute("UPDATE staged SET category_id=? WHERE id=?", (hid, s["id"]))
            from_history += 1
        else:
            ai_targets.append(s)

    filled = 0
    ai_failed = False
    if ai_targets:
        suggestions = ai.categorize(
            con, [{"description": s["description"], "amount": s["amount_cents"]} for s in ai_targets],
            list(cats.values()))
        if suggestions is None:
            ai_failed = True
        else:
            for s, name in zip(ai_targets, suggestions):
                cid = name_to_id.get(name)
                if cid:
                    con.execute("UPDATE staged SET category_id=? WHERE id=?", (cid, s["id"]))
                    filled += 1
    con.commit()
    if ai_failed and not (ruled or from_history):
        return back("AI couldn't categorize this batch - try again, or set categories manually.")
    parts = []
    if transfers:
        parts.append(f"{transfers} matched as transfers")
    if ruled:
        parts.append(f"{ruled} matched a rule")
    if from_history:
        parts.append(f"{from_history} from your past categories")
    if filled:
        parts.append(f"{filled} suggested by AI")
    if ai_failed:
        parts.append("AI was unavailable for the rest")
    return back("AI review done: " + (", ".join(parts) or "nothing to do") + ". Check the suggestions and post.")


def _receipt_review_pending(con):
    """Match unmatched receipts to pending staged rows by amount+date, use receipt
    content to AI-suggest categories. Suggestions only; nothing posts."""
    from urllib.parse import quote

    def back(note):
        return RedirectResponse("/review?note=" + quote(note), status_code=303)

    matched, categorized, err = _categorize_from_receipts(con)
    con.commit()
    if not matched and not err:
        return back("No matching receipts found for pending transactions "
                    "(need an unmatched receipt with the same dollar amount, within 7 days).")
    parts = []
    if matched:
        parts.append(f"{matched} receipt(s) matched to pending rows")
    if categorized:
        parts.append(f"{categorized} categorized from receipt content")
    if err:
        parts.append(err)
    return back("📎 " + "; ".join(parts) + ". Check the suggestions and post.")


def _categorize_from_invoices(con):
    """Match open invoices to pending staged deposits, then suggest correct income categories.
    Returns (match_count, categorized_count, error_or_None)."""
    matches = staged_invoice_matches(con)
    if not matches:
        return 0, 0, None
    cats = {a["name"]: a["id"] for a in con.execute(
        "SELECT id, name FROM accounts WHERE type='income' AND active=1"
    ).fetchall()}
    ai_suggestions = {}
    if ai.available(con) and cats:
        targets = [(sid, inv) for sid, inv in matches.items()]
        txns = []
        for sid, inv in targets:
            items = con.execute("SELECT description FROM invoice_items WHERE invoice_id=?", (inv["id"],)).fetchall()
            item_desc = ", ".join(it["description"] for it in items)
            desc = f"Customer: {inv['customer']} | Memo: {inv['memo']} | Items: {item_desc}"
            txns.append({"description": desc, "amount": inv["total"]})
        suggestions = ai.categorize(con, txns, list(cats.keys()))
        if suggestions:
            for (sid, inv), name in zip(targets, suggestions):
                cid = cats.get(name)
                if cid:
                    ai_suggestions[sid] = cid
    categorized = 0
    for sid, inv in matches.items():
        cid = ai_suggestions.get(sid)
        if not cid:
            hist_row = con.execute(
                "SELECT s.account_id FROM invoices i "
                "JOIN entries e ON e.id = COALESCE(i.paid_entry_id, i.matched_entry_id) "
                "JOIN splits s ON s.entry_id = e.id "
                "JOIN accounts a ON a.id = s.account_id "
                "WHERE i.customer_id = ? AND a.type = 'income' "
                "GROUP BY s.account_id ORDER BY COUNT(*) DESC LIMIT 1",
                (inv["customer_id"],)
            ).fetchone()
            if hist_row:
                cid = hist_row["account_id"]
            elif cats:
                income_names = list(cats.keys())
                preferred = [n for n in income_names if "service" in n.lower() or "sales" in n.lower() or "revenue" in n.lower()]
                if preferred:
                    cid = cats[preferred[0]]
                else:
                    cid = list(cats.values())[0]
        if cid:
            con.execute("UPDATE staged SET category_id=? WHERE id=? AND status='pending'", (cid, sid))
            categorized += 1
    err = None
    if not ai_suggestions and ai.available(con) and cats:
        err = "matched invoices but AI couldn't suggest categories — fell back to history/defaults"
    elif not ai.available(con):
        err = "AI is off — fell back to history/defaults"
    return len(matches), categorized, err


def _invoice_review_pending(con):
    """Match open invoices to pending staged deposits, suggest income categories.
    Suggestions only; nothing posts."""
    from urllib.parse import quote
    def back(note):
        return RedirectResponse("/review?note=" + quote(note), status_code=303)
    matched, categorized, err = _categorize_from_invoices(con)
    con.commit()
    if not matched and not err:
        return back("No matching open invoices found for pending deposits "
                    "(need an unpaid invoice with the same dollar amount, near the deposit date).")
    parts = []
    if matched:
        parts.append(f"{matched} invoice(s) matched to pending deposits")
    if categorized:
        parts.append(f"{categorized} categorized")
    if err:
        parts.append(err)
    return back("📄 " + "; ".join(parts) + ". Check the suggestions and post.")


# ---------- registers & entries ----------

@app.get("/register/{account_id}", response_class=HTMLResponse)
def register_view(request: Request, account_id: int, err: str = ""):
    con = db.connect()
    try:
        acct, rows = ledger.register(con, account_id)
        bal = ledger.display_balance(acct["type"], ledger.raw_balance(con, account_id))
        return templates.TemplateResponse(request, "register.html", ctx(
            request, con, acct=acct, rows=rows, balance=bal, jobs=_active_jobs(con), 
            cats=categories(con), bank_cards=ledger.accounts_with_balances(con, kinds=('bank', 'card')), err=err))
    finally:
        con.close()


@app.post("/entry/edit/{entry_id}")
def entry_edit(entry_id: int,
               date: str = Form(...),
               payee: str = Form(...),
               memo: str = Form(""),
               account_id: str = Form(None),
               category_id: str = Form(None),
               job_id: str = Form(""),
               register_account_id: int = Form(None),
               back: str = Form("/")):
    con = db.connect()
    try:
        norm_date = ledger.normalize_date(date)
        cat_id = int(category_id) if category_id and category_id.strip() else None
        new_reg_acct_id = int(account_id) if account_id and account_id.strip() else None
        job_val = int(job_id) if job_id and job_id.strip() else None
        
        ledger.update_entry_fields(con, entry_id, payee, memo, cat_id, job_val, norm_date, register_account_id, new_reg_acct_id)
        con.commit()
        return RedirectResponse(back if back.startswith("/") else "/", status_code=303)
    except ValueError as e:
        redirect_url = back if back.startswith("/") else "/"
        sep = "&" if "?" in redirect_url else "?"
        return RedirectResponse(f"{redirect_url}{sep}err={str(e)}", status_code=303)
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


def _active_jobs(con):
    return con.execute("SELECT id, name FROM jobs WHERE status='active' ORDER BY created_at DESC").fetchall()


@app.get("/entry/new", response_class=HTMLResponse)
def entry_new(request: Request):
    con = db.connect()
    try:
        return templates.TemplateResponse(request, "entry.html", ctx(
            request, con, cats=categories(con), jobs=_active_jobs(con), error=None))
    finally:
        con.close()


@app.post("/entry/new")
def entry_create(request: Request, date: str = Form(...), payee: str = Form(...),
                 amount: str = Form(...), to_account: int = Form(...), from_account: int = Form(...),
                 memo: str = Form(""), job_id: str = Form("")):
    con = db.connect()
    try:
        cents = ledger.parse_amount_to_cents(amount)
        ledger.post_entry(con, ledger.normalize_date(date), payee,
                          [(to_account, cents), (from_account, -cents)], memo,
                          job_id=int(job_id) if job_id.strip() else None)
        con.commit()
        return RedirectResponse("/", status_code=303)
    except ValueError as e:
        return templates.TemplateResponse(request, "entry.html", ctx(
            request, con, cats=categories(con), jobs=_active_jobs(con), error=str(e)))
    finally:
        con.close()


@app.post("/entry/{entry_id}/job")
def entry_set_job(entry_id: int, job_id: str = Form(""), back: str = Form("/")):
    con = db.connect()
    try:
        ledger.set_entry_job(con, entry_id, int(job_id) if job_id.strip() else None)
        con.commit()
        return RedirectResponse(back if back.startswith("/") else "/", status_code=303)
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


def vendor_match(vendor, payee):
    if not vendor or not payee:
        return False
    from importer import payee_key
    v = payee_key(vendor)
    p = payee_key(payee)
    if not v or not p:
        return False
    if v in p or p in v:
        return True
    v_words = {w for w in v.split() if len(w) >= 3}
    p_words = {w for w in p.split() if len(w) >= 3}
    if v_words & p_words:
        return True
    return False


def resolve_receipt_match(con, doc):
    """Find a unique candidate entry for a receipt document using date, amount, and vendor matching."""
    cands = receipt_candidates(con, doc)
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]["id"]
        
    # If there are multiple candidates, try to match by vendor name
    vendor = doc["vendor"] if doc["vendor"] else ""
    if vendor:
        from importer import payee_key
        matched_cands = [c for c in cands if vendor_match(vendor, c["payee"])]
        if len(matched_cands) == 1:
            return matched_cands[0]["id"]
        if len(matched_cands) > 1:
            first_key = payee_key(matched_cands[0]["payee"])
            if all(payee_key(c["payee"]) == first_key for c in matched_cands):
                if doc["doc_date"]:
                    from datetime import date as dt_date
                    def dist(c):
                        try:
                            return abs((dt_date.fromisoformat(c["date"]) - dt_date.fromisoformat(doc["doc_date"])).days)
                        except Exception:
                            return 999
                    matched_cands.sort(key=dist)
                return matched_cands[0]["id"]

    # If no vendor was extracted, but all candidates are for the exact same payee (and thus interchangeable):
    from importer import payee_key
    first_key = payee_key(cands[0]["payee"])
    if all(payee_key(c["payee"]) == first_key for c in cands):
        if doc["doc_date"]:
            from datetime import date as dt_date
            def dist(c):
                try:
                    return abs((dt_date.fromisoformat(c["date"]) - dt_date.fromisoformat(doc["doc_date"])).days)
                except Exception:
                    return 999
            cands.sort(key=dist)
        return cands[0]["id"]

    return None


def match_combined_amazon_receipts(con):
    """Find unmatched Amazon entries that sum exactly to a combination of unmatched Amazon receipts.
    Matches them in the database and returns the number of newly matched documents."""
    # 1. Fetch all unmatched expense entries where payee is Amazon
    entries = con.execute(
        "SELECT DISTINCT e.id, e.date, e.payee, s.amount_cents FROM entries e "
        "JOIN splits s ON s.entry_id=e.id "
        "JOIN accounts a ON a.id=s.account_id "
        "WHERE a.type='expense' "
        "AND e.payee LIKE '%AMAZON%' "
        "AND NOT EXISTS (SELECT 1 FROM documents d WHERE d.entry_id=e.id)"
    ).fetchall()
    
    # 2. Fetch all unmatched Amazon documents
    docs = con.execute(
        "SELECT * FROM documents "
        "WHERE status='unmatched' "
        "AND vendor='Amazon' "
        "AND amount_cents IS NOT NULL"
    ).fetchall()
    
    if not entries or not docs:
        return 0
        
    from datetime import date as dt_date, timedelta
    
    matched_count = 0
    assigned_doc_ids = set()
    
    for entry in entries:
        target = entry["amount_cents"]
        entry_date_str = entry["date"]
        try:
            ed = dt_date.fromisoformat(entry_date_str)
        except ValueError:
            continue
            
        # Filter unmatched docs within date window: [entry_date - 4 days, entry_date + 1 day]
        window_docs = []
        for d in docs:
            if d["id"] in assigned_doc_ids or not d["doc_date"]:
                continue
            try:
                dd = dt_date.fromisoformat(d["doc_date"])
                if ed - timedelta(days=4) <= dd <= ed + timedelta(days=1):
                    window_docs.append(d)
            except ValueError:
                continue
                
        if len(window_docs) < 2:
            continue
            
        # Find subset sum
        n = len(window_docs)
        matching_subsets = []
        for i in range(1 << n):
            subset = [window_docs[j] for j in range(n) if (i & (1 << j))]
            if len(subset) >= 2 and sum(d["amount_cents"] for d in subset) == target:
                matching_subsets.append(subset)
                
        # If exactly one unique combination matches the target, link them!
        if len(matching_subsets) == 1:
            best_subset = matching_subsets[0]
            for d in best_subset:
                con.execute("UPDATE documents SET status='matched', entry_id=? WHERE id=?", (entry["id"], d["id"]))
                assigned_doc_ids.add(d["id"])
                matched_count += 1
                
    return matched_count


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
    match_entry_id = resolve_receipt_match(con, doc)
    if match_entry_id:
        con.execute("UPDATE documents SET status='matched', entry_id=? WHERE id=?", (match_entry_id, doc["id"]))
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
            entry, cat = None, None
            if d["entry_id"]:
                entry = con.execute("SELECT * FROM entries WHERE id=?", (d["entry_id"],)).fetchone()
                cat = ledger.entry_category(con, d["entry_id"])  # None for transfers/multi-split
            items.append({"doc": d, "candidates": cands, "entry": entry, "category": cat})
        exp_cats = con.execute("SELECT id, name FROM accounts WHERE type='expense' AND active=1 ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "receipts.html", ctx(
            request, con, items=items, exp_cats=exp_cats, msg=msg, err=err))
    finally:
        con.close()


@app.get("/receipts/missing", response_class=HTMLResponse)
def receipts_missing(request: Request, period: str = "this-year", minamt: str = ""):
    con = db.connect()
    try:
        try:
            start, end, label = insights.parse_period(period)
        except ValueError:
            period, (start, end, label) = "this-year", insights.parse_period("this-year")
        try:
            min_cents = ledger.parse_amount_to_cents(minamt) if minamt.strip() else 0
        except ValueError:
            min_cents = 0
        rows = insights.missing_receipts(con, start, end, min_cents)
        return templates.TemplateResponse(request, "receipts_missing.html", ctx(
            request, con, rows=rows, period=period, label=label, minamt=minamt,
            total=sum(r["amount"] for r in rows), count=len(rows)))
    finally:
        con.close()


def _ingest_amazon_order(con, order):
    """Turn one parsed Amazon order into a receipt document and auto-match. Dedupes on order id.
    Returns 'matched' | 'imported' | 'duplicate'."""
    import hashlib
    sha = hashlib.sha256(("amazon:" + order["order_id"]).encode()).hexdigest()
    if con.execute("SELECT 1 FROM documents WHERE sha256=?", (sha,)).fetchone():
        return "duplicate"
    lines = [f"Amazon order {order['order_id']}", f"Date: {order['date']}", ""]
    lines += [f"  - {it}" for it in order["items"]] or ["  (item names not in export)"]
    lines += ["", f"Order total: ${ledger.fmt_cents(order['total_cents'])}"]
    db.DOCS.mkdir(parents=True, exist_ok=True)
    safe = "amazon_" + "".join(c if c.isalnum() else "-" for c in order["order_id"])[:40] + ".txt"
    dest = db.DOCS / f"rcpt_{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{safe}"
    dest.write_text("\n".join(lines), encoding="utf-8")
    cur = con.execute(
        "INSERT INTO documents(filename,path,vendor,doc_date,amount_cents,sha256) VALUES(?,?,?,?,?,?)",
        (safe, str(dest), "Amazon", order["date"], order["total_cents"], sha))
    doc = con.execute("SELECT * FROM documents WHERE id=?", (cur.lastrowid,)).fetchone()
    match_entry_id = resolve_receipt_match(con, doc)
    if match_entry_id:
        con.execute("UPDATE documents SET status='matched', entry_id=? WHERE id=?", (match_entry_id, doc["id"]))
        return "matched"
    return "imported"


@app.post("/receipts/upload")
async def receipts_upload(files: list[UploadFile] = File(...)):
    con = db.connect()
    try:
        for f in files:
            _ingest_receipt(con, await f.read(), f.filename or "receipt.jpg")
        _categorize_from_receipts(con)
        con.commit()
        return RedirectResponse("/receipts", status_code=303)
    finally:
        con.close()


@app.post("/receipts/import-amazon")
async def receipts_import_amazon(file: UploadFile = File(...)):
    from urllib.parse import quote
    con = db.connect()
    try:
        try:
            orders = importer.parse_amazon_orders(await file.read())
        except ValueError as e:
            return RedirectResponse("/receipts?err=" + quote(str(e)), status_code=303)
        counts = {"matched": 0, "imported": 0, "duplicate": 0}
        for o in orders:
            counts[_ingest_amazon_order(con, o)] += 1
        combined_matched = match_combined_amazon_receipts(con)
        _categorize_from_receipts(con)
        con.commit()
        total_matched = counts["matched"] + combined_matched
        remaining_imported = max(0, counts["imported"] - combined_matched)
        note = (f"Amazon: {len(orders)} orders read - {total_matched} matched (including combined), "
                f"{remaining_imported} imported (need matching), {counts['duplicate']} already imported. "
                "Amazon bills per shipment, so some orders won't match a single charge - match those by hand.")
        return RedirectResponse("/receipts?msg=" + quote(note), status_code=303)
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
        _categorize_from_receipts(con)
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
            match_entry_id = resolve_receipt_match(con, d)
            if match_entry_id:
                con.execute("UPDATE documents SET status='matched', entry_id=? WHERE id=?", (match_entry_id, d["id"]))
                matched += 1
        combined_matched = match_combined_amazon_receipts(con)
        matched += combined_matched
        con.commit()
        return RedirectResponse("/receipts?msg=" + quote(f"Re-checked matches: {matched} newly matched."), status_code=303)
    finally:
        con.close()


def _receipt_context(doc):
    """Text describing what a receipt was for, to feed categorization: vendor + (for Amazon/text
    receipts) the itemized contents of the saved file."""
    parts = [doc["vendor"]] if doc["vendor"] else []
    p = Path(doc["path"])
    if p.suffix.lower() == ".txt" and p.exists():
        try:
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return " | ".join(parts)[:2000] or (doc["vendor"] or "receipt")


def staged_receipt_matches(con):
    """Find unmatched receipts whose amount matches a pending staged transaction (±7 days).
    Also supports matching a staged Amazon transaction to a combination of unmatched Amazon receipts.
    Returns a dict {staged_id: doc_row} — one receipt per staged row (nearest date, each
    receipt used at most once)."""
    from collections import defaultdict
    from datetime import date as dt_date, timedelta
    docs = con.execute(
        "SELECT * FROM documents WHERE status='unmatched' AND amount_cents IS NOT NULL"
    ).fetchall()
    if not docs:
        return {}
    pending = con.execute(
        "SELECT id, date, amount_cents, description FROM staged WHERE status='pending'"
    ).fetchall()
    if not pending:
        return {}
    by_amount = defaultdict(list)
    for d in docs:
        by_amount[d["amount_cents"]].append(d)
    candidates = []  # (staged_id, doc, date_distance)
    for st in pending:
        for doc in by_amount.get(st["amount_cents"], []):
            if doc["doc_date"] and st["date"]:
                try:
                    dist = abs((dt_date.fromisoformat(st["date"]) -
                                dt_date.fromisoformat(doc["doc_date"])).days)
                except (ValueError, TypeError):
                    dist = 0
                if dist > 7:
                    continue
            else:
                dist = 0
            candidates.append((st["id"], doc, dist))
    # Greedy assignment: nearest date first, each receipt and staged row used at most once
    candidates.sort(key=lambda x: x[2])
    used_docs, used_staged, result = set(), set(), {}
    for sid, doc, _ in candidates:
        if sid in used_staged or doc["id"] in used_docs:
            continue
        result[sid] = doc
        used_staged.add(sid)
        used_docs.add(doc["id"])

    # Now, find combined matches for the remaining unmatched staged rows with AMAZON in description
    remaining_staged = [
        st for st in pending
        if st["id"] not in used_staged and ("AMAZON" in st["description"].upper() or "AMZN" in st["description"].upper())
    ]
    remaining_docs = [
        d for d in docs
        if d["id"] not in used_docs and (d["vendor"] or "").lower() == "amazon"
    ]
    if remaining_staged and remaining_docs:
        for st in remaining_staged:
            target = st["amount_cents"]
            try:
                ed = dt_date.fromisoformat(st["date"])
            except (ValueError, TypeError):
                continue
            window_docs = []
            for d in remaining_docs:
                if d["id"] in used_docs or not d["doc_date"]:
                    continue
                try:
                    dd = dt_date.fromisoformat(d["doc_date"])
                    if ed - timedelta(days=4) <= dd <= ed + timedelta(days=1):
                        window_docs.append(d)
                except (ValueError, TypeError):
                    continue
            if len(window_docs) < 2:
                continue
            n = len(window_docs)
            matching_subsets = []
            for i in range(1 << n):
                subset = [window_docs[j] for j in range(n) if (i & (1 << j))]
                if len(subset) >= 2 and sum(d["amount_cents"] for d in subset) == target:
                    matching_subsets.append(subset)
            if len(matching_subsets) == 1:
                best_subset = matching_subsets[0]
                doc_repr = dict(best_subset[0])
                doc_repr["vendor"] = f"Amazon ({len(best_subset)} items)"
                result[st["id"]] = doc_repr
                used_staged.add(st["id"])
                for d in best_subset:
                    used_docs.add(d["id"])

    return result


def staged_invoice_matches(con):
    """Find open/unpaid invoices whose total matches a pending staged deposit (-staged.amount_cents == invoice_total)
    within a date range (deposit date must be between invoice.date - 5 days and invoice.date + 120 days).
    Returns a dict {staged_id: invoice_row} (best match per staged row — nearest date, one invoice per row)."""
    from collections import defaultdict
    from datetime import date as dt_date
    rows = con.execute(
        "SELECT id, number, customer_id, date, due_date, status, memo FROM invoices "
        "WHERE status != 'void' AND matched_entry_id IS NULL AND paid_entry_id IS NULL"
    ).fetchall()
    if not rows:
        return {}
    open_invs = []
    for r in rows:
        total = invoicing.invoice_total(con, r["id"])
        if total > 0:
            open_invs.append({**dict(r), "total": total})
    if not open_invs:
        return {}
    pending = con.execute(
        "SELECT id, date, amount_cents FROM staged WHERE status='pending' AND amount_cents < 0"
    ).fetchall()
    if not pending:
        return {}
    by_amount = defaultdict(list)
    for inv in open_invs:
        by_amount[inv["total"]].append(inv)
    candidates = []
    for st in pending:
        deposit_amt = -st["amount_cents"]
        for inv in by_amount.get(deposit_amt, []):
            if inv["date"] and st["date"]:
                try:
                    dist = (dt_date.fromisoformat(st["date"]) -
                            dt_date.fromisoformat(inv["date"])).days
                except (ValueError, TypeError):
                    dist = 0
                if dist < -5 or dist > 120:
                    continue
            else:
                dist = 0
            candidates.append((st["id"], inv, abs(dist)))
    candidates.sort(key=lambda x: x[2])
    used_invs, used_staged, result = set(), set(), {}
    for sid, inv, _ in candidates:
        if sid in used_staged or inv["id"] in used_invs:
            continue
        cust = con.execute("SELECT name FROM customers WHERE id=?", (inv["customer_id"],)).fetchone()
        inv["customer"] = cust["name"] if cust else "Unknown"
        result[sid] = inv
        used_staged.add(sid)
        used_invs.add(inv["id"])
    return result


def _categorize_from_receipts(con):
    """Match unmatched receipts to pending staged rows, then use receipt content (vendor/items)
    to AI-suggest categories. Returns (match_count, categorized_count, error_or_None)."""
    matches = staged_receipt_matches(con)
    if not matches:
        return 0, 0, None
    if not ai.available(con):
        return len(matches), 0, ("matched receipts but AI is off — add a Claude API key "
                                  "in Settings for receipt-based suggestions")
    cats = {a["name"]: a["id"] for a in con.execute(
        "SELECT id, name FROM accounts WHERE type IN ('expense','income') AND active=1"
    ).fetchall()}
    targets = [(sid, doc) for sid, doc in matches.items()]
    txns = [{"description": _receipt_context(doc), "amount": doc["amount_cents"] or 0}
            for _, doc in targets]
    suggestions = ai.categorize(con, txns, list(cats.keys()))
    if not suggestions:
        return len(matches), 0, "matched receipts but AI couldn't suggest categories — try again"
    categorized = 0
    for (sid, _), name in zip(targets, suggestions):
        cid = cats.get(name)
        if cid:
            con.execute("UPDATE staged SET category_id=? WHERE id=? AND status='pending'",
                        (cid, sid))
            categorized += 1
    return len(matches), categorized, None


def _recategorize_from_receipts(con, docs):
    """AI-suggest a category (from the expense chart of accounts) for each matched receipt's
    transaction, using the receipt's vendor/items, and re-point the entry's category leg.
    One batched AI call. Returns (changed_count, error_or_None). Suggestions only touch the
    category leg of simple expense entries; transfers/multi-split are skipped."""
    if not ai.available(con):
        return 0, "AI is off - add a Claude API key in Settings."
    targets = [d for d in docs if d["entry_id"] and ledger.entry_category(con, d["entry_id"])]
    if not targets:
        return 0, "No matched receipts with a simple expense category to refine."
    exp = {a["name"]: a["id"] for a in con.execute(
        "SELECT id, name FROM accounts WHERE type='expense' AND active=1").fetchall()}
    txns = [{"description": _receipt_context(d), "amount": d["amount_cents"] or 0} for d in targets]
    sugg = ai.categorize(con, txns, list(exp.keys()))
    if not sugg:
        return 0, "AI couldn't suggest categories - try again."
    changed = 0
    for d, name in zip(targets, sugg):
        nid = exp.get(name)
        if nid and ledger.set_entry_category(con, d["entry_id"], nid) is not None:
            changed += 1
    return changed, None


@app.post("/receipts/recategorize")
def receipts_recategorize(doc_id: int = Form(...)):
    from urllib.parse import quote
    con = db.connect()
    try:
        doc = con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not doc:
            return RedirectResponse("/receipts", status_code=303)
        changed, err = _recategorize_from_receipts(con, [doc])
        con.commit()
        msg = ("Category updated from the receipt." if changed
               else (err or "No change - the transaction isn't a simple expense (maybe a transfer)."))
        return RedirectResponse("/receipts?" + ("msg=" if changed else "err=") + quote(msg), status_code=303)
    finally:
        con.close()


@app.post("/receipts/recategorize-all")
def receipts_recategorize_all():
    from urllib.parse import quote
    con = db.connect()
    try:
        docs = con.execute("SELECT * FROM documents WHERE kind='receipt' AND status='matched'").fetchall()
        changed, err = _recategorize_from_receipts(con, docs)
        con.commit()
        if err and not changed:
            return RedirectResponse("/receipts?err=" + quote(err), status_code=303)
        return RedirectResponse("/receipts?msg=" + quote(
            f"Recategorized {changed} transaction(s) from their receipts. Review them in the registers; "
            "use the dropdown here to fix any you disagree with."), status_code=303)
    finally:
        con.close()


@app.post("/receipts/setcategory")
def receipts_setcategory(doc_id: int = Form(...), account_id: int = Form(...)):
    from urllib.parse import quote
    con = db.connect()
    try:
        doc = con.execute("SELECT entry_id FROM documents WHERE id=?", (doc_id,)).fetchone()
        if doc and doc["entry_id"]:
            old = ledger.set_entry_category(con, doc["entry_id"], account_id)
            con.commit()
            if old is None:
                return RedirectResponse("/receipts?err=" + quote(
                    "Couldn't change that one (not a simple expense, or different account type)."), status_code=303)
        return RedirectResponse("/receipts", status_code=303)
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


_INLINE_MEDIA = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
}


@app.get("/doc/{doc_id}")
def doc_file(doc_id: int):
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return RedirectResponse("/receipts", status_code=303)
        if not os.path.exists(row["path"]):
            # File isn't on this machine. Receipt files DO sync (docs-sync mirrors them via the
            # cloud and sync._import repoints paths), but it may not have run here yet — so show a
            # helpful note instead of 500-ing.
            if (row["vendor"] or "").lower() == "amazon":
                order = row["filename"].replace("amazon_", "").rsplit(".", 1)[0]
                total = ledger.fmt_cents(row["amount_cents"]) if row["amount_cents"] is not None else "—"
                return PlainTextResponse(
                    f"Amazon order {order}\nDate: {row['doc_date'] or '—'}\nTotal: ${total}\n\n"
                    "(The receipt file isn't on this computer yet. If you sync between machines, "
                    "open Settings -> Sync and click 'Pull from cloud now', then refresh.)")
            return PlainTextResponse(
                "Receipt file isn't on this computer yet.\n(If you sync between machines, open "
                "Settings -> Sync and click 'Pull from cloud now', then refresh.)", status_code=404)
        ext = os.path.splitext(row["path"])[1].lower()
        media = _INLINE_MEDIA.get(ext) or mimetypes.guess_type(row["path"])[0] or "application/octet-stream"
        # inline so the browser shows the receipt in a tab (image / PDF / Amazon text) — not a download
        return FileResponse(row["path"], media_type=media, filename=row["filename"],
                            content_disposition_type="inline")
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


# ---------- time tracking & jobs ----------

@app.get("/time", response_class=HTMLResponse)
def time_page(request: Request, start: str = "", end: str = ""):
    con = db.connect()
    try:
        year = date_cls.today().year
        start = start or f"{year}-01-01"
        end = end or f"{year}-12-31"
        return templates.TemplateResponse(request, "time.html", ctx(
            request, con, summary=timetracking.summary(con, start, end),
            entries=timetracking.list_entries(con, start, end), start=start, end=end, year=year,
            jobs=con.execute("SELECT id, name FROM jobs WHERE status='active' ORDER BY created_at DESC").fetchall(),
            cats=timetracking.categories(con),
            default_rate=db.get_setting(con, "default_hourly_rate", "0")))
    finally:
        con.close()


@app.post("/time")
def time_add(date: str = Form(...), hours: float = Form(...), job_id: str = Form(""),
             category: str = Form(""), note: str = Form(""), billable: str = Form(""),
             rate: str = Form("")):
    con = db.connect()
    try:
        rate_cents = None
        if str(rate).strip():
            try:
                rate_cents = ledger.parse_amount_to_cents(rate)
            except ValueError:
                rate_cents = None
        timetracking.add_entry(
            con, ledger.normalize_date(date), hours,
            job_id=int(job_id) if job_id.strip() else None,
            category=category, note=note, billable=bool(billable), rate_cents=rate_cents)
        con.commit()
        return RedirectResponse("/time", status_code=303)
    finally:
        con.close()


@app.post("/time/delete")
def time_delete(entry_id: int = Form(...)):
    con = db.connect()
    try:
        con.execute("DELETE FROM time_entries WHERE id=?", (entry_id,))
        con.commit()
        return RedirectResponse("/time", status_code=303)
    finally:
        con.close()


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    con = db.connect()
    try:
        return templates.TemplateResponse(request, "jobs.html", ctx(
            request, con, jobs=timetracking.jobs_overview(con),
            customers=con.execute("SELECT id, name FROM customers ORDER BY name").fetchall()))
    finally:
        con.close()


@app.post("/jobs")
def jobs_add(name: str = Form(...), customer_id: str = Form(""), notes: str = Form("")):
    con = db.connect()
    try:
        if name.strip():
            timetracking.add_job(con, name,
                                 customer_id=int(customer_id) if customer_id.strip() else None, notes=notes)
            con.commit()
        return RedirectResponse("/jobs", status_code=303)
    finally:
        con.close()


@app.post("/jobs/status")
def jobs_status(job_id: int = Form(...), status: str = Form(...)):
    con = db.connect()
    try:
        timetracking.set_job_status(con, job_id, status)
        con.commit()
        return RedirectResponse("/jobs", status_code=303)
    finally:
        con.close()


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    con = db.connect()
    try:
        rep = timetracking.job_report(con, job_id)
        if not rep:
            return RedirectResponse("/jobs", status_code=303)
        return templates.TemplateResponse(request, "job_detail.html", ctx(request, con, rep=rep))
    finally:
        con.close()


# ---------- reconciliation ----------

@app.get("/reconcile", response_class=HTMLResponse)
def reconcile_page(request: Request, msg: str = ""):
    con = db.connect()
    try:
        return templates.TemplateResponse(request, "reconcile.html", ctx(
            request, con, accounts=reconcile.status(con), msg=msg))
    finally:
        con.close()


@app.get("/reconcile/{account_id}", response_class=HTMLResponse)
def reconcile_account(request: Request, account_id: int, date: str = "", balance: str = "", msg: str = ""):
    con = db.connect()
    try:
        acct = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not acct or acct["kind"] not in ("bank", "card"):
            return RedirectResponse("/reconcile", status_code=303)
        last = reconcile.last_reconciliation(con, account_id)
        result = txns = dups = None
        if date.strip() and balance.strip():  # preview a reconciliation (no save)
            try:
                sd = ledger.normalize_date(date)
                bal = ledger.parse_amount_to_cents(balance)
                result = reconcile.compute(con, account_id, sd, bal)
                after = last["statement_date"] if last else None
                txns = reconcile.period_transactions(con, account_id, after, sd)
                dups = reconcile.likely_duplicates(con, account_id, after, sd)
            except ValueError:
                result = None
        return templates.TemplateResponse(request, "reconcile_account.html", ctx(
            request, con, acct=acct, last=last, history=reconcile.history(con, account_id),
            result=result, txns=txns, dups=dups, date=date, balance=balance, msg=msg))
    finally:
        con.close()


@app.post("/reconcile")
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
        _write_account_section(w, p["income"])
        w.writerow(["Total Income", f"{p['total_income']/100:.2f}"])
        w.writerow([])
        w.writerow(["EXPENSES"])
        _write_account_section(w, p["expenses"])
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


# ---------- insights / analysis ----------

def _insights_facts(label, growth, exp, jobs, cash, health):
    """Compact block of the exact figures (dollars) for the AI narration."""
    m = ledger.fmt_cents
    pct = lambda x: f"{x:+.1f}%" if x is not None else "n/a"
    L = [f"Period: {label} (vs {growth['base_label']})."]
    for k, lbl in (("income", "Income"), ("expenses", "Expenses"), ("net", "Net profit")):
        g = growth[k]
        L.append(f"{lbl}: ${m(g['current'])} vs ${m(g['previous'])} ({pct(g['pct_change'])}).")
    movers = [r for r in exp["rows"] if r["delta"] != 0][:6]
    if movers:
        L.append("Biggest expense changes: " + "; ".join(
            f"{r['name']} ${m(r['current'])} ({'+' if r['delta'] >= 0 else '-'}${m(abs(r['delta']))})" for r in movers))
    prof = [j for j in jobs if j["net_cash"]][:5]
    if prof:
        L.append("Job net profit: " + "; ".join(f"{j['name']} ${m(j['net_cash'])}" for j in prof))
    L.append(f"Cash on hand: ${m(cash['cash_on_hand'])}. Credit-card debt: ${m(cash['card_debt'])}.")
    if health["issues"]:
        L.append("Needs attention: " + "; ".join(health["issues"]) + ".")
    return "\n".join(L)


@app.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request, period: str = "this-year", base: str = "last-year", explain: str = ""):
    con = db.connect()
    try:
        try:
            start, end, label = insights.parse_period(period)
        except ValueError:
            period, (start, end, label) = "this-year", insights.parse_period("this-year")
        pnl = insights.pnl_summary(con, start, end)
        growth = insights.compare(con, period, base)
        trend = insights.monthly_trend(con, start, end)
        exp = insights.expense_changes(con, period, base)
        cash = insights.cash_position(con, end)
        health = insights.bookkeeping_health(con, start, end)
        jobs = [j for j in timetracking.jobs_overview(con) if j["net_cash"] or j["hours"]]
        narrative = None
        if explain and ai.available(con):
            narrative = ai.analyze(con, _insights_facts(label, growth, exp, jobs, cash, health))
        return templates.TemplateResponse(request, "insights.html", ctx(
            request, con, period=period, base=base, label=label, pnl=pnl, growth=growth,
            trend=trend, exp=exp, cash=cash, health=health, jobs=jobs[:8],
            narrative=narrative, explained=bool(explain)))
    finally:
        con.close()


# ---------- assistant (Opus chatbot) ----------

CHAT_HISTORY = []  # in-memory transcript for the assistant (single local user; resets on restart)


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    con = db.connect()
    try:
        return templates.TemplateResponse(request, "chat.html", ctx(
            request, con, history=CHAT_HISTORY, err=None))
    finally:
        con.close()


@app.post("/chat", response_class=HTMLResponse)
def chat_send(request: Request, message: str = Form(""), clear: str = Form("")):
    con = db.connect()
    try:
        if clear:
            CHAT_HISTORY.clear()
            return RedirectResponse("/chat", status_code=303)
        err = None
        msg = message.strip()
        if msg:
            CHAT_HISTORY.append({"role": "user", "content": msg})
            reply, err = chat.ask(con, CHAT_HISTORY)
            if reply:
                CHAT_HISTORY.append({"role": "assistant", "content": reply})
        return templates.TemplateResponse(request, "chat.html", ctx(
            request, con, history=CHAT_HISTORY, err=err))
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
        pairs = importer.rescan_transfers(con)
        con.commit()
        return _migrate_redirect(msg=(
            f"{staged} transactions staged for Review across {len(by_source)} account(s). "
            f"({skipped['not_bank_card']} rows on category accounts skipped - those are the "
            f"same transactions seen from the other side.) {pairs} transfer pair(s) auto-matched."))
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


@app.post("/invoices/import-qbo")
async def invoices_import_qbo(file: UploadFile = File(...)):
    from urllib.parse import quote
    con = db.connect()
    try:
        try:
            parsed = migrate.parse_invoices(await file.read())
        except ValueError as e:
            return RedirectResponse("/invoices?err=" + quote(str(e)), status_code=303)
        created, skipped = migrate.import_invoices(con, parsed)
        con.commit()
        note = (f"Imported {created} invoice(s) from QuickBooks ({skipped} already present). "
                "Records only - these don't post income to your books; income still comes from your "
                "deposit/statement imports.")
        return RedirectResponse("/invoices?msg=" + quote(note), status_code=303)
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


def _match_invoice_to_entry(con, invoice_id, entry_id):
    """Link an invoice to an existing deposit entry (records-only: no ledger posting)."""
    e = con.execute("SELECT date FROM entries WHERE id=?", (entry_id,)).fetchone()
    if not e:
        return False
    con.execute("UPDATE invoices SET status='paid', paid_date=?, matched_entry_id=? WHERE id=?",
                (e["date"], entry_id, invoice_id))
    return True


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_view(request: Request, invoice_id: int, msg: str = "", err: str = ""):
    con = db.connect()
    try:
        inv, items, total = invoicing.get_invoice(con, invoice_id)
        if not inv:
            return RedirectResponse("/invoices", status_code=303)
        banks = con.execute("SELECT * FROM accounts WHERE kind='bank' AND active=1").fetchall()
        income = con.execute("SELECT * FROM accounts WHERE type='income' AND active=1 ORDER BY name").fetchall()
        candidates = matched = None
        if inv["matched_entry_id"]:
            matched = con.execute("SELECT id, date, payee FROM entries WHERE id=?", (inv["matched_entry_id"],)).fetchone()
        elif not inv["paid_entry_id"] and inv["status"] != "void":
            # sent invoices, and QBO-imported 'paid' ones not yet linked to a deposit
            candidates = invoice_deposit_candidates(con, inv, total)
        return templates.TemplateResponse(request, "invoice_view.html", ctx(
            request, con, inv=inv, items=items, total=total, banks=banks, income=income,
            candidates=candidates, matched=matched,
            msg=msg, err=err, email_on=invoicing.email_configured(con),
            biz_address=db.get_setting(con, "business_address", ""),
            biz_email=db.get_setting(con, "business_email", ""),
            biz_phone=db.get_setting(con, "business_phone", ""),
            terms=db.get_setting(con, "invoice_terms", "")))
    finally:
        con.close()


@app.post("/invoices/{invoice_id}/match")
def invoice_match(invoice_id: int, entry_id: int = Form(...)):
    con = db.connect()
    try:
        _match_invoice_to_entry(con, invoice_id, entry_id)
        con.commit()
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    finally:
        con.close()


@app.post("/invoices/{invoice_id}/unmatch")
def invoice_unmatch(invoice_id: int):
    con = db.connect()
    try:
        # only clears the link + paid status; never deletes the deposit entry
        con.execute("UPDATE invoices SET status='sent', paid_date=NULL, matched_entry_id=NULL WHERE id=?",
                    (invoice_id,))
        con.commit()
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    finally:
        con.close()


@app.post("/invoices/match-all")
def invoices_match_all():
    from urllib.parse import quote
    con = db.connect()
    try:
        matched = 0
        rows = con.execute("SELECT id FROM invoices WHERE status != 'void' "
                           "AND matched_entry_id IS NULL AND paid_entry_id IS NULL").fetchall()
        for r in rows:
            inv, _, total = invoicing.get_invoice(con, r["id"])
            cands = invoice_deposit_candidates(con, inv, total)
            if len(cands) == 1:
                _match_invoice_to_entry(con, r["id"], cands[0]["id"])
                matched += 1
        con.commit()
        return RedirectResponse("/invoices?msg=" + quote(
            f"Matched {matched} invoice(s) to deposits already on your books (no new entries created)."),
            status_code=303)
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


@app.get("/invoices/{invoice_id}/summary")
def invoice_summary(invoice_id: int):
    con = db.connect()
    try:
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
        missing_receipts = len(insights.missing_receipts(con, start, end))
        return templates.TemplateResponse(request, "taxes.html", ctx(
            request, con, year=year, pnl=p, miles=miles, rate=rate,
            mileage_deduction=round(miles * rate * 100), uncat=uncat, pending=pending,
            receipts_matched=receipts_matched, receipts_unmatched=receipts_unmatched,
            missing_receipts=missing_receipts))
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
                w.writerow(["INCOME"]); _write_account_section(w, p["income"])
                w.writerow(["Total Income", f"{p['total_income']/100:.2f}"]); w.writerow([])
                w.writerow(["EXPENSES"]); _write_account_section(w, p["expenses"])
                w.writerow(["Total Expenses", f"{p['total_expenses']/100:.2f}"]); w.writerow([])
                w.writerow(["Net Profit", f"{p['net']/100:.2f}"])
            z.writestr(f"{year}_profit_and_loss.csv", make_csv(pnl_rows))

            def bs_rows(w):
                w.writerow(["Balance Sheet", f"as of {end}"]); w.writerow([])
                for section, items_, tot in (("ASSETS", bs["assets"], bs["total_assets"]),
                                             ("LIABILITIES", bs["liabilities"], bs["total_liabilities"]),
                                             ("EQUITY", bs["equity"], bs["total_equity"])):
                    w.writerow([section]); _write_account_section(w, items_)
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

def _set_parent(con, account_id, parent_id):
    """Validate and set/clear an account's parent. Raises ValueError on an invalid move."""
    if not parent_id:
        con.execute("UPDATE accounts SET parent_id=NULL WHERE id=?", (account_id,))
        return
    if parent_id == account_id:
        raise ValueError("An account can't be its own parent.")
    child = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    parent = con.execute("SELECT * FROM accounts WHERE id=?", (parent_id,)).fetchone()
    if not child or not parent:
        raise ValueError("Account not found.")
    if parent["type"] != child["type"]:
        raise ValueError("A sub-account must have the same type as its parent.")
    if parent["parent_id"] is not None:
        raise ValueError("Only two levels are allowed - the parent must be a top-level account.")
    if con.execute("SELECT 1 FROM accounts WHERE parent_id=?", (account_id,)).fetchone():
        raise ValueError("This account has sub-accounts, so it can't also become a sub-account.")
    con.execute("UPDATE accounts SET parent_id=? WHERE id=?", (parent_id, account_id))


@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request, err: str = "", show_hidden: str = ""):
    con = db.connect()
    try:
        accounts = ledger.accounts_with_balances(con, include_inactive=bool(show_hidden))
        parents = [a for a in accounts if a["parent_id"] is None and a["active"]]
        hidden_count = con.execute("SELECT COUNT(*) c FROM accounts WHERE active=0").fetchone()["c"]
        return templates.TemplateResponse(request, "accounts.html", ctx(
            request, con, accounts=accounts, parents=parents, err=err,
            show_hidden=bool(show_hidden), hidden_count=hidden_count))
    finally:
        con.close()


@app.post("/accounts/active")
def accounts_set_active(account_id: int = Form(...), active: int = Form(...), show_hidden: str = Form("")):
    from urllib.parse import quote
    con = db.connect()
    try:
        suffix = "?show_hidden=1" if show_hidden else ""
        if not active:  # hiding: protect reports — refuse if the account has history or active children
            if con.execute("SELECT 1 FROM splits WHERE account_id=? LIMIT 1", (account_id,)).fetchone():
                return RedirectResponse("/accounts" + (suffix or "?") + ("&" if suffix else "") +
                                        "err=" + quote("Can't hide an account that has transactions — it would drop from reports."),
                                        status_code=303)
            if con.execute("SELECT 1 FROM accounts WHERE parent_id=? AND active=1 LIMIT 1", (account_id,)).fetchone():
                return RedirectResponse("/accounts" + (suffix or "?") + ("&" if suffix else "") +
                                        "err=" + quote("Hide or move its sub-accounts first."), status_code=303)
        con.execute("UPDATE accounts SET active=? WHERE id=?", (1 if active else 0, account_id))
        con.commit()
        return RedirectResponse("/accounts" + suffix, status_code=303)
    finally:
        con.close()


@app.post("/accounts")
def accounts_add(name: str = Form(...), type: str = Form("expense"), kind: str = Form("category"),
                 parent_id: str = Form("")):
    from urllib.parse import quote
    con = db.connect()
    try:
        if parent_id:  # sub-account inherits type/kind from its (top-level) parent
            p = con.execute("SELECT * FROM accounts WHERE id=?", (int(parent_id),)).fetchone()
            if not p:
                raise ValueError("Parent account not found.")
            if p["parent_id"] is not None:
                raise ValueError("Pick a top-level account as the parent (only two levels are allowed).")
            cur = con.execute("INSERT INTO accounts(name,type,kind,parent_id) VALUES(?,?,?,?)",
                              (name.strip(), p["type"], p["kind"], p["id"]))
        else:
            cur = con.execute("INSERT INTO accounts(name,type,kind) VALUES(?,?,?)", (name.strip(), type, kind))
        con.commit()
        return RedirectResponse("/accounts", status_code=303)
    except sqlite3.IntegrityError:
        return RedirectResponse("/accounts?err=" + quote(f"An account named '{name.strip()}' already exists (names must be unique)."),
                                status_code=303)
    except ValueError as e:
        return RedirectResponse("/accounts?err=" + quote(str(e)), status_code=303)
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


@app.post("/accounts/parent")
def accounts_set_parent(account_id: int = Form(...), parent_id: str = Form("")):
    from urllib.parse import quote
    con = db.connect()
    try:
        _set_parent(con, account_id, int(parent_id) if parent_id else None)
        con.commit()
        return RedirectResponse("/accounts", status_code=303)
    except ValueError as e:
        return RedirectResponse("/accounts?err=" + quote(str(e)), status_code=303)
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
            backup=backup.status(), restorable=backup.list_restorable()[:30],
            sync_status=sync.status(), msg=msg, err=err))
    finally:
        con.close()


@app.get("/backup.zip")
def backup_zip():
    data = backup.zip_bytes()
    ts = date_cls.today().isoformat()
    return StreamingResponse(iter([data]), media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename=shopbooks_backup_{ts}.zip"})


@app.post("/backup/now")
def backup_now(back: str = Form("/settings")):
    from urllib.parse import quote
    backup.snapshot()
    dest = back if back.startswith("/") else "/settings"
    sep = "&" if "?" in dest else "?"
    return RedirectResponse(f"{dest}{sep}saved=1", status_code=303)


@app.post("/backup/restore")
def backup_restore(name: str = Form(...)):
    from urllib.parse import quote
    con = db.connect()
    try:
        had_data = not backup.looks_fresh(db.DB_PATH)
    finally:
        con.close()
    try:
        backup.restore(name)
    except FileNotFoundError:
        return RedirectResponse("/settings?err=" + quote("That backup could not be found."), status_code=303)
    note = f"Restored from {name}." + (" Your previous data was saved as a pre-restore backup." if had_data else "")
    return RedirectResponse("/settings?msg=" + quote(note), status_code=303)


@app.post("/sync/enable")
def sync_enable(on: str = Form("0")):
    from urllib.parse import quote
    con = db.connect()
    try:
        db.set_setting(con, "sync_enabled", "1" if on == "1" else "0")
        con.commit()
    finally:
        con.close()
    if on == "1" and not sync.cloud():
        return RedirectResponse("/settings?err=" + quote(
            "Sync turned on, but no cloud folder is set. Set a Backup folder (in a synced "
            "Dropbox/OneDrive location) above, then it will sync there."), status_code=303)
    msg = "Cloud sync turned on." if on == "1" else "Cloud sync turned off."
    return RedirectResponse("/settings?msg=" + quote(msg), status_code=303)


@app.post("/sync/now")
def sync_now():
    from urllib.parse import quote
    r = sync.export_on_close()
    s = r.get("status")
    if s == "exported":
        note = f"Synced to the cloud (version {r['version']})."
    elif s == "unchanged":
        note = "Already in sync - nothing to push."
    elif s == "blocked_cloud_newer":
        return RedirectResponse("/settings?err=" + quote(
            "The cloud copy is newer than your last sync - the other computer pushed changes. "
            "Use 'Pull from cloud now' to get them, or 'Keep this computer's books' to overwrite."),
            status_code=303)
    elif s == "no_cloud":
        return RedirectResponse("/settings?err=" + quote(
            "No cloud folder set - set a Backup folder in a synced location first."), status_code=303)
    elif s == "disabled":
        return RedirectResponse("/settings?err=" + quote("Turn cloud sync on first."), status_code=303)
    else:
        note = f"Sync: {s}" + (f" ({r['error']})" if r.get("error") else "")
    return RedirectResponse("/settings?msg=" + quote(note), status_code=303)


@app.post("/sync/pull")
def sync_pull():
    from urllib.parse import quote
    r = sync.pull()
    s = r.get("status")
    if r.get("imported"):
        return RedirectResponse("/settings?msg=" + quote(
            f"Pulled the latest books from the cloud (version {r.get('cloud_version')})."), status_code=303)
    if s == "up_to_date":
        note = "Already up to date with the cloud - nothing to pull."
    elif s == "cloud_unavailable":
        return RedirectResponse("/settings?err=" + quote(
            "The cloud copy hasn't finished downloading yet. Open your sync folder in Finder/Explorer "
            "to force it to download, then try Pull again."), status_code=303)
    elif s == "conflict":
        return RedirectResponse("/settings?err=" + quote(
            "Both this computer and the cloud changed - choose 'Take the cloud copy' or "
            "'Keep this computer's books' below."), status_code=303)
    elif s == "local_ahead":
        return RedirectResponse("/settings?err=" + quote(
            "Your books here are newer than the cloud copy - nothing to pull."), status_code=303)
    elif s == "no_cloud":
        return RedirectResponse("/settings?err=" + quote(
            "No cloud folder set - set a Backup folder in a synced location first."), status_code=303)
    elif s == "disabled":
        return RedirectResponse("/settings?err=" + quote("Turn cloud sync on first."), status_code=303)
    else:
        note = f"Sync: {s}" + (f" ({r['error']})" if r.get("error") else "")
    return RedirectResponse("/settings?msg=" + quote(note), status_code=303)


@app.post("/sync/resolve")
def sync_resolve(choice: str = Form(...)):
    from urllib.parse import quote
    if choice == "cloud":
        r = sync.take_cloud()
        note = "Took the cloud copy; this computer's unsynced changes were saved as a pre-sync backup."
    elif choice == "local":
        r = sync.keep_local()
        note = "Kept this computer's books and overwrote the cloud copy."
    else:
        return RedirectResponse("/settings?err=" + quote("Unknown choice."), status_code=303)
    if r.get("status") in ("no_cloud", "error"):
        return RedirectResponse("/settings?err=" + quote(
            "Could not resolve: " + r.get("error", r.get("status", ""))), status_code=303)
    return RedirectResponse("/settings?msg=" + quote(note), status_code=303)


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
        plain = ("mileage_rate", "default_hourly_rate", "ai_backend", "ai_model", "categorize_model",
                 "ollama_url", "ollama_model", "business_name", "backup_dir", "business_address", "business_email",
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
