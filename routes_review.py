"""Statement import and the Review queue routes."""
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

import ai
import db
import feeds
import importer
import ledger
from staging import _categorize_from_receipts, _post_staged, match_combined_amazon_receipts, resolve_receipt_match, staged_invoice_matches, staged_receipt_matches
from webutil import categories, ctx, templates

router = APIRouter()

@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    con = db.connect()
    try:
        sources = con.execute("SELECT * FROM accounts WHERE kind IN ('bank','card') AND active=1 ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "import.html", ctx(
            request, con, sources=sources, feeds_connected=feeds.connected(con), error=None))
    finally:
        con.close()

@router.post("/import")
async def do_import(request: Request, file: UploadFile = File(...), account_id: int = Form(None)):
    con = db.connect()
    try:
        raw = await file.read()
        name = (file.filename or "statement").lower()
        if not (name.endswith(".csv") or name.endswith(".pdf")):
            raise ValueError("Upload a .pdf or .csv file.")
            
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        if name.endswith(".pdf"):
            tmp = db.DOCS / f"stmt_{timestamp}_{Path(name).name}"
        else:
            tmp = db.DOCS / f"temp_stmt_{timestamp}_{Path(name).name}"
            
        db.DOCS.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(raw)
        
        # 1. Text extraction & Account Auto-detection:
        if name.endswith(".pdf"):
            text = importer.pdf_text(tmp)
        else:
            text = raw.decode("utf-8-sig", errors="replace")
            
        detected_account_id = importer.detect_account_id(con, file.filename or "", text)
        
        target_account_id = account_id if account_id is not None else detected_account_id
        
        acct = con.execute("SELECT * FROM accounts WHERE id=?", (target_account_id,)).fetchone()
        if not acct:
            raise ValueError("Target account not found.")
            
        # 2. Extract transactions:
        txns, note = [], ""
        if name.endswith(".csv"):
            txns = importer.parse_csv(raw)
        elif name.endswith(".pdf"):
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
        
        if not txns:
            if not name.endswith(".pdf") and tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass
            raise ValueError("No transactions found in that file.")
            
        # If account_id was passed, we process single-step import (backwards compatibility for tests)
        if account_id is not None:
            if not name.endswith(".pdf") and tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass
            
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
            
        # Otherwise, render the confirmation page
        sources = con.execute("SELECT * FROM accounts WHERE kind IN ('bank','card') AND active=1 ORDER BY name").fetchall()
        
        import json
        txns_json = json.dumps(txns)
        
        import_warning = importer.is_duplicate_statement(con, target_account_id, txns, file.filename)
        
        return templates.TemplateResponse(request, "import_confirm.html", ctx(
            request, con,
            filename=file.filename,
            temp_file_path=str(tmp),
            txns=txns[:10],
            txns_count=len(txns),
            detected_account_id=detected_account_id,
            sources=sources,
            txns_json=txns_json,
            import_warning=import_warning,
            note=note
        ))
        
    except ValueError as e:
        sources = con.execute("SELECT * FROM accounts WHERE kind IN ('bank','card') AND active=1 ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "import.html", ctx(request, con, sources=sources, error=str(e)))
    finally:
        con.close()

@router.post("/import/confirm")
async def do_import_confirm(
    request: Request,
    filename: str = Form(...),
    temp_file_path: str = Form(...),
    account_id: int = Form(...),
    txns_json: str = Form(...),
    note: str = Form("")
):
    import json
    con = db.connect()
    try:
        txns = json.loads(txns_json)
        if not txns:
            raise ValueError("No transactions to import.")
            
        cur = con.execute("INSERT INTO batches(filename,account_id) VALUES(?,?)", (filename, account_id))
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
        
        if "temp_stmt_" in temp_file_path:
            p = Path(temp_file_path)
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass
                    
        from urllib.parse import quote
        dates = sorted(t["date"] for t in txns if t.get("date"))
        if dates:
            note = (note + f" Imported {len(txns)} transactions dated {dates[0]} to {dates[-1]} "
                    "- check the year looks right before posting.").strip()
        return RedirectResponse("/review?note=" + quote(note), status_code=303)
        
    except Exception as e:
        if "temp_stmt_" in temp_file_path:
            p = Path(temp_file_path)
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass
        sources = con.execute("SELECT * FROM accounts WHERE kind IN ('bank','card') AND active=1 ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "import.html", ctx(request, con, sources=sources, error=str(e)))
    finally:
        con.close()

@router.get("/review", response_class=HTMLResponse)
def review(request: Request, note: str = "", err: str = ""):
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT st.*, b.filename, b.account_id source_id, a.name source_name "
            "FROM staged st JOIN batches b ON b.id=st.batch_id JOIN accounts a ON a.id=b.account_id "
            "WHERE st.status='pending' ORDER BY b.id DESC, st.date, st.id").fetchall()
        receipt_matches = staged_receipt_matches(con)
        invoice_matches = staged_invoice_matches(con)
        unmatched_receipts = con.execute(
            "SELECT * FROM documents WHERE status='unmatched' ORDER BY doc_date DESC, id DESC"
        ).fetchall()
        items = []
        for r in rows:
            booked = importer.find_posted_transfer(con, r["source_id"], r["amount_cents"], r["date"])
            catrow = con.execute("SELECT name, kind FROM accounts WHERE id=?", (r["category_id"],)).fetchone() \
                if r["category_id"] else None
            transfer_to = catrow["name"] if catrow and catrow["kind"] in ("bank", "card") else None
            dup = (importer.possible_duplicate(con, r["source_id"], r["date"], r["amount_cents"])
                   and transfer_to is None and booked is None)
            rdocs = receipt_matches.get(r["id"], [])
            rinv = invoice_matches.get(r["id"])
            items.append({**dict(r), "dup": dup, "transfer_to": transfer_to, "transfer_booked": booked is not None,
                          "receipts": [{"id": d["id"], "vendor": d["vendor"]} for d in rdocs],
                          "matched_doc_ids": {d["id"] for d in rdocs},
                          "invoice_number": rinv["number"] if rinv else None,
                          "invoice_customer": rinv["customer"] if rinv else None,
                          "invoice_id": rinv["id"] if rinv else None})
        cats = categories(con)
        return templates.TemplateResponse(request, "review.html", ctx(request, con, items=items, cats=cats, unmatched_receipts=unmatched_receipts, note=note, err=err, feeds_connected=feeds.connected(con)))
    finally:
        con.close()

@router.post("/review")
async def review_action(request: Request):
    form = await request.form()
    con = db.connect()
    try:
        def cat_for(sid):
            v = form.get(f"cat_{sid}", "")
            return int(v) if v else None

        def splits_for(sid):
            """Category splits typed into a row's Split drawer, as (account_id, magnitude_cents)
            with positive magnitudes. _post_staged applies the row's sign and checks they balance."""
            cids, amts, parts = form.getlist(f"scat_{sid}"), form.getlist(f"samt_{sid}"), []
            for c, a in zip(cids, amts):
                c, a = (c or "").strip(), (a or "").strip()
                if c and a:
                    try:
                        parts.append((int(c), abs(ledger.parse_amount_to_cents(a))))
                    except ValueError:
                        continue
            return parts

        def is_split(sid):
            return form.get(f"splitmode_{sid}") == "1"

        # Persist any typed memos and category picks first, so they survive a reload — changing a
        # row's category (or memo) and then posting/skipping a DIFFERENT row must not revert it.
        # A row in split mode has its single-category select disabled, so `cat_{id}` isn't submitted
        # and we don't clobber its category here. (`scat_`/`samt_` split fields don't match `cat_`.)
        for k in form.keys():
            if k.startswith("memo_"):
                try:
                    msid = int(k.split("_", 1)[1])
                except ValueError:
                    continue
                con.execute("UPDATE staged SET memo=? WHERE id=? AND status='pending'",
                            (str(form[k]).strip(), msid))
            elif k.startswith("cat_"):
                try:
                    csid = int(k.split("_", 1)[1])
                except ValueError:
                    continue
                v = str(form[k]).strip()
                con.execute("UPDATE staged SET category_id=? WHERE id=? AND status='pending'",
                            (int(v) if v else None, csid))

        if "save_matches" in form:
            sid = int(form["save_matches"])
            doc_ids = [int(x) for x in form.getlist(f"docs_{sid}")]
            con.execute("UPDATE documents SET staged_id=NULL WHERE staged_id=?", (sid,))
            con.execute("DELETE FROM document_staged_links WHERE staged_id=?", (sid,))
            if doc_ids:
                con.execute(
                    f"UPDATE documents SET staged_id=? WHERE id IN ({','.join('?' for _ in doc_ids)})",
                    [sid] + doc_ids
                )
                for did in doc_ids:
                    con.execute(
                        "INSERT OR IGNORE INTO document_staged_links(document_id, staged_id) VALUES(?, ?)",
                        (did, sid)
                    )
            con.commit()
            return RedirectResponse("/review", status_code=303)
        elif "post_one" in form:
            sid = int(form["post_one"])
            if is_split(sid):
                if not _post_staged(con, sid, None, splits=splits_for(sid)):
                    from urllib.parse import quote
                    con.commit()
                    return RedirectResponse("/review?err=" + quote(
                        "The split amounts must add up to the transaction total. Nothing was posted."),
                        status_code=303)
            else:
                _post_staged(con, sid, cat_for(sid), remember=f"remember_{sid}" in form)
        elif "skip_one" in form:
            con.execute("UPDATE staged SET status='skipped' WHERE id=?", (int(form["skip_one"]),))
        elif "post_selected" in form:
            for sid in sorted(int(v) for v in form.getlist("sel")):
                if is_split(sid):
                    _post_staged(con, sid, None, splits=splits_for(sid))
                elif cat_for(sid):
                    _post_staged(con, sid, cat_for(sid))
        elif "skip_selected" in form:
            ids = [int(v) for v in form.getlist("sel")]
            if ids:
                qmarks = ",".join("?" * len(ids))
                con.execute(f"UPDATE staged SET status='skipped' WHERE status='pending' AND id IN ({qmarks})", ids)
        elif "set_category_selected" in form:
            ids = [int(v) for v in form.getlist("sel")]
            bulk_cat = form.get("bulk_category", "")
            if ids and bulk_cat:
                qmarks = ",".join("?" * len(ids))
                con.execute(f"UPDATE staged SET category_id=? WHERE status='pending' AND id IN ({qmarks})",
                           [int(bulk_cat)] + ids)
        elif "post_all" in form:
            ids = [int(k.split("_", 1)[1]) for k in form.keys() if k.startswith("cat_")]
            for sid in sorted(ids):
                if is_split(sid):
                    _post_staged(con, sid, None, splits=splits_for(sid))
                elif cat_for(sid):
                    _post_staged(con, sid, cat_for(sid))
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
                "WHERE i.kind='invoice' AND i.customer_id = ? AND a.type = 'income' "
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
