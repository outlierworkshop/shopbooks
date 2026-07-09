"""The ingest→match→post engine shared by Review, Receipts, the folder watchers and bank feeds: stage rows, match receipts/invoices, and post staged rows to the ledger."""
from datetime import datetime
from pathlib import Path

import ai
import db
import importer
import invoicing
import ledger
from webutil import categories

def _link_staged_receipt(con, staged_id, entry_id):
    """Find the receipt(s) matched to a staged transaction and link them directly to the posted entry."""
    matches = staged_receipt_matches(con)
    docs = matches.get(staged_id)
    if not docs:
        return
    for doc in docs:
        con.execute(
            "UPDATE documents SET status='matched', entry_id=?, staged_id=NULL WHERE id=?",
            (entry_id, doc["id"])
        )
        con.execute(
            "INSERT OR IGNORE INTO document_entry_links(document_id, entry_id) VALUES(?, ?)",
            (doc["id"], entry_id)
        )
        con.execute(
            "DELETE FROM document_staged_links WHERE document_id=? AND staged_id=?",
            (doc["id"], staged_id)
        )

def _post_staged(con, staged_id, category_id, remember=False, splits=None):
    """Post one staged row to the ledger. Returns True if it posted (or intentionally skipped a
    booked transfer), False on a no-op / invalid input.

    Single category (the common path): pass `category_id`; books [(category, +amt), (source, -amt)].
    Split across categories: pass `splits` as a list of (category_id, magnitude_cents) with all
    magnitudes POSITIVE. This applies the row's own sign so each category leg carries the same
    direction as amount_cents, then balances them against the source. The magnitudes must add up to
    abs(amount_cents) or nothing is posted (so a mis-entered split can never book a wrong entry).
    `category_id`/`remember` are ignored in split mode (a split isn't a single-rule payee)."""
    st = con.execute(
        "SELECT st.*, b.account_id source_id FROM staged st JOIN batches b ON b.id=st.batch_id WHERE st.id=?",
        (staged_id,)).fetchone()
    if not st or st["status"] != "pending":
        return False
    total = st["amount_cents"]

    if splits:
        parts = [(int(cid), abs(int(mag))) for cid, mag in splits if cid and mag]
        if not parts or sum(m for _, m in parts) != abs(total):
            return False
        sign = -1 if total < 0 else 1
        cat_legs = [(cid, sign * mag) for cid, mag in parts]
    else:
        if not category_id:
            return False
        cat_legs = [(category_id, total)]

    # post-once for transfers: only a SINGLE own-account category is a transfer. If the very same
    # transfer is already booked from the other statement, skip instead of double-counting.
    if len(cat_legs) == 1:
        cat = con.execute("SELECT kind FROM accounts WHERE id=?", (cat_legs[0][0],)).fetchone()
        if cat and cat["kind"] in ("bank", "card") and \
                importer.find_posted_transfer(con, st["source_id"], total, st["date"]) is not None:
            con.execute("UPDATE staged SET status='skipped' WHERE id=?", (staged_id,))
            return True

    entry_id = ledger.post_entry(con, st["date"], st["description"],
                                 cat_legs + [(st["source_id"], -total)], memo=st["memo"])
    _link_staged_receipt(con, staged_id, entry_id)
    # invoice auto-mark only for a single income category (a plain deposit paying one invoice)
    if len(cat_legs) == 1:
        cat_type = con.execute("SELECT type FROM accounts WHERE id=?", (cat_legs[0][0],)).fetchone()
        if cat_type and cat_type["type"] == "income":
            matched_inv = staged_invoice_matches(con).get(staged_id)
            if matched_inv:
                con.execute("UPDATE invoices SET status='paid', paid_date=?, matched_entry_id=? WHERE id=?",
                            (st["date"], entry_id, matched_inv["id"]))
    # staged.category_id remembers the single category; a split has no single one, so store NULL.
    primary_cat = cat_legs[0][0] if len(cat_legs) == 1 else None
    con.execute("UPDATE staged SET status='posted', entry_id=?, category_id=? WHERE id=?",
                (entry_id, primary_cat, staged_id))
    if remember and len(cat_legs) == 1:
        token = st["description"].upper().split("  ")[0].strip()[:40]
        if token and not con.execute("SELECT 1 FROM rules WHERE pattern=?", (token,)).fetchone():
            con.execute("INSERT INTO rules(pattern,account_id) VALUES(?,?)", (token, cat_legs[0][0]))
    return True

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

def _watch_receipt(con, path, data):
    """One receipt file found by the watcher. Thin wrapper: _ingest_receipt already dedupes on
    content hash, extracts with AI, and auto-matches — identical to a manual upload or the existing
    'Import a whole folder' button."""
    status = _ingest_receipt(con, data, path.name)
    if status in ("matched", "imported"):
        _categorize_from_receipts(con)
    return status, ""

