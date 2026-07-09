"""Receipt upload/matching routes and document serving."""
import mimetypes
import os
from pathlib import Path
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse

import db
import importer
import insights
import ledger
from staging import RECEIPT_EXTS, _categorize_from_receipts, _ingest_amazon_order, _ingest_receipt, _post_staged, _recategorize_from_receipts, match_combined_amazon_receipts, receipt_candidates, resolve_receipt_match
from webutil import _INLINE_MEDIA, ctx, templates

router = APIRouter()

@router.get("/receipts", response_class=HTMLResponse)
def receipts(request: Request, msg: str = "", err: str = ""):
    con = db.connect()
    try:
        from collections import defaultdict
        docs = con.execute("SELECT * FROM documents WHERE kind='receipt' ORDER BY status DESC, uploaded_at DESC").fetchall()
        
        # 1. Fetch matched staged links map
        staged_links = con.execute("SELECT document_id, staged_id FROM document_staged_links").fetchall()
        doc_staged_map = defaultdict(set)
        for lnk in staged_links:
            doc_staged_map[lnk["document_id"]].add(lnk["staged_id"])

        # 2. Fetch matched entry links map
        entry_links = con.execute("SELECT document_id, entry_id FROM document_entry_links").fetchall()
        doc_entries_map = defaultdict(list)
        for lnk in entry_links:
            entry = con.execute("SELECT * FROM entries WHERE id=?", (lnk["entry_id"],)).fetchone()
            if entry:
                cat = ledger.entry_category(con, lnk["entry_id"])
                doc_entries_map[lnk["document_id"]].append({"entry": entry, "category": cat})

        # 3. Fetch pending staged transactions
        pending_transactions = con.execute(
            "SELECT st.id, st.date, st.amount_cents, st.description, a.name source_name "
            "FROM staged st JOIN batches b ON b.id=st.batch_id JOIN accounts a ON a.id=b.account_id "
            "WHERE st.status='pending' ORDER BY st.date, st.id"
        ).fetchall()

        # Fetch the latest 100 posted transactions (expense/income splits) for fallback
        latest_posted = None

        items = []
        for d in docs:
            cands = receipt_candidates(con, d) if d["status"] == "unmatched" else []
            entries = doc_entries_map[d["id"]]
            
            # Fallback for legacy database rows without join table links populated
            if not entries and d["entry_id"]:
                entry = con.execute("SELECT * FROM entries WHERE id=?", (d["entry_id"],)).fetchone()
                if entry:
                    cat = ledger.entry_category(con, d["entry_id"])
                    entries.append({"entry": entry, "category": cat})
            
            matched_ids = {e["entry"]["id"] for e in entries}
            
            if d["status"] == "unmatched":
                doc_date = d["doc_date"]
                if doc_date:
                    # Query 50 on or before doc_date
                    before_rows = con.execute(
                        "SELECT DISTINCT e.id, e.date, e.payee, abs(s.amount_cents) amount_cents, a.name acct "
                        "FROM entries e "
                        "JOIN splits s ON s.entry_id=e.id "
                        "JOIN accounts a ON a.id=s.account_id "
                        "WHERE a.type IN ('expense','income') AND e.date <= ? "
                        "ORDER BY e.date DESC, e.id DESC "
                        "LIMIT 50", (doc_date,)
                    ).fetchall()
                    # Query 50 after doc_date
                    after_rows = con.execute(
                        "SELECT DISTINCT e.id, e.date, e.payee, abs(s.amount_cents) amount_cents, a.name acct "
                        "FROM entries e "
                        "JOIN splits s ON s.entry_id=e.id "
                        "JOIN accounts a ON a.id=s.account_id "
                        "WHERE a.type IN ('expense','income') AND e.date > ? "
                        "ORDER BY e.date ASC, e.id ASC "
                        "LIMIT 50", (doc_date,)
                    ).fetchall()
                    doc_posted = list(before_rows) + list(after_rows)
                else:
                    if latest_posted is None:
                        latest_posted = con.execute(
                            "SELECT DISTINCT e.id, e.date, e.payee, abs(s.amount_cents) amount_cents, a.name acct "
                            "FROM entries e "
                            "JOIN splits s ON s.entry_id=e.id "
                            "JOIN accounts a ON a.id=s.account_id "
                            "WHERE a.type IN ('expense','income') "
                            "ORDER BY e.date DESC, e.id DESC "
                            "LIMIT 100"
                        ).fetchall()
                    doc_posted = list(latest_posted)
                
                # Ensure currently matched entries are always in the list even if old
                for me in entries:
                    eid = me["entry"]["id"]
                    if not any(p["id"] == eid for p in doc_posted):
                        row = con.execute(
                            "SELECT DISTINCT e.id, e.date, e.payee, abs(s.amount_cents) amount_cents, a.name acct "
                            "FROM entries e "
                            "JOIN splits s ON s.entry_id=e.id "
                            "JOIN accounts a ON a.id=s.account_id "
                            "WHERE e.id=? AND a.type IN ('expense','income')", (eid,)
                        ).fetchone()
                        if row:
                            doc_posted.append(row)
                
                doc_posted.sort(key=lambda x: (x["date"], x["id"]), reverse=True)
            else:
                doc_posted = []

            items.append({
                "doc": d,
                "candidates": cands,
                "entries": entries,
                "entry": entries[0]["entry"] if entries else None,
                "category": entries[0]["category"] if entries else None,
                "matched_staged_ids": doc_staged_map[d["id"]],
                "matched_entry_ids": matched_ids,
                "posted_transactions": doc_posted
            })
            
        exp_cats = con.execute("SELECT id, name FROM accounts WHERE type='expense' AND active=1 ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "receipts.html", ctx(
            request, con, items=items, exp_cats=exp_cats, pending_transactions=pending_transactions, msg=msg, err=err))
    finally:
        con.close()