def _watch_statement(con, path, data):
    """One statement file found by the watcher. Mirrors do_import's single-step path (detect
    account -> parse -> categorize -> stage) exactly, minus the confirmation screen a human would
    see for a manual upload — a background watcher can't ask, so it always takes its best account
    guess and lands everything as PENDING in Review for the owner to confirm, same as always."""
    name = path.name.lower()
    if name.endswith(".csv"):
        text = data.decode("utf-8-sig", errors="replace")
        txns = importer.parse_csv(data)
    elif name.endswith(".pdf"):
        text = importer.pdf_text(path)  # the file is still on disk in the watch folder
        extracted = None
        acct_guess_id = importer.detect_account_id(con, path.name, text)
        acct_guess = con.execute("SELECT name FROM accounts WHERE id=?", (acct_guess_id,)).fetchone() \
            if acct_guess_id else None
        if ai.available(con) and acct_guess:
            extracted = (ai.extract_statement(con, text, acct_guess["name"]) if text.strip()
                        else ai.extract_statement_pdf(con, str(path), acct_guess["name"]))
        txns = []
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
    else:
        return "error", "not a .pdf or .csv"

    account_id = importer.detect_account_id(con, path.name, text)
    if not account_id:
        return "error", "couldn't tell which account this statement is for"
    if not txns:
        return "empty", "no transactions found in the file"
    dup = importer.is_duplicate_statement(con, account_id, txns, path.name)
    if dup:
        return "duplicate", dup

    cur = con.execute("INSERT INTO batches(filename,account_id) VALUES(?,?)", (path.name, account_id))
    batch_id = cur.lastrowid
    cats = {a["id"]: a["name"] for a in categories(con, ("expense", "income"))}
    ai_cats = None
    uncategorized = [t for t in txns if importer.apply_rules(con, t["description"]) is None]
    if uncategorized and ai.available(con):
        ai_cats = ai.categorize(con, [{"description": t["description"], "amount": t["amount_cents"]} for t in txns],
                                list(cats.values()))
    importer.stage_transactions(con, batch_id, txns, account_id, cats, ai_cats)
    return "imported", f"{len(txns)} transaction(s) staged in Review"

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
    """Find unmatched receipts matched to pending staged transactions (manual or auto ±7 days).
    Also supports matching a staged Amazon transaction to a combination of unmatched Amazon receipts.
    Returns a dict {staged_id: list_of_doc_rows}."""
    from collections import defaultdict
    from datetime import date as dt_date, timedelta

    # 1. Fetch manual matches (documents linked via document_staged_links)
    manual_matches = defaultdict(list)
    manually_used_doc_ids = set()
    for r in con.execute(
        "SELECT d.*, l.staged_id AS link_staged_id FROM documents d "
        "JOIN document_staged_links l ON l.document_id=d.id "
        "WHERE d.status='unmatched'"
    ).fetchall():
        manual_matches[r["link_staged_id"]].append(r)
        manually_used_doc_ids.add(r["id"])

    # 2. Fetch the remaining unmatched documents (excluding manual matches)
    docs = [
        d for d in con.execute(
            "SELECT * FROM documents WHERE status='unmatched' AND amount_cents IS NOT NULL"
        ).fetchall()
        if d["id"] not in manually_used_doc_ids
    ]

    pending = con.execute(
        "SELECT id, date, amount_cents, description FROM staged WHERE status='pending'"
    ).fetchall()
    if not pending:
        return {sid: list_of_docs for sid, list_of_docs in manual_matches.items()}

    # Initialize result dictionary with the manual matches
    result = {sid: list_of_docs for sid, list_of_docs in manual_matches.items()}

    # Only auto-match for staged rows that do not have manual matches
    pending_for_auto = [st for st in pending if st["id"] not in result]
    if not docs or not pending_for_auto:
        return result

    by_amount = defaultdict(list)
    for d in docs:
        by_amount[d["amount_cents"]].append(d)

    candidates = []  # (staged_id, doc, date_distance)
    for st in pending_for_auto:
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
    used_docs, used_staged = set(), set()
    for sid, doc, _ in candidates:
        if sid in used_staged or doc["id"] in used_docs:
            continue
        result[sid] = [doc]
        used_staged.add(sid)
        used_docs.add(doc["id"])

    # Now, find combined matches for the remaining unmatched staged rows with AMAZON in description
    remaining_staged = [
        st for st in pending_for_auto
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
                result[st["id"]] = best_subset
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
        "WHERE kind='invoice' AND status != 'void' AND matched_entry_id IS NULL AND paid_entry_id IS NULL"
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
    targets = [(sid, docs) for sid, docs in matches.items()]
    txns = []
    for sid, docs in targets:
        combined_desc = " & ".join(_receipt_context(d) for d in docs)[:2000]
        total_cents = sum(d["amount_cents"] or 0 for d in docs)
        txns.append({"description": combined_desc, "amount": total_cents})
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
    
    # Compile list of (document, entry_id) targets
    targets = []
    for d in docs:
        entry_ids = [r["entry_id"] for r in con.execute("SELECT entry_id FROM document_entry_links WHERE document_id=?", (d["id"],)).fetchall()]
        if not entry_ids and d["entry_id"]:
            entry_ids = [d["entry_id"]]
        for eid in entry_ids:
            if ledger.entry_category(con, eid):
                targets.append((d, eid))

    if not targets:
        return 0, "No matched receipts with a simple expense category to refine."
    exp = {a["name"]: a["id"] for a in con.execute(
        "SELECT id, name FROM accounts WHERE type='expense' AND active=1").fetchall()}
    txns = [{"description": _receipt_context(d), "amount": d["amount_cents"] or 0} for d, _ in targets]
    sugg = ai.categorize(con, txns, list(exp.keys()))
    if not sugg:
        return 0, "AI couldn't suggest categories - try again."
    changed = 0
    for (d, eid), name in zip(targets, sugg):
        nid = exp.get(name)
        if nid and ledger.set_entry_category(con, eid, nid) is not None:
            changed += 1
    return changed, None