@router.get("/receipts/missing", response_class=HTMLResponse)
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

@router.post("/receipts/upload")
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

@router.post("/receipts/import-amazon")
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

@router.post("/receipts/import-folder")
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

@router.post("/receipts/rematch")
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

@router.post("/receipts/recategorize")
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

@router.post("/receipts/recategorize-all")
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

@router.post("/receipts/setcategory")
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

@router.post("/receipts/match")
def receipts_match(doc_id: int = Form(...), entry_id: int = Form(...)):
    con = db.connect()
    try:
        con.execute("UPDATE documents SET status='matched', entry_id=? WHERE id=?", (entry_id, doc_id))
        con.execute("INSERT OR IGNORE INTO document_entry_links(document_id, entry_id) VALUES(?, ?)", (doc_id, entry_id))
        con.commit()
        return RedirectResponse("/receipts", status_code=303)
    finally:
        con.close()

@router.post("/receipts/update")
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

@router.post("/receipts/unmatch")
def receipts_unmatch(doc_id: int = Form(...)):
    con = db.connect()
    try:
        con.execute("UPDATE documents SET status='unmatched', entry_id=NULL WHERE id=?", (doc_id,))
        con.execute("DELETE FROM document_entry_links WHERE document_id=?", (doc_id,))
        con.commit()
        return RedirectResponse("/receipts", status_code=303)
    finally:
        con.close()

@router.post("/receipts/save-staged-matches")
async def save_staged_matches(request: Request):
    form = await request.form()
    doc_id = int(form["doc_id"])
    staged_ids = [int(x) for x in form.getlist(f"staged_{doc_id}")]
    con = db.connect()
    try:
        con.execute("DELETE FROM document_staged_links WHERE document_id=?", (doc_id,))
        for sid in staged_ids:
            con.execute("INSERT OR IGNORE INTO document_staged_links(document_id, staged_id) VALUES(?, ?)", (doc_id, sid))
        
        # Try to run AI categorization first on the newly matched staged transactions
        try:
            _categorize_from_receipts(con)
        except Exception:
            pass
        
        # Auto-post all matched pending staged transactions
        for sid in staged_ids:
            st = con.execute("SELECT * FROM staged WHERE id=? AND status='pending'", (sid,)).fetchone()
            if not st:
                continue
            
            category_id = st["category_id"]
            if not category_id:
                category_id = importer.apply_rules(con, st["description"])
            if not category_id:
                hist = importer.history_map(con)
                h = hist.get(importer.payee_key(st["description"]))
                cats = {a["name"]: a["id"] for a in con.execute(
                    "SELECT id, name FROM accounts WHERE type IN ('expense','income') AND active=1"
                ).fetchall()}
                if h in cats.values():
                    category_id = h
            if not category_id:
                uncat_row = con.execute("SELECT id FROM accounts WHERE name='Uncategorized Expense'").fetchone()
                if uncat_row:
                    category_id = uncat_row["id"]
            
            if category_id:
                _post_staged(con, sid, category_id)
        
        con.commit()
        return RedirectResponse("/receipts", status_code=303)
    finally:
        con.close()

@router.post("/receipts/save-entry-matches")
async def save_entry_matches(request: Request):
    form = await request.form()
    doc_id = int(form["doc_id"])
    entry_ids = [int(x) for x in form.getlist(f"entry_{doc_id}")]
    con = db.connect()
    try:
        # Delete existing links in document_entry_links for this document
        con.execute("DELETE FROM document_entry_links WHERE document_id=?", (doc_id,))
        for eid in entry_ids:
            con.execute("INSERT OR IGNORE INTO document_entry_links(document_id, entry_id) VALUES(?, ?)", (doc_id, eid))
        
        # Update document status and the legacy entry_id column
        if entry_ids:
            con.execute("UPDATE documents SET status='matched', entry_id=? WHERE id=?", (entry_ids[0], doc_id))
        else:
            # Check if there are still staged matches
            has_staged = con.execute("SELECT 1 FROM document_staged_links WHERE document_id=?", (doc_id,)).fetchone()
            if not has_staged:
                con.execute("UPDATE documents SET status='unmatched', entry_id=NULL WHERE id=?", (doc_id,))
        con.commit()
        return RedirectResponse("/receipts", status_code=303)
    finally:
        con.close()

@router.post("/receipts/delete")
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

@router.get("/doc/{doc_id}")
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
