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
import duplicates
import feeds
import importer
import insights
import invoicing
import ledger
import migrate
import reconcile
import search
import recurring
import sync
import timetracking
import watcher

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


@app.on_event("startup")
def _start_watchers():
    # Deferred to the startup event (not called at import time) so _watch_statement/_watch_receipt,
    # defined later in this file, already exist by the time this runs. TestClient(app.app) used
    # without `with` (the pattern this repo's tests use) never fires this, so tests never spin up
    # a real background thread — they call watcher.run_once(...) directly instead.
    watcher.start(_watch_statement, _watch_receipt)


@app.on_event("shutdown")
def _sync_on_close():
    watcher.stop()
    sync.export_on_close()  # push this machine's books to the cloud copy on a clean exit


def ctx(request, con, **kw):
    pending = con.execute("SELECT COUNT(*) c FROM staged WHERE status='pending'").fetchone()["c"]
    unmatched = con.execute("SELECT COUNT(*) c FROM documents WHERE status='unmatched'").fetchone()["c"]
    return {"request": request, "pending_count": pending, "unmatched_count": unmatched,
            "ai_on": ai.available(con), "today": date_cls.today().isoformat(),
            "reset_suspected": backup.reset_suspected(),
            "sync_alert": sync.last_alert(),
            "business_name": db.get_setting(con, "business_name", "My Business"),
            "sales_tax_rate": db.get_setting(con, "sales_tax_rate", "0"), **kw}


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

def _briefing_facts(b):
    """Compact figures block for the optional AI day-brief narration."""
    m = ledger.fmt_cents
    L = [f"Date: {b['today']}.",
         f"Cash on hand: ${m(b['cash_on_hand'])}. Credit-card debt: ${m(b['card_debt'])}.",
         f"Receivables: ${m(b['receivables_total'])} outstanding across {b['open_invoices']} invoice(s); "
         f"${m(b['receivables_overdue'])} overdue ({b['overdue_count']} invoice(s))."]
    if b["next_tax"]:
        L.append(f"Next estimated tax: {b['next_tax']['quarter']} ~${m(b['next_tax']['amount'])} "
                 f"due {b['next_tax']['due_date']} (in {b['next_tax']['days']} days).")
    L.append(("Needs attention: " + "; ".join(a["text"] for a in b["attention"]) + ".")
             if b["attention"] else "Nothing needs attention — the books are tidy.")
    return "\n".join(L)


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    brief: str = "",
    pl_period: str = "this-quarter",
    exp_period: str = "this-quarter",
    sales_period: str = "ytd"
):
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
        brief_data = insights.briefing(con)
        narrative = ai.analyze(con, _briefing_facts(brief_data)) if (brief and ai.available(con)) else None
        trend = insights.monthly_trend(con, f"{year}-01-01", date_cls.today().isoformat())

        # --- Helper for custom period P&L comparisons ---
        def _get_comparison(con, period_str):
            from insights import parse_period, pnl_summary, _delta
            from datetime import timedelta
            today = date_cls.today()
            
            # Resolve current period
            cs, ce, clabel = parse_period(period_str, today)
            
            # Resolve base period
            p_clean = period_str.strip().lower()
            if p_clean in ("last-30-days", "30-days"):
                bs = (today - timedelta(days=60)).isoformat()
                be = (today - timedelta(days=31)).isoformat()
                blabel = "Prev 30 Days"
            elif p_clean in ("this-month-to-date", "month-to-date", "mtd", "this-month", "month"):
                from insights import _month_end
                y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
                bs = f"{y}-{m:02d}-01"
                be = _month_end(y, m).isoformat()
                blabel = f"{y}-{m:02d}"
            elif p_clean in ("this-quarter-to-date", "fq-to-date", "qtd"):
                from insights import _month_end
                cur_q = (today.month - 1) // 3 + 1
                y, prev_q = (today.year - 1, 4) if cur_q == 1 else (today.year, cur_q - 1)
                sm = 3 * (prev_q - 1) + 1
                bs = f"{y}-{sm:02d}-01"
                be = _month_end(y, sm + 2).isoformat()
                blabel = f"{y} Q{prev_q}"
            else:
                base_str = "last-year"
                if "quarter" in p_clean:
                    base_str = "last-quarter"
                elif "month" in p_clean:
                    base_str = "last-month"
                bs, be, blabel = parse_period(base_str, today)
                
            cur = pnl_summary(con, cs, ce)
            prev = pnl_summary(con, bs, be)
            return {
                "current_label": clabel,
                "base_label": blabel,
                "income": _delta(cur["income_total"], prev["income_total"]),
                "expenses": _delta(cur["expense_total"], prev["expense_total"]),
                "net": _delta(cur["net"], prev["net"]),
            }

        # --- New Dashboard Widget Calculations ---
        # 1. P&L compare
        p_l_compare = _get_comparison(con, pl_period)

        # 2. Expense breakdown
        exp_start, exp_end, exp_label = insights.parse_period(exp_period)
        exp_compare = _get_comparison(con, exp_period)
        exp_pnl = insights.pnl_summary(con, exp_start, exp_end)
        expense_breakdown = exp_pnl["expense_by_category"]

        expense_slices = []
        top_expenses = expense_breakdown[:4]
        other_amount = sum(x["amount"] for x in expense_breakdown[4:])
        colors = ['#1c7ed6', '#37b24d', '#f59f00', '#7048e8', '#ae3ec9']
        for i, item in enumerate(top_expenses):
            if item["amount"] > 0:
                expense_slices.append({
                    "name": item["name"],
                    "amount": item["amount"],
                    "color": colors[i % len(colors)]
                })
        if other_amount > 0:
            expense_slices.append({
                "name": "Other",
                "amount": other_amount,
                "color": "#737373"
            })

        # 3. Cash Flow chart (past 8 months + 3 months forecast = 12 months)
        today_dt = date_cls.today()
        historical_cash = []
        for i in range(8, 0, -1):
            m = today_dt.month - i
            y = today_dt.year
            while m <= 0:
                m += 12
                y -= 1
            start_date = f"{y}-{m:02d}-01"
            end_date = insights._month_end(y, m).isoformat()
            bal = insights.cash_position(con, end_date)["cash_on_hand"]
            pnl_m = insights.pnl_summary(con, start_date, end_date)
            label = insights._month_end(y, m).strftime("%b '%y")
            historical_cash.append({
                "label": label,
                "balance": bal,
                "inflow": pnl_m["income_total"],
                "outflow": pnl_m["expense_total"],
                "projected": False
            })

        forecast_data = insights.cash_forecast(con, horizon_days=90)
        projected_cash = []
        for m_item in forecast_data["months"]:
            parts = m_item["label"].split()
            label = f"{parts[0]} '{parts[1][2:]}"
            projected_cash.append({
                "label": label,
                "balance": m_item["end_balance"],
                "inflow": m_item["inflow"],
                "outflow": m_item["outflow"],
                "projected": True
            })

        cash_flow_chart = historical_cash + projected_cash
        balances = [x["balance"] for x in cash_flow_chart]
        cash_flow_max = max(balances) if balances else 1000000
        cash_flow_min = min(balances) if balances else 0

        # Find maximum inflow/outflow to scale the money in/out bars
        all_flows = [x["inflow"] for x in cash_flow_chart] + [x["outflow"] for x in cash_flow_chart]
        money_flow_max = max(all_flows) if all_flows else 1000000

        # 4. Paid Last 30 days
        from datetime import timedelta
        thirty_days_ago = (today_dt - timedelta(days=30)).isoformat()
        paid_direct = con.execute(
            "SELECT COALESCE(SUM(abs(s.amount_cents)), 0) FROM splits s "
            "JOIN accounts a ON a.id=s.account_id "
            "JOIN entries e ON e.id=s.entry_id "
            "JOIN invoices i ON i.paid_entry_id=e.id "
            "WHERE a.type='income' AND e.date >= ?", (thirty_days_ago,)
        ).fetchone()[0]
        paid_matched = con.execute(
            "SELECT COALESCE(SUM(abs(s.amount_cents)), 0) FROM splits s "
            "JOIN accounts a ON a.id=s.account_id "
            "JOIN entries e ON e.id=s.entry_id "
            "JOIN invoices i ON i.matched_entry_id=e.id "
            "WHERE a.type='income' AND e.date >= ? AND i.paid_entry_id IS NULL", (thirty_days_ago,)
        ).fetchone()[0]
        paid_links = con.execute(
            "SELECT COALESCE(SUM(abs(s.amount_cents)), 0) FROM splits s "
            "JOIN accounts a ON a.id=s.account_id "
            "JOIN entries e ON e.id=s.entry_id "
            "JOIN invoice_entry_links iel ON iel.entry_id=e.id "
            "WHERE a.type='income' AND e.date >= ? "
            "AND e.id NOT IN (SELECT COALESCE(paid_entry_id, 0) FROM invoices) "
            "AND e.id NOT IN (SELECT COALESCE(matched_entry_id, 0) FROM invoices)", (thirty_days_ago,)
        ).fetchone()[0]
        paid_last_30_days = (paid_direct or 0) + (paid_matched or 0) + (paid_links or 0)

        # 5. Sales calculation for the selected sales period
        sales_start, sales_end, sales_label = insights.parse_period(sales_period)
        sales_pnl = insights.pnl_summary(con, sales_start, sales_end)
        sales_total = sales_pnl["income_total"]

        # 6. Accounts Receivable aging slices
        ar_colors = {
            "current": '#37b24d',
            "1-30": '#17becf',
            "31-60": '#7048e8',
            "61-90": '#1c7ed6',
            "90+": '#d8842a'
        }
        ar_slices = []
        aging_data = invoicing.ar_aging(con)
        for bracket, val in aging_data["buckets"].items():
            if val > 0:
                ar_slices.append({
                    "name": bracket,
                    "amount": val,
                    "color": ar_colors.get(bracket, '#737373')
                })

        return templates.TemplateResponse(request, "dashboard.html", ctx(
            request, con, accounts=accounts, pnl=p, recent=recent, year=year,
            aging=aging_data, brief=brief_data, narrative=narrative,
            briefed=bool(brief), trend=trend,
            pl_period=pl_period,
            exp_period=exp_period,
            sales_period=sales_period,
            p_l_compare=p_l_compare,
            exp_compare=exp_compare,
            expense_slices=expense_slices,
            cash_flow_chart=cash_flow_chart,
            cash_flow_min=cash_flow_min,
            cash_flow_max=cash_flow_max,
            money_flow_max=money_flow_max,
            paid_last_30_days=paid_last_30_days,
            sales_total=sales_total,
            ar_slices=ar_slices))
    finally:
        con.close()


# ---------- import & review ----------

@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    con = db.connect()
    try:
        sources = con.execute("SELECT * FROM accounts WHERE kind IN ('bank','card') AND active=1 ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "import.html", ctx(
            request, con, sources=sources, feeds_connected=feeds.connected(con), error=None))
    finally:
        con.close()


@app.post("/import")
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


@app.post("/import/confirm")
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


@app.get("/review", response_class=HTMLResponse)
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


@app.post("/review")
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


# ---------- registers & entries ----------

@app.get("/register/{account_id}", response_class=HTMLResponse)
def register_view(request: Request, account_id: int, msg: str = "", err: str = ""):
    con = db.connect()
    try:
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
               customer_id: str = Form(""),
               register_account_id: int = Form(None),
               back: str = Form("/")):
    con = db.connect()
    try:
        norm_date = ledger.normalize_date(date)
        cat_id = int(category_id) if category_id and category_id.strip() else None
        new_reg_acct_id = int(account_id) if account_id and account_id.strip() else None
        job_val = int(job_id) if job_id and job_id.strip() else None
        cust_val = int(customer_id) if customer_id and customer_id.strip() else None
        
        ledger.update_entry_fields(con, entry_id, payee, memo, cat_id, job_val, norm_date, register_account_id, new_reg_acct_id, customer_id=cust_val)
        con.commit()
        return RedirectResponse(back if back.startswith("/") else "/", status_code=303)
    except ValueError as e:
        redirect_url = back if back.startswith("/") else "/"
        sep = "&" if "?" in redirect_url else "?"
        return RedirectResponse(f"{redirect_url}{sep}err={str(e)}", status_code=303)
    finally:
        con.close()


@app.post("/entry/{entry_id}/splits")
async def entry_splits_save(entry_id: int, request: Request):
    """Re-allocate a posted entry across one-or-more categories (turn a simple entry into a split,
    or edit an existing split) from the register. Anchored to register_account_id; same field shape
    as /entry/new (direction + scat[]/samt[])."""
    form = await request.form()
    con = db.connect()
    back = str(form.get("back", "/"))
    dest = back if back.startswith("/") else "/"
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
        return RedirectResponse(dest, status_code=303)
    except ValueError as e:
        from urllib.parse import quote
        sep = "&" if "?" in dest else "?"
        return RedirectResponse(f"{dest}{sep}err={quote(str(e))}", status_code=303)
    finally:
        con.close()


@app.post("/entry/delete/{entry_id}")
def entry_delete(entry_id: int, back: str = Form("/")):
    con = db.connect()
    try:
        ledger.delete_entry(con, entry_id)
        con.commit()
        return RedirectResponse(back, status_code=303)
    except ValueError as e:
        from urllib.parse import quote
        dest = back if back.startswith("/") else "/"
        sep = "&" if "?" in dest else "?"
        return RedirectResponse(f"{dest}{sep}err={quote(str(e))}", status_code=303)
    finally:
        con.close()


@app.post("/register/{account_id}/bulk-delete")
def register_bulk_delete(account_id: int, entry_ids: list[int] = Form(default=[]), back: str = Form("/")):
    """Delete several posted entries at once (e.g. a batch that posted with the wrong sign). Each
    delete goes through ledger.delete_entry, so staged/document/invoice links revert exactly as a
    single delete would; a locked period skips that entry rather than aborting the whole selection."""
    from urllib.parse import quote
    con = db.connect()
    try:
        deleted = locked = 0
        for eid in entry_ids:
            try:
                ledger.delete_entry(con, eid)
                deleted += 1
            except ledger.LockedPeriodError:
                locked += 1
        con.commit()
        dest = back if back.startswith("/") else "/"
        sep = "&" if "?" in dest else "?"
        note = f"Deleted {deleted} entry(ies)." if deleted else "Nothing deleted."
        if locked:
            note += f" {locked} skipped (in a closed period)."
        return RedirectResponse(f"{dest}{sep}msg={quote(note)}", status_code=303)
    finally:
        con.close()


@app.post("/register/{account_id}/bulk-category")
def register_bulk_category(account_id: int, entry_ids: list[int] = Form(default=[]),
                           category_id: str = Form(""), back: str = Form("/")):
    """Set the category on several posted 2-split entries at once, without touching date/payee/memo/
    job/customer. Reuses ledger.update_entry_fields with each entry's own current values so only the
    category changes; split (>2-leg) entries and locked-period entries are skipped, not aborted."""
    from urllib.parse import quote
    con = db.connect()
    try:
        if not category_id.strip():
            dest = back if back.startswith("/") else "/"
            sep = "&" if "?" in dest else "?"
            return RedirectResponse(f"{dest}{sep}err=" + quote("Pick a category first."), status_code=303)
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
        dest = back if back.startswith("/") else "/"
        sep = "&" if "?" in dest else "?"
        note = f"Updated category on {updated} entry(ies)." if updated else "Nothing updated."
        if locked:
            note += f" {locked} skipped (in a closed period)."
        return RedirectResponse(f"{dest}{sep}msg={quote(note)}", status_code=303)
    finally:
        con.close()


@app.get("/duplicates", response_class=HTMLResponse)
def duplicates_page(request: Request, msg: str = "", err: str = ""):
    con = db.connect()
    try:
        groups = duplicates.find_duplicate_groups(con)
        return templates.TemplateResponse(request, "duplicates.html", ctx(
            request, con, groups=groups, window=duplicates.WINDOW_DAYS, msg=msg, err=err))
    finally:
        con.close()


@app.post("/duplicates/delete")
def duplicates_delete(entry_ids: list[int] = Form(default=[])):
    """Delete the entries the owner checked as duplicates. Reuses ledger.delete_entry (so staged rows
    revert to pending, receipts/invoices unlink) per id; a locked-period entry is skipped, not aborted."""
    from urllib.parse import quote
    con = db.connect()
    try:
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
        return RedirectResponse("/duplicates?msg=" + quote(note), status_code=303)
    finally:
        con.close()


def _active_jobs(con):
    return con.execute("SELECT id, name FROM jobs WHERE status='active' ORDER BY created_at DESC").fetchall()


def _entry_sources(con):
    """Bank/card accounts (the money account an entry moves through), tree order."""
    return ledger.accounts_with_balances(con, kinds=("bank", "card"))


@app.get("/entry/new", response_class=HTMLResponse)
def entry_new(request: Request):
    con = db.connect()
    try:
        return templates.TemplateResponse(request, "entry.html", ctx(
            request, con, cats=categories(con), sources=_entry_sources(con),
            jobs=_active_jobs(con), error=None))
    finally:
        con.close()


@app.post("/entry/new")
async def entry_create(request: Request):
    """Manual entry with one money account (source) and one-or-more category splits.
    direction 'out' = money leaves the source (categories are debited, e.g. an expense or a card
    payment); 'in' = money arrives (categories credited, e.g. income). Category legs carry the
    magnitude with the direction's sign; the source leg balances the total. Splitting = more than
    one category row, each summing into the source."""
    form = await request.form()
    con = db.connect()

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


@app.post("/entry/{entry_id}/customer")
def entry_set_customer(entry_id: int, customer_id: str = Form(""), back: str = Form("/")):
    con = db.connect()
    try:
        ledger.set_entry_customer(con, entry_id, int(customer_id) if customer_id.strip() else None)
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


# ---------- folder watchers (issue: auto-detect from dropped files) ----------
# Callbacks passed to watcher.start()/watcher.run_once() — watcher.py owns the generic scan/dedupe
# engine and knows nothing about statements or receipts; these two functions ARE the app-specific
# processing, reusing the exact same pipelines a manual upload uses.

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


@app.get("/receipts", response_class=HTMLResponse)
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
        con.execute("INSERT OR IGNORE INTO document_entry_links(document_id, entry_id) VALUES(?, ?)", (doc_id, entry_id))
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
        con.execute("DELETE FROM document_entry_links WHERE document_id=?", (doc_id,))
        con.commit()
        return RedirectResponse("/receipts", status_code=303)
    finally:
        con.close()


@app.post("/receipts/save-staged-matches")
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


@app.post("/receipts/save-entry-matches")
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

@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = ""):
    con = db.connect()
    try:
        return templates.TemplateResponse(request, "search.html", ctx(
            request, con, q=q, results=search.run(con, q)))
    finally:
        con.close()


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


@app.post("/reconcile/finish")
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


@app.post("/reconcile/upload")
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


@app.post("/reconcile/adjust")
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


def _forecast_facts(f):
    """Compact figures block for the optional AI forecast narration."""
    m = ledger.fmt_cents
    L = [f"Cash-flow forecast as of {f['today']} ({f['horizon_days']} days).",
         f"Starting cash: ${m(f['starting_cash'])}. Projected cash at the end: ${m(f['projected_end'])}.",
         f"Estimated monthly burn: ${m(f['avg_monthly_expense'])} "
         f"(of which ${m(f['recurring_monthly_expense'])} is known recurring bills).",
         f"Expected invoice collections over the horizon: ${m(f['expected_inflow_total'])}; "
         f"recurring income: ${m(f['recurring_income_total'])}."]
    L.append(f"Projected low point: ${m(f['low_point']['balance'])} around {f['low_point']['label']}."
             + (" Cash is projected to go NEGATIVE." if f["goes_negative"] else ""))
    L.append("By month: " + "; ".join(
        f"{mo['label']} in ${m(mo['inflow'])} / out ${m(mo['outflow'])} -> ${m(mo['end_balance'])}" for mo in f["months"]))
    return "\n".join(L)


@app.get("/forecast", response_class=HTMLResponse)
def forecast_page(request: Request, horizon: int = 90, explain: str = ""):
    con = db.connect()
    try:
        horizon = horizon if horizon in (30, 60, 90, 180) else 90
        f = insights.cash_forecast(con, horizon_days=horizon)
        narrative = ai.analyze(con, _forecast_facts(f)) if (explain and ai.available(con)) else None
        return templates.TemplateResponse(request, "forecast.html", ctx(
            request, con, f=f, horizon=horizon, narrative=narrative, explained=bool(explain)))
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
        "JOIN customers c ON c.id=i.customer_id WHERE i.kind IN ('invoice', 'credit_memo') ORDER BY i.id DESC").fetchall()
    today = date_cls.today().isoformat()
    out = []
    for r in rows:
        total = invoicing.invoice_total(con, r["id"])
        pay_total = invoicing.invoice_payments_total(con, r["id"])
        applied_credits = invoicing.invoice_applied_credits(con, r["id"]) if r["kind"] == "invoice" else invoicing.invoice_credit_sources_total(con, r["id"])
        outstanding_balance = invoicing.invoice_outstanding_balance(con, r["id"])
        overdue = r["status"] in ("sent", "partially_paid") and r["due_date"] < today
        out.append({**dict(r), "total": total, "payments_total": pay_total, "applied_credits": applied_credits, "outstanding_balance": outstanding_balance, "overdue": overdue})
    return out


@app.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, msg: str = "", err: str = ""):
    con = db.connect()
    try:
        customers_raw = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        customers = []
        for c in customers_raw:
            # Query all open/partially paid invoices & credit memos for this customer
            invs = con.execute(
                "SELECT id FROM invoices WHERE customer_id=? AND kind IN ('invoice', 'credit_memo') AND status IN ('sent', 'partially_paid')",
                (c["id"],)
            ).fetchall()
            outstanding = 0
            for r in invs:
                outstanding += invoicing.invoice_outstanding_balance(con, r["id"])
            credit = invoicing.customer_available_credit(con, c["id"])
            customers.append({**dict(c), "outstanding": outstanding, "credit": credit})
            
        return templates.TemplateResponse(request, "invoices.html", ctx(
            request, con, invoices=_invoice_rows(con), customers=customers, msg=msg, err=err,
            aging=invoicing.ar_aging(con), email_on=invoicing.email_configured(con)))
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
                 phone: str = Form(""), notes: str = Form("")):
    con = db.connect()
    try:
        con.execute("INSERT INTO customers(name,email,address,phone,notes) VALUES(?,?,?,?,?)",
                    (name.strip(), email.strip(), address.strip(), phone.strip(), notes.strip()))
        con.commit()
        return RedirectResponse("/customers", status_code=303)
    finally:
        con.close()


@app.post("/customers/update")
def customer_update(customer_id: int = Form(...), name: str = Form(...), email: str = Form(""),
                    address: str = Form(""), phone: str = Form(""), notes: str = Form("")):
    con = db.connect()
    try:
        con.execute("UPDATE customers SET name=?, email=?, address=?, phone=?, notes=? WHERE id=?",
                    (name.strip(), email.strip(), address.strip(), phone.strip(), notes.strip(), customer_id))
        con.commit()
        return RedirectResponse(f"/customers/{customer_id}", status_code=303)
    finally:
        con.close()


# ---------- customer pages and actions ----------

@app.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request):
    con = db.connect()
    try:
        err = request.query_params.get("err", "")
        msg = request.query_params.get("msg", "")
        
        # Calculate summary metrics
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        summary = []
        ar_total = 0
        credits_total = 0
        
        for c in customers:
            invs = con.execute("SELECT id FROM invoices WHERE customer_id=? AND kind='invoice' AND status!='void'", (c["id"],)).fetchall()
            total_sales = 0
            total_open = 0
            for inv in invs:
                total_sales += invoicing.invoice_total(con, inv["id"])
                total_open += invoicing.invoice_outstanding_balance(con, inv["id"])
            
            credit = invoicing.customer_available_credit(con, c["id"])
            ar_total += total_open
            credits_total += credit
            
            summary.append({
                "id": c["id"],
                "name": c["name"],
                "email": c["email"],
                "phone": c["phone"],
                "address": c["address"],
                "notes": c["notes"],
                "total_sales": total_sales,
                "total_open": total_open,
                "credit": credit
            })
            
        return templates.TemplateResponse(request, "customers.html", ctx(
            request, con,
            customers=summary,
            ar_total=ar_total,
            credits_total=credits_total,
            err=err,
            msg=msg
        ))
    finally:
        con.close()


@app.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_detail(customer_id: int, request: Request):
    con = db.connect()
    try:
        err = request.query_params.get("err", "")
        msg = request.query_params.get("msg", "")
        
        customer = con.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not customer:
            return RedirectResponse("/customers?err=Customer+not+found", status_code=303)
            
        # KPI calculations
        invs = con.execute("SELECT id FROM invoices WHERE customer_id=? AND kind='invoice' AND status!='void'", (customer_id,)).fetchall()
        total_sales = 0
        total_open = 0
        for inv in invs:
            total_sales += invoicing.invoice_total(con, inv["id"])
            total_open += invoicing.invoice_outstanding_balance(con, inv["id"])
            
        credits_avail = invoicing.customer_available_credit(con, customer_id)
        
        # Document files (tax forms, etc.)
        files = con.execute("SELECT * FROM customer_files WHERE customer_id=? ORDER BY uploaded_at DESC", (customer_id,)).fetchall()
        
        # Chronological notes
        notes = con.execute("SELECT * FROM customer_notes WHERE customer_id=? ORDER BY created_at DESC", (customer_id,)).fetchall()
        
        # Invoice and estimates history
        history = con.execute("SELECT * FROM invoices WHERE customer_id=? ORDER BY date DESC, number DESC", (customer_id,)).fetchall()
        
        history_summary = []
        for h in history:
            total = invoicing.invoice_total(con, h["id"])
            open_bal = invoicing.invoice_outstanding_balance(con, h["id"])
            history_summary.append({
                "id": h["id"],
                "number": h["number"],
                "date": h["date"],
                "due_date": h["due_date"],
                "kind": h["kind"],
                "status": h["status"],
                "total": total,
                "outstanding": open_bal
            })
            
        return templates.TemplateResponse(request, "customer_detail.html", ctx(
            request, con,
            customer=customer,
            total_sales=total_sales,
            total_open=total_open,
            credits_avail=credits_avail,
            files=files,
            notes=notes,
            history=history_summary,
            err=err,
            msg=msg
        ))
    finally:
        con.close()


@app.post("/customers/{customer_id}/upload-file")
async def customer_upload_file(customer_id: int, file: UploadFile = File(...), kind: str = Form("tax_form")):
    con = db.connect()
    try:
        customer = con.execute("SELECT name FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not customer:
            return RedirectResponse("/customers?err=Customer+not+found", status_code=303)
            
        cust_dir = db.DOCS / "customer_files"
        cust_dir.mkdir(parents=True, exist_ok=True)
        
        safe_name = "".join(c for c in file.filename if c.isalnum() or c in (".", "-", "_")).strip()
        if not safe_name:
            safe_name = "file"
        
        import uuid
        unique_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
        dest_path = cust_dir / unique_name
        
        with open(dest_path, "wb") as f:
            f.write(await file.read())
            
        con.execute(
            "INSERT INTO customer_files(customer_id, filename, path, kind) VALUES(?,?,?,?)",
            (customer_id, file.filename, str(dest_path.resolve()), kind)
        )
        con.commit()
        
        return RedirectResponse(f"/customers/{customer_id}?msg=File+uploaded+successfully", status_code=303)
    finally:
        con.close()


@app.get("/customers/file/{file_id}")
def customer_download_file(file_id: int):
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM customer_files WHERE id=?", (file_id,)).fetchone()
        if not row:
            return RedirectResponse("/customers?err=File+not+found", status_code=303)
            
        path = row["path"]
        if not os.path.exists(path):
            return PlainTextResponse("File does not exist on disk.", status_code=404)
            
        ext = os.path.splitext(path)[1].lower()
        media = _INLINE_MEDIA.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        
        return FileResponse(path, media_type=media, filename=row["filename"], content_disposition_type="inline")
    finally:
        con.close()


@app.post("/customers/file/{file_id}/delete")
def customer_delete_file(file_id: int):
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM customer_files WHERE id=?", (file_id,)).fetchone()
        if not row:
            return RedirectResponse("/customers?err=File+not+found", status_code=303)
            
        if os.path.exists(row["path"]):
            os.remove(row["path"])
            
        con.execute("DELETE FROM customer_files WHERE id=?", (file_id,))
        con.commit()
        
        return RedirectResponse(f"/customers/{row['customer_id']}?msg=File+deleted", status_code=303)
    finally:
        con.close()


@app.post("/customers/{customer_id}/add-note")
def customer_add_note(customer_id: int, note: str = Form(...)):
    con = db.connect()
    try:
        if not note.strip():
            return RedirectResponse(f"/customers/{customer_id}?err=Note+cannot+be+empty", status_code=303)
            
        con.execute("INSERT INTO customer_notes(customer_id, note) VALUES(?,?)", (customer_id, note.strip()))
        con.commit()
        return RedirectResponse(f"/customers/{customer_id}?msg=Note+added", status_code=303)
    finally:
        con.close()


@app.post("/customers/note/{note_id}/delete")
def customer_delete_note(note_id: int):
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM customer_notes WHERE id=?", (note_id,)).fetchone()
        if not row:
            return RedirectResponse("/customers?err=Note+not+found", status_code=303)
            
        con.execute("DELETE FROM customer_notes WHERE id=?", (note_id,))
        con.commit()
        return RedirectResponse(f"/customers/{row['customer_id']}?msg=Note+deleted", status_code=303)
    finally:
        con.close()


@app.get("/customers/{customer_id}/report", response_class=HTMLResponse)
def customer_report(customer_id: int, request: Request):
    con = db.connect()
    try:
        customer = con.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not customer:
            return RedirectResponse("/customers?err=Customer+not+found", status_code=303)
            
        invoices = con.execute(
            "SELECT id, number, date, kind, status FROM invoices "
            "WHERE customer_id=? AND status!='void' ORDER BY date, id", (customer_id,)
        ).fetchall()
        
        txns = []
        for inv in invoices:
            tot = invoicing.invoice_total(con, inv["id"])
            if inv["kind"] == "credit_memo":
                txns.append({
                    "date": inv["date"],
                    "number": inv["number"],
                    "type": "Credit Memo",
                    "debit_cents": 0,
                    "credit_cents": abs(tot),
                    "id": inv["id"]
                })
            else:
                txns.append({
                    "date": inv["date"],
                    "number": inv["number"],
                    "type": "Invoice",
                    "debit_cents": tot,
                    "credit_cents": 0,
                    "id": inv["id"]
                })
                
                # All payments against this invoice — including multi-payment invoices tracked via
                # invoice_entry_links (invoicing.invoice_payment_entries mirrors invoice_payments_total,
                # so the statement reconciles with the invoice's outstanding balance).
                for p in invoicing.invoice_payment_entries(con, inv["id"]):
                    txns.append({
                        "date": p["date"],
                        "number": f"PMT-{p['entry_id']}",
                        "type": "Payment",
                        "debit_cents": 0,
                        "credit_cents": p["amount_cents"],
                        "id": p["entry_id"]
                    })
                                
        txns.sort(key=lambda x: (x["date"], x["type"] != "Invoice"))
        
        running_bal = 0
        ledger_rows = []
        total_invoiced = 0
        total_payments = 0
        
        for tx in txns:
            running_bal += tx["debit_cents"] - tx["credit_cents"]
            total_invoiced += tx["debit_cents"]
            total_payments += tx["credit_cents"]
            ledger_rows.append({
                "date": tx["date"],
                "number": tx["number"],
                "type": tx["type"],
                "debit": tx["debit_cents"],
                "credit": tx["credit_cents"],
                "balance": running_bal
            })
            
        business_address = db.get_setting(con, "business_address", "")
        business_email = db.get_setting(con, "business_email", "")
        business_phone = db.get_setting(con, "business_phone", "")
        return templates.TemplateResponse(request, "customer_report.html", ctx(
            request, con,
            customer=customer,
            ledger=ledger_rows,
            total_invoiced=total_invoiced,
            total_payments=total_payments,
            ending_balance=running_bal,
            business_address=business_address,
            business_email=business_email,
            business_phone=business_phone
        ))
    finally:
        con.close()


# ---------- products & services catalog ----------

@app.get("/items", response_class=HTMLResponse)
def items_page(request: Request):
    con = db.connect()
    try:
        err = request.query_params.get("err", "")
        msg = request.query_params.get("msg", "")
        
        items = con.execute(
            "SELECT i.*, a.name as account_name FROM items i "
            "LEFT JOIN accounts a ON a.id=i.income_account_id "
            "ORDER BY i.name"
        ).fetchall()
        
        income_accounts = categories(con, types=("income",))
        
        total_items = sum(1 for it in items if it["active"])
        mapped_items = sum(1 for it in items if it["income_account_id"] and it["active"])
        
        return templates.TemplateResponse(request, "items.html", ctx(
            request, con,
            items=items,
            income_accounts=income_accounts,
            total_items=total_items,
            mapped_items=mapped_items,
            err=err,
            msg=msg
        ))
    finally:
        con.close()


@app.post("/items")
def items_create(name: str = Form(...), sku: str = Form(""), description: str = Form(""),
                 unit_price: str = Form("0.00"), income_account_id: str = Form(""),
                 taxable: str = Form("")):
    con = db.connect()
    try:
        if not name.strip():
            return RedirectResponse("/items?err=Name is required", status_code=303)

        unit_cents = 0
        if unit_price.strip():
            try:
                unit_cents = ledger.parse_amount_to_cents(unit_price)
            except ValueError:
                return RedirectResponse("/items?err=Invalid price format", status_code=303)

        acct_id = int(income_account_id) if income_account_id.strip() else None

        con.execute(
            "INSERT INTO items(name, sku, description, unit_cents, income_account_id, taxable) VALUES(?,?,?,?,?,?)",
            (name.strip(), sku.strip() or None, description.strip(), unit_cents, acct_id, 1 if taxable else 0)
        )
        con.commit()
        return RedirectResponse("/items?msg=Product/service added successfully", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/items?err={str(e)}", status_code=303)
    finally:
        con.close()


@app.post("/items/update")
def items_update(item_id: int = Form(...), name: str = Form(...), sku: str = Form(""),
                 description: str = Form(""), unit_price: str = Form("0.00"),
                 income_account_id: str = Form(""), active: str = Form("0"), taxable: str = Form("")):
    con = db.connect()
    try:
        if not name.strip():
            return RedirectResponse("/items?err=Name is required", status_code=303)

        unit_cents = 0
        if unit_price.strip():
            try:
                unit_cents = ledger.parse_amount_to_cents(unit_price)
            except ValueError:
                return RedirectResponse("/items?err=Invalid price format", status_code=303)

        acct_id = int(income_account_id) if income_account_id.strip() else None
        is_active = 1 if active == "1" else 0

        con.execute(
            "UPDATE items SET name=?, sku=?, description=?, unit_cents=?, income_account_id=?, active=?, taxable=? WHERE id=?",
            (name.strip(), sku.strip() or None, description.strip(), unit_cents, acct_id, is_active, 1 if taxable else 0, item_id)
        )
        con.commit()
        return RedirectResponse("/items?msg=Product/service updated successfully", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/items?err={str(e)}", status_code=303)
    finally:
        con.close()


@app.post("/items/import-qbo")
async def items_import_qbo(file: UploadFile = File(...)):
    con = db.connect()
    try:
        contents = await file.read()
        parsed = migrate.parse_items(con, contents)
        created, updated, skipped = migrate.import_items(con, parsed)
        con.commit()
        return RedirectResponse(
            f"/items?msg=Import complete: {created} items created, {updated} updated",
            status_code=303
        )
    except Exception as e:
        return RedirectResponse(f"/items?err={str(e)}", status_code=303)
    finally:
        con.close()


@app.get("/invoices/new", response_class=HTMLResponse)
def invoice_new(request: Request):
    con = db.connect()
    try:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        kind = request.query_params.get("kind", "invoice")
        standard_items = con.execute("SELECT * FROM items WHERE active=1 ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "invoice_new.html", ctx(
            request, con, customers=customers, kind=kind, standard_items=standard_items, error=None))
    finally:
        con.close()


def _active_items(con):
    """Active catalog products/services for invoice/estimate line dropdowns."""
    return con.execute("SELECT * FROM items WHERE active=1 ORDER BY name").fetchall()


def _parse_line_items(form):
    """Invoice/estimate line rows from the form as (desc, qty, unit_cents, item_id, taxable) tuples,
    skipping blank-description rows. item_id links a line back to the catalog item it was filled from;
    taxable rides a per-row hidden field (a checkbox alone wouldn't submit for unchecked rows and
    would misalign). Both lists are aligned row-for-row with the descriptions."""
    descs = form.getlist("item_desc")
    qtys = form.getlist("item_qty")
    prices = form.getlist("item_price")
    item_ids = form.getlist("item_id")
    taxables = form.getlist("item_taxable")
    if len(item_ids) != len(descs):        # no catalog on the page → no per-row item select posted
        item_ids = [""] * len(descs)
    if len(taxables) != len(descs):
        taxables = ["0"] * len(descs)
    out = []
    for d, q, p, iid, tx in zip(descs, qtys, prices, item_ids, taxables):
        if not d.strip():
            continue
        out.append((d.strip(), float(q or 1), ledger.parse_amount_to_cents(p),
                    int(iid) if (iid and iid.strip()) else None,
                    1 if str(tx).strip() in ("1", "on", "true", "True") else 0))
    return out


def _insert_line_items(con, invoice_id, items):
    """Insert parsed (desc, qty, unit_cents, item_id, taxable) rows for an invoice/estimate."""
    for d, q, u, iid, tx in items:
        con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,item_id,taxable) VALUES(?,?,?,?,?,?)",
                    (invoice_id, d, q, u, iid, tx))


@app.post("/invoices/new")
async def invoice_create(request: Request):
    form = await request.form()
    con = db.connect()
    try:
        customer_id = int(form["customer_id"])
        inv_date = ledger.normalize_date(form["date"])
        due_date = ledger.normalize_date(form["due_date"])
        kind = form.get("kind", "invoice")
        items = _parse_line_items(form)
        if not items:
            raise ValueError("Add at least one line item.")
        
        if kind == "credit_memo":
            number = invoicing.next_credit_memo_number(con)
        else:
            number = invoicing.next_number(con)
            
        cur = con.execute(
            "INSERT INTO invoices(number,customer_id,date,due_date,memo,kind) VALUES(?,?,?,?,?,?)",
            (number, customer_id, inv_date, due_date, form.get("memo", "").strip(), kind))
        inv_id = cur.lastrowid
        _insert_line_items(con, inv_id, items)
        con.commit()
        return RedirectResponse(f"/invoices/{inv_id}", status_code=303)
    except (ValueError, KeyError) as e:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        kind = form.get("kind", "invoice")
        return templates.TemplateResponse(request, "invoice_new.html", ctx(
            request, con, customers=customers, kind=kind, standard_items=_active_items(con), error=str(e)))
    finally:
        con.close()


@app.get("/invoices/{invoice_id}/edit", response_class=HTMLResponse)
def invoice_edit(request: Request, invoice_id: int):
    con = db.connect()
    try:
        inv, items, total = invoicing.get_invoice(con, invoice_id)
        if not inv:
            return RedirectResponse("/invoices", status_code=303)
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        standard_items = con.execute("SELECT * FROM items WHERE active=1 ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "invoice_edit.html", ctx(
            request, con, inv=inv, items=items, customers=customers, standard_items=standard_items, error=None))
    finally:
        con.close()


@app.post("/invoices/{invoice_id}/edit")
async def invoice_update(request: Request, invoice_id: int):
    form = await request.form()
    con = db.connect()
    try:
        inv, _, _ = invoicing.get_invoice(con, invoice_id)
        if not inv:
            return RedirectResponse("/invoices", status_code=303)
        customer_id = int(form["customer_id"])
        inv_date = ledger.normalize_date(form["date"])
        due_date = ledger.normalize_date(form["due_date"])
        items = _parse_line_items(form)
        if not items:
            raise ValueError("Add at least one line item.")
        
        con.execute(
            "UPDATE invoices SET customer_id=?, date=?, due_date=?, memo=? WHERE id=?",
            (customer_id, inv_date, due_date, form.get("memo", "").strip(), invoice_id))
        con.execute("DELETE FROM invoice_items WHERE invoice_id=?", (invoice_id,))
        _insert_line_items(con, invoice_id, items)

        _update_document_status(con, invoice_id)
            
        _update_entry_customers_for_invoice(con, invoice_id)
        _cleanup_entry_customers(con)
        con.commit()
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    except (ValueError, KeyError) as e:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        inv, items, _ = invoicing.get_invoice(con, invoice_id)
        return templates.TemplateResponse(request, "invoice_edit.html", ctx(
            request, con, inv=inv, items=items, customers=customers, standard_items=_active_items(con), error=str(e)))
    finally:
        con.close()


def _update_document_status(con, invoice_id):
    inv = con.execute("SELECT kind, status, paid_entry_id FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv or inv["status"] == "void":
        return
    
    if inv["kind"] == "credit_memo":
        total = abs(invoicing.invoice_total(con, invoice_id))
        applied = con.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM credit_applications WHERE credit_invoice_id=?", (invoice_id,)).fetchone()[0]
        if applied >= total:
            status = "paid"
        elif applied > 0:
            status = "partially_paid"
        else:
            status = "sent"
        con.execute("UPDATE invoices SET status=? WHERE id=?", (status, invoice_id))
        
    elif inv["kind"] == "invoice":
        total = invoicing.invoice_total(con, invoice_id)
        payments = invoicing.invoice_payments_total(con, invoice_id)
        applied = con.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM credit_applications WHERE invoice_id=?", (invoice_id,)).fetchone()[0]
        
        total_credited = payments + applied
        if total_credited >= total:
            status = "paid"
            dates = []
            if inv["paid_entry_id"]:
                dates.append(con.execute("SELECT date FROM entries WHERE id=?", (inv["paid_entry_id"],)).fetchone()["date"])
            eids = [r["entry_id"] for r in con.execute("SELECT entry_id FROM invoice_entry_links WHERE invoice_id=?", (invoice_id,)).fetchall()]
            for eid in eids:
                dates.append(con.execute("SELECT date FROM entries WHERE id=?", (eid,)).fetchone()["date"])
            credit_dates = con.execute("SELECT date FROM credit_applications WHERE invoice_id=?", (invoice_id,)).fetchall()
            for cd in credit_dates:
                dates.append(cd["date"])
            
            paid_date = max(dates) if dates else date_cls.today().isoformat()
            con.execute("UPDATE invoices SET status=?, paid_date=? WHERE id=?", (status, paid_date, invoice_id))
        elif total_credited > 0:
            con.execute("UPDATE invoices SET status='partially_paid', paid_date=NULL WHERE id=?", (invoice_id,))
        else:
            con.execute("UPDATE invoices SET status='sent', paid_date=NULL WHERE id=?", (invoice_id,))


def get_available_credits_for_customer(con, customer_id):
    return invoicing.available_credits_for_customer(con, customer_id)


def _apply_credit_core(con, invoice_id, credit_invoice_id, amount_cents, d):
    """Apply `amount_cents` of credit from credit_invoice_id onto invoice_id. Caps at BOTH the
    source's available credit and the target invoice's remaining balance, so no credit is wasted.
    Updates both documents' statuses. Returns the amount actually applied. Raises ValueError."""
    inv, _, _ = invoicing.get_invoice(con, invoice_id)
    if not inv or inv["kind"] != "invoice" or inv["status"] == "void":
        raise ValueError("Invalid target invoice")
    credit_inv, _, _ = invoicing.get_invoice(con, credit_invoice_id)
    if not credit_inv or credit_inv["customer_id"] != inv["customer_id"]:
        raise ValueError("The credit must belong to the same customer")
    applied = con.execute("SELECT COALESCE(SUM(amount_cents),0) FROM credit_applications "
                          "WHERE credit_invoice_id=?", (credit_invoice_id,)).fetchone()[0]
    if credit_inv["kind"] == "credit_memo":
        avail = abs(invoicing.invoice_total(con, credit_invoice_id)) - applied
    else:
        avail = invoicing.invoice_payments_total(con, credit_invoice_id) - \
            invoicing.invoice_total(con, credit_invoice_id) - applied
    target_outstanding = invoicing.invoice_outstanding_balance(con, invoice_id)
    if target_outstanding <= 0:
        raise ValueError("This invoice has no remaining balance")
    amount_cents = min(amount_cents, avail, target_outstanding)
    if amount_cents <= 0:
        raise ValueError("No available credit on the source")
    con.execute("INSERT INTO credit_applications(credit_invoice_id, invoice_id, amount_cents, date) VALUES(?,?,?,?)",
                (credit_invoice_id, invoice_id, amount_cents, d))
    _update_document_status(con, invoice_id)
    _update_document_status(con, credit_invoice_id)
    return amount_cents


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


def _update_entry_customers_for_invoice(con, invoice_id):
    # Find the customer_id for this invoice
    inv = con.execute("SELECT customer_id FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        return
    cust_id = inv["customer_id"]
    
    # Find all entries currently linked to this invoice
    eids = [r["entry_id"] for r in con.execute("SELECT entry_id FROM invoice_entry_links WHERE invoice_id=?", (invoice_id,)).fetchall()]
    row = con.execute("SELECT paid_entry_id, matched_entry_id FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if row:
        if row["paid_entry_id"]:
            eids.append(row["paid_entry_id"])
        if row["matched_entry_id"]:
            eids.append(row["matched_entry_id"])
            
    for eid in set(eids):
        con.execute("UPDATE entries SET customer_id=? WHERE id=?", (cust_id, eid))


def _cleanup_entry_customers(con):
    # Set customer_id to NULL for any entries that are no longer linked to any invoices
    con.execute("""
    UPDATE entries SET customer_id = NULL
    WHERE customer_id IS NOT NULL
      AND id NOT IN (SELECT entry_id FROM invoice_entry_links)
      AND id NOT IN (SELECT paid_entry_id FROM invoices WHERE paid_entry_id IS NOT NULL)
      AND id NOT IN (SELECT matched_entry_id FROM invoices WHERE matched_entry_id IS NOT NULL)
    """)


def _match_invoice_to_entry(con, invoice_id, entry_id):
    """Link an invoice to an existing deposit entry (records-only: no ledger posting)."""
    e = con.execute("SELECT date FROM entries WHERE id=?", (entry_id,)).fetchone()
    if not e:
        return False
    con.execute("INSERT OR IGNORE INTO invoice_entry_links(invoice_id, entry_id) VALUES(?, ?)",
                (invoice_id, entry_id))
                
    _, _, total = invoicing.get_invoice(con, invoice_id)
    payments_total = invoicing.invoice_payments_total(con, invoice_id)
    status = 'paid' if payments_total >= total else 'partially_paid'
    
    con.execute("UPDATE invoices SET status=?, paid_date=?, matched_entry_id=? WHERE id=?",
                (status, e["date"], entry_id, invoice_id))
    _update_entry_customers_for_invoice(con, invoice_id)
    return True


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_view(request: Request, invoice_id: int, msg: str = "", err: str = ""):
    con = db.connect()
    try:
        inv, items, total = invoicing.get_invoice(con, invoice_id)
        if not inv:
            return RedirectResponse("/invoices", status_code=303)
        if inv["kind"] == "estimate":
            return RedirectResponse(f"/estimates/{invoice_id}", status_code=303)
        banks = con.execute("SELECT * FROM accounts WHERE kind='bank' AND active=1").fetchall()
        income = con.execute("SELECT * FROM accounts WHERE type='income' AND active=1 ORDER BY name").fetchall()
        
        # Get all matched entries for this invoice via invoice_entry_links
        matched_entries = con.execute(
            "SELECT DISTINCT e.id, e.date, e.payee, abs(s.amount_cents) amount_cents, a.name acct "
            "FROM entries e "
            "JOIN splits s ON s.entry_id=e.id "
            "JOIN accounts a ON a.id=s.account_id "
            "JOIN invoice_entry_links iel ON iel.entry_id=e.id "
            "WHERE iel.invoice_id=? AND a.type='income'", (invoice_id,)
        ).fetchall()
        
        # Support fallback legacy matched_entry_id
        if not matched_entries and inv["matched_entry_id"]:
            row = con.execute(
                "SELECT DISTINCT e.id, e.date, e.payee, abs(s.amount_cents) amount_cents, a.name acct "
                "FROM entries e "
                "JOIN splits s ON s.entry_id=e.id "
                "JOIN accounts a ON a.id=s.account_id "
                "WHERE e.id=? AND a.type='income'", (inv["matched_entry_id"],)
            ).fetchone()
            if row:
                matched_entries = [row]
                
        matched_entry_ids = {m["id"] for m in matched_entries}
        
        # Query available deposits
        available_deposits = con.execute(
            "SELECT DISTINCT e.id, e.date, e.payee, abs(s.amount_cents) amount_cents, a.name acct "
            "FROM entries e "
            "JOIN splits s ON s.entry_id=e.id "
            "JOIN accounts a ON a.id=s.account_id "
            "WHERE a.type='income' AND s.amount_cents < 0 "
            "AND e.id NOT IN (SELECT entry_id FROM invoice_entry_links WHERE invoice_id != ?) "
            "AND e.id NOT IN (SELECT matched_entry_id FROM invoices WHERE matched_entry_id IS NOT NULL AND id != ?) "
            "AND e.id NOT IN (SELECT paid_entry_id FROM invoices WHERE paid_entry_id IS NOT NULL AND id != ?) "
            "ORDER BY e.date DESC, e.id DESC "
            "LIMIT 100", (invoice_id, invoice_id, invoice_id)
        ).fetchall()
        available_deposits = list(available_deposits)
        
        # Ensure currently matched entries are always in the list even if old
        for m in matched_entries:
            if not any(a["id"] == m["id"] for a in available_deposits):
                available_deposits.append(m)
        available_deposits.sort(key=lambda x: (x["date"], x["id"]), reverse=True)

        candidates = None
        matched = matched_entries[0] if matched_entries else None
        if not inv["paid_entry_id"] and inv["status"] != "void" and not matched_entries:
            # sent invoices, and QBO-imported 'paid' ones not yet linked to a deposit
            candidates = invoice_deposit_candidates(con, inv, total)
            
        payments_total = invoicing.invoice_payments_total(con, invoice_id)
        
        applied_credits_list = con.execute(
            "SELECT ca.id, ca.amount_cents, ca.date, i.number, i.id credit_invoice_id FROM credit_applications ca "
            "JOIN invoices i ON i.id=ca.credit_invoice_id "
            "WHERE ca.invoice_id=?", (invoice_id,)
        ).fetchall()
        applied_credits_total = invoicing.invoice_applied_credits(con, invoice_id)

        credit_applications_list = con.execute(
            "SELECT ca.id, ca.amount_cents, ca.date, i.number, i.id invoice_id FROM credit_applications ca "
            "JOIN invoices i ON i.id=ca.invoice_id "
            "WHERE ca.credit_invoice_id=?", (invoice_id,)
        ).fetchall()
        credit_applications_total = invoicing.invoice_credit_sources_total(con, invoice_id)

        outstanding_balance = invoicing.invoice_outstanding_balance(con, invoice_id)

        available_credits = []
        if inv["kind"] == "invoice" and outstanding_balance > 0:
            available_credits = get_available_credits_for_customer(con, inv["customer_id"])

        remaining_credit = 0
        if inv["kind"] == "credit_memo":
            remaining_credit = abs(outstanding_balance)
        elif inv["kind"] == "invoice":
            remaining_credit = max(0, payments_total - total - credit_applications_total)

        # For a credit memo with credit left, the open invoices it can be applied to (feature #2)
        applicable_invoices = []
        if inv["kind"] == "credit_memo" and remaining_credit > 0:
            for r in con.execute("SELECT id, number, due_date FROM invoices WHERE customer_id=? AND kind='invoice' "
                                 "AND status IN ('sent','partially_paid')", (inv["customer_id"],)).fetchall():
                ob = invoicing.invoice_outstanding_balance(con, r["id"])
                if ob > 0:
                    applicable_invoices.append({"id": r["id"], "number": r["number"],
                                                "due_date": r["due_date"], "outstanding": ob})

        return templates.TemplateResponse(request, "invoice_view.html", ctx(
            request, con, inv=inv, items=items, total=total, banks=banks, income=income,
            subtotal=invoicing.invoice_subtotal(con, invoice_id), tax=invoicing.invoice_tax(con, invoice_id),
            candidates=candidates, matched=matched, matched_entries=matched_entries,
            matched_entry_ids=matched_entry_ids, available_deposits=available_deposits,
            payments_total=payments_total, outstanding_balance=outstanding_balance,
            applied_credits_list=applied_credits_list, applied_credits_total=applied_credits_total,
            credit_applications_list=credit_applications_list, credit_applications_total=credit_applications_total,
            available_credits=available_credits, remaining_credit=remaining_credit,
            applicable_invoices=applicable_invoices,
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
        con.execute("DELETE FROM invoice_entry_links WHERE invoice_id=?", (invoice_id,))
        _cleanup_entry_customers(con)
        con.commit()
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    finally:
        con.close()


@app.post("/invoices/{invoice_id}/save-matches")
async def invoice_save_matches(invoice_id: int, request: Request):
    form = await request.form()
    entry_ids = [int(x) for x in form.getlist("entry_ids")]
    con = db.connect()
    try:
        con.execute("DELETE FROM invoice_entry_links WHERE invoice_id=?", (invoice_id,))
        for eid in entry_ids:
            con.execute("INSERT OR IGNORE INTO invoice_entry_links(invoice_id, entry_id) VALUES(?, ?)", (invoice_id, eid))
        
        if entry_ids:
            dates = []
            for eid in entry_ids:
                e = con.execute("SELECT date FROM entries WHERE id=?", (eid,)).fetchone()
                if e:
                    dates.append(e["date"])
            latest_date = max(dates) if dates else date_cls.today().isoformat()
            
            _, _, total = invoicing.get_invoice(con, invoice_id)
            payments_total = invoicing.invoice_payments_total(con, invoice_id)
            status = 'paid' if payments_total >= total else 'partially_paid'
            
            con.execute(
                "UPDATE invoices SET status=?, paid_date=?, matched_entry_id=? WHERE id=?",
                (status, latest_date, entry_ids[0], invoice_id)
            )
            _update_entry_customers_for_invoice(con, invoice_id)
        else:
            con.execute(
                "UPDATE invoices SET status='sent', paid_date=NULL, matched_entry_id=NULL WHERE id=?",
                (invoice_id,)
            )
        _cleanup_entry_customers(con)
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
        rows = con.execute("SELECT id FROM invoices WHERE kind='invoice' AND status != 'void' "
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
            linked = con.execute(
                "SELECT credit_invoice_id FROM credit_applications WHERE invoice_id=? "
                "UNION "
                "SELECT invoice_id FROM credit_applications WHERE credit_invoice_id=?",
                (invoice_id, invoice_id)
            ).fetchall()
            con.execute("DELETE FROM credit_applications WHERE invoice_id=? OR credit_invoice_id=?", (invoice_id, invoice_id))
            con.execute("UPDATE invoices SET status='void' WHERE id=? AND status!='paid'", (invoice_id,))
            for r in linked:
                _update_document_status(con, r[0])
        elif action == "draft":
            con.execute("UPDATE invoices SET status='draft' WHERE id=? AND status IN ('sent','void')", (invoice_id,))
        elif action == "delete":
            linked = con.execute(
                "SELECT credit_invoice_id FROM credit_applications WHERE invoice_id=? "
                "UNION "
                "SELECT invoice_id FROM credit_applications WHERE credit_invoice_id=?",
                (invoice_id, invoice_id)
            ).fetchall()
            con.execute("DELETE FROM invoices WHERE id=? AND status IN ('draft','void')", (invoice_id,))
            for r in linked:
                _update_document_status(con, r[0])
            con.commit()
            return RedirectResponse("/invoices", status_code=303)
        con.commit()
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    finally:
        con.close()


@app.post("/invoices/{invoice_id}/apply-credit")
def invoice_apply_credit(invoice_id: int, credit_invoice_id: int = Form(...), amount: float = Form(...), apply_date: str = Form(...)):
    """Apply a credit source (memo or overpaid invoice) ONTO this invoice — from the invoice's side."""
    from urllib.parse import quote
    con = db.connect()
    try:
        try:
            amt = ledger.parse_amount_to_cents(str(amount))
            if amt <= 0:
                raise ValueError("Amount must be greater than 0")
            _apply_credit_core(con, invoice_id, credit_invoice_id, amt, ledger.normalize_date(apply_date))
            con.commit()
            return RedirectResponse(f"/invoices/{invoice_id}?msg=Credit+applied", status_code=303)
        except ValueError as e:
            return RedirectResponse(f"/invoices/{invoice_id}?err=" + quote(str(e)), status_code=303)
    finally:
        con.close()


@app.post("/credit-memos/{credit_id}/apply")
def credit_memo_apply(credit_id: int, invoice_id: int = Form(...), amount: float = Form(...), apply_date: str = Form(...)):
    """Apply THIS credit (memo or overpaid invoice) to a chosen invoice — from the credit's side (#2)."""
    from urllib.parse import quote
    con = db.connect()
    try:
        try:
            amt = ledger.parse_amount_to_cents(str(amount))
            if amt <= 0:
                raise ValueError("Amount must be greater than 0")
            _apply_credit_core(con, invoice_id, credit_id, amt, ledger.normalize_date(apply_date))
            con.commit()
            return RedirectResponse(f"/invoices/{credit_id}?msg=Credit+applied", status_code=303)
        except ValueError as e:
            return RedirectResponse(f"/invoices/{credit_id}?err=" + quote(str(e)), status_code=303)
    finally:
        con.close()


@app.post("/invoices/{invoice_id}/to-credit-memo")
def invoice_overpayment_to_credit(invoice_id: int):
    """Turn an invoice's overpayment excess into a standalone credit memo in one click (#4). The
    excess is moved out of the invoice (recorded as an application onto the new memo), so it is never
    double-counted as available credit."""
    from urllib.parse import quote
    con = db.connect()
    try:
        inv, _, total = invoicing.get_invoice(con, invoice_id)
        if not inv or inv["kind"] != "invoice":
            return RedirectResponse("/invoices", status_code=303)
        applied_as_source = con.execute("SELECT COALESCE(SUM(amount_cents),0) FROM credit_applications "
                                        "WHERE credit_invoice_id=?", (invoice_id,)).fetchone()[0]
        excess = invoicing.invoice_payments_total(con, invoice_id) - total - applied_as_source
        if excess <= 0:
            return RedirectResponse(f"/invoices/{invoice_id}?err=No+overpayment+to+convert", status_code=303)
        today = date_cls.today().isoformat()
        number = invoicing.next_credit_memo_number(con)
        cm_id = con.execute(
            "INSERT INTO invoices(number,customer_id,date,due_date,memo,kind) VALUES(?,?,?,?,?,'credit_memo')",
            (number, inv["customer_id"], today, today, f"Credit from overpayment on {inv['number']}")).lastrowid
        con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,?)",
                    (cm_id, f"Overpayment credit from {inv['number']}", excess))
        # consume the invoice's excess (source = overpaid invoice, target = the new memo)
        con.execute("INSERT INTO credit_applications(credit_invoice_id, invoice_id, amount_cents, date) VALUES(?,?,?,?)",
                    (invoice_id, cm_id, excess, today))
        _update_document_status(con, invoice_id)
        _update_document_status(con, cm_id)
        con.commit()
        return RedirectResponse(f"/invoices/{cm_id}?msg=" + quote(
            f"Created credit memo {number} from the ${ledger.fmt_cents(excess)} overpayment."), status_code=303)
    finally:
        con.close()


@app.post("/credit-applications/{application_id}/delete")
def credit_application_delete(application_id: int, back: str = Form(...)):
    con = db.connect()
    try:
        row = con.execute("SELECT credit_invoice_id, invoice_id FROM credit_applications WHERE id=?", (application_id,)).fetchone()
        if row:
            con.execute("DELETE FROM credit_applications WHERE id=?", (application_id,))
            _update_document_status(con, row["invoice_id"])
            _update_document_status(con, row["credit_invoice_id"])
            con.commit()
        return RedirectResponse(back, status_code=303)
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
            
        payments_total = invoicing.invoice_payments_total(con, invoice_id)
        outstanding = max(0, total - payments_total)
        if outstanding <= 0:
            return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

        d = ledger.normalize_date(paid_date)
        # Split the payment so collected sales tax lands in the Sales Tax Payable liability, not income
        # (proportional to the invoice's tax on a partial payment).
        sub = invoicing.invoice_subtotal(con, invoice_id)
        tax = invoicing.invoice_tax(con, invoice_id)
        inc_part, tax_part = invoicing.tax_allocation(sub, tax, outstanding)
        tax_acct = invoicing.sales_tax_account_id(con)
        if tax_part and tax_acct:
            legs = [(bank_id, outstanding), (income_id, -inc_part), (tax_acct, -tax_part)]
        else:  # no tax (or account missing) → the whole payment is income
            legs = [(bank_id, outstanding), (income_id, -outstanding)]
        entry_id = ledger.post_entry(con, d, f"Invoice {inv['number']} - {inv['customer']}",
                                     legs, memo=f"invoice #{inv['number']}",
                                     customer_id=inv["customer_id"])
        con.execute("UPDATE invoices SET status='paid', paid_date=?, paid_entry_id=? WHERE id=?",
                    (d, entry_id, invoice_id))
        _update_entry_customers_for_invoice(con, invoice_id)
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
        con.execute("UPDATE invoices SET paid_entry_id=NULL WHERE id=?", (invoice_id,))
        
        # Re-evaluate status based on remaining matches in invoice_entry_links
        rem = con.execute(
            "SELECT e.id, e.date FROM entries e "
            "JOIN invoice_entry_links iel ON iel.entry_id=e.id "
            "WHERE iel.invoice_id=? ORDER BY e.date DESC", (invoice_id,)
        ).fetchall()
        if rem:
            total_payments = 0
            for row_rem in rem:
                eid = row_rem["id"]
                val = con.execute(
                    "SELECT SUM(abs(s.amount_cents)) FROM splits s "
                    "JOIN accounts a ON a.id=s.account_id "
                    "WHERE s.entry_id=? AND a.type='income'", (eid,)
                ).fetchone()[0]
                if val:
                    total_payments += val
            
            _, _, total = invoicing.get_invoice(con, invoice_id)
            status = 'paid' if total_payments >= total else 'partially_paid'
            con.execute(
                "UPDATE invoices SET status=?, paid_date=?, matched_entry_id=? WHERE id=?",
                (status, rem[0]["date"], rem[0]["id"], invoice_id)
            )
        else:
            con.execute(
                "UPDATE invoices SET status='sent', paid_date=NULL, matched_entry_id=NULL WHERE id=?",
                (invoice_id,)
            )
        _cleanup_entry_customers(con)
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


# ---------- AR reminders (issue #36) ----------

@app.post("/invoices/{invoice_id}/remind")
def invoice_remind(invoice_id: int):
    from urllib.parse import quote
    con = db.connect()
    try:
        if not invoicing.email_configured(con):
            return RedirectResponse(f"/invoices/{invoice_id}?err=" + quote(
                "Set up SMTP in Settings to send reminders."), status_code=303)
        try:
            res = _reminder_send(con, invoice_id, today=date_cls.today().isoformat())
        except Exception as e:
            return RedirectResponse(f"/invoices/{invoice_id}?err=" + quote(f"Reminder failed: {e}"), status_code=303)
        con.commit()
        if res == "sent":
            return RedirectResponse(f"/invoices/{invoice_id}?msg=" + quote("Reminder emailed."), status_code=303)
        msg = ("That customer has no email address — add one on the Invoices page."
               if res == "no_email" else "Nothing to remind — the invoice isn't open.")
        return RedirectResponse(f"/invoices/{invoice_id}?err=" + quote(msg), status_code=303)
    finally:
        con.close()


@app.post("/invoices/remind-all")
def invoices_remind_all():
    from urllib.parse import quote
    con = db.connect()
    try:
        if not invoicing.email_configured(con):
            return RedirectResponse("/invoices?err=" + quote("Set up SMTP in Settings to send reminders."),
                                    status_code=303)
        today = date_cls.today().isoformat()
        overdue = [r for r in invoicing.ar_aging(con, today)["rows"] if r["overdue"]]
        sent = no_email = skipped = failed = 0
        for r in overdue:
            try:
                res = _reminder_send(con, r["id"], skip_days=7, today=today)
            except Exception:
                failed += 1
                continue
            sent += res == "sent"
            no_email += res == "no_email"
            skipped += res == "skipped"
        con.commit()
        parts = [f"{sent} reminder(s) sent"]
        if skipped:
            parts.append(f"{skipped} skipped (already reminded within 7 days)")
        if no_email:
            parts.append(f"{no_email} with no email on file")
        if failed:
            parts.append(f"{failed} failed to send")
        return RedirectResponse("/invoices?msg=" + quote("; ".join(parts) + "."), status_code=303)
    finally:
        con.close()


def _reminder_send(con, inv_id, skip_days=0, today=None):
    """Send one overdue reminder. Returns 'sent' | 'no_email' | 'skipped'. Raises on SMTP error."""
    from datetime import datetime
    today = today or date_cls.today().isoformat()
    inv, items, total = invoicing.get_invoice(con, inv_id)
    if not inv or inv["kind"] != "invoice" or inv["status"] not in ("sent", "partially_paid") or total <= 0:
        return "skipped"
    if skip_days and inv["last_reminder_date"]:
        last = datetime.strptime(inv["last_reminder_date"], "%Y-%m-%d")
        if (datetime.strptime(today, "%Y-%m-%d") - last).days < skip_days:
            return "skipped"
    to = (inv["customer_email"] or "").strip()
    if not to:
        return "no_email"
    subj = db.get_setting(con, "reminder_subject", "") or None
    body = db.get_setting(con, "reminder_body", "") or None
    pdf = invoicing.render_pdf(con, inv, items, total)
    invoicing.send_invoice_email(con, inv, total, pdf, to, subj, body)
    con.execute("UPDATE invoices SET last_reminder_date=? WHERE id=?", (today, inv_id))
    return "sent"


# ---------- estimates / quotes (issue #35) ----------
# Estimates are invoices rows with kind='estimate': they never post to the ledger or match deposits.
# An accepted estimate converts into a real invoice (copying its line items).

def _estimate_rows(con):
    rows = con.execute(
        "SELECT i.*, c.name customer, c.email customer_email FROM invoices i "
        "JOIN customers c ON c.id=i.customer_id WHERE i.kind='estimate' ORDER BY i.id DESC").fetchall()
    return [{**dict(r), "total": invoicing.invoice_total(con, r["id"])} for r in rows]


@app.get("/estimates", response_class=HTMLResponse)
def estimates_page(request: Request, msg: str = "", err: str = ""):
    con = db.connect()
    try:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "estimates.html", ctx(
            request, con, estimates=_estimate_rows(con), customers=customers, msg=msg, err=err,
            email_on=invoicing.email_configured(con)))
    finally:
        con.close()


@app.get("/estimates/new", response_class=HTMLResponse)
def estimate_new(request: Request):
    con = db.connect()
    try:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        standard_items = con.execute("SELECT * FROM items WHERE active=1 ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "estimate_new.html", ctx(
            request, con, customers=customers, standard_items=standard_items, error=None))
    finally:
        con.close()


@app.post("/estimates/new")
async def estimate_create(request: Request):
    form = await request.form()
    con = db.connect()
    try:
        customer_id = int(form["customer_id"])
        est_date = ledger.normalize_date(form["date"])
        valid_until = ledger.normalize_date(form["valid_until"])
        items = _parse_line_items(form)
        if not items:
            raise ValueError("Add at least one line item.")
        number = invoicing.next_estimate_number(con)
        cur = con.execute(
            "INSERT INTO invoices(number,customer_id,date,due_date,memo,kind) VALUES(?,?,?,?,?,'estimate')",
            (number, customer_id, est_date, valid_until, form.get("memo", "").strip()))
        est_id = cur.lastrowid
        _insert_line_items(con, est_id, items)
        con.commit()
        return RedirectResponse(f"/estimates/{est_id}", status_code=303)
    except (ValueError, KeyError) as e:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "estimate_new.html", ctx(
            request, con, customers=customers, standard_items=_active_items(con), error=str(e)))
    finally:
        con.close()


@app.get("/estimates/{estimate_id}/edit", response_class=HTMLResponse)
def estimate_edit(request: Request, estimate_id: int):
    con = db.connect()
    try:
        est, items, total = invoicing.get_invoice(con, estimate_id)
        if not est or est["kind"] != "estimate":
            return RedirectResponse("/estimates", status_code=303)
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "invoice_edit.html", ctx(
            request, con, inv=est, items=items, customers=customers, standard_items=_active_items(con), error=None))
    finally:
        con.close()


@app.post("/estimates/{estimate_id}/edit")
async def estimate_update(request: Request, estimate_id: int):
    form = await request.form()
    con = db.connect()
    try:
        est, _, _ = invoicing.get_invoice(con, estimate_id)
        if not est or est["kind"] != "estimate":
            return RedirectResponse("/estimates", status_code=303)
        customer_id = int(form["customer_id"])
        est_date = ledger.normalize_date(form["date"])
        due_date = ledger.normalize_date(form["due_date"])
        items = _parse_line_items(form)
        if not items:
            raise ValueError("Add at least one line item.")
        
        con.execute(
            "UPDATE invoices SET customer_id=?, date=?, due_date=?, memo=? WHERE id=?",
            (customer_id, est_date, due_date, form.get("memo", "").strip(), estimate_id))
        con.execute("DELETE FROM invoice_items WHERE invoice_id=?", (estimate_id,))
        _insert_line_items(con, estimate_id, items)
        con.commit()
        return RedirectResponse(f"/estimates/{estimate_id}", status_code=303)
    except (ValueError, KeyError) as e:
        customers = con.execute("SELECT * FROM customers ORDER BY name").fetchall()
        est, items, _ = invoicing.get_invoice(con, estimate_id)
        return templates.TemplateResponse(request, "invoice_edit.html", ctx(
            request, con, inv=est, items=items, customers=customers, standard_items=_active_items(con), error=str(e)))
    finally:
        con.close()


@app.get("/estimates/{estimate_id}", response_class=HTMLResponse)
def estimate_view(request: Request, estimate_id: int, msg: str = "", err: str = ""):
    con = db.connect()
    try:
        est, items, total = invoicing.get_invoice(con, estimate_id)
        if not est or est["kind"] != "estimate":
            return RedirectResponse("/estimates", status_code=303)
        converted = None
        if est["converted_invoice_id"]:
            converted = con.execute("SELECT id, number FROM invoices WHERE id=?",
                                    (est["converted_invoice_id"],)).fetchone()
        return templates.TemplateResponse(request, "estimate_view.html", ctx(
            request, con, inv=est, items=items, total=total, converted=converted, msg=msg, err=err,
            subtotal=invoicing.invoice_subtotal(con, estimate_id), tax=invoicing.invoice_tax(con, estimate_id),
            email_on=invoicing.email_configured(con),
            biz_address=db.get_setting(con, "business_address", ""),
            biz_email=db.get_setting(con, "business_email", ""),
            biz_phone=db.get_setting(con, "business_phone", ""),
            terms=db.get_setting(con, "invoice_terms", "")))
    finally:
        con.close()


@app.post("/estimates/{estimate_id}/status")
def estimate_status(estimate_id: int, action: str = Form(...)):
    con = db.connect()
    try:
        if action == "delete":
            con.execute("DELETE FROM invoices WHERE id=? AND kind='estimate'", (estimate_id,))
            con.commit()
            return RedirectResponse("/estimates", status_code=303)
        if action in ("draft", "sent", "accepted", "declined"):
            con.execute("UPDATE invoices SET status=? WHERE id=? AND kind='estimate'", (action, estimate_id))
            con.commit()
        return RedirectResponse(f"/estimates/{estimate_id}", status_code=303)
    finally:
        con.close()


@app.get("/estimates/{estimate_id}/pdf")
def estimate_pdf(estimate_id: int):
    con = db.connect()
    try:
        est, items, total = invoicing.get_invoice(con, estimate_id)
        if not est or est["kind"] != "estimate":
            return RedirectResponse("/estimates", status_code=303)
        pdf = invoicing.render_pdf(con, est, items, total)
        return StreamingResponse(iter([pdf]), media_type="application/pdf",
                                 headers={"Content-Disposition": f"inline; filename={est['number']}.pdf"})
    finally:
        con.close()


@app.post("/estimates/{estimate_id}/email")
def estimate_email(estimate_id: int, to_addr: str = Form(...), subject: str = Form(""), body: str = Form("")):
    from urllib.parse import quote
    con = db.connect()
    try:
        est, items, total = invoicing.get_invoice(con, estimate_id)
        if not est or est["kind"] != "estimate":
            return RedirectResponse("/estimates", status_code=303)
        biz = db.get_setting(con, "business_name", "My Business")
        subj = subject.strip() or f"Estimate {est['number']} from {biz}"
        msg = body.strip() or (f"Hi {est['customer']},\n\nAttached is estimate {est['number']} for "
                               f"${ledger.fmt_cents(total)}, valid until {est['due_date']}. "
                               "Let me know if you'd like to proceed.\n\nThank you!")
        pdf = invoicing.render_pdf(con, est, items, total)
        try:
            invoicing.send_invoice_email(con, est, total, pdf, to_addr.strip(), subj, msg)
        except Exception as e:
            return RedirectResponse(f"/estimates/{estimate_id}?err=" + quote(f"Email failed: {e}"), status_code=303)
        con.execute("UPDATE invoices SET status='sent' WHERE id=? AND status='draft'", (estimate_id,))
        con.commit()
        return RedirectResponse(f"/estimates/{estimate_id}?msg=" + quote(f"Emailed to {to_addr}"), status_code=303)
    finally:
        con.close()


@app.post("/estimates/{estimate_id}/convert")
def estimate_convert(estimate_id: int):
    from urllib.parse import quote
    from datetime import timedelta
    con = db.connect()
    try:
        est, items, total = invoicing.get_invoice(con, estimate_id)
        if not est or est["kind"] != "estimate":
            return RedirectResponse("/estimates", status_code=303)
        if est["converted_invoice_id"]:  # already converted — go to the existing invoice
            return RedirectResponse(f"/invoices/{est['converted_invoice_id']}", status_code=303)
        today = date_cls.today()
        number = invoicing.next_number(con)
        cur = con.execute(
            "INSERT INTO invoices(number,customer_id,date,due_date,memo,kind) VALUES(?,?,?,?,?,'invoice')",
            (number, est["customer_id"], today.isoformat(), (today + timedelta(days=30)).isoformat(),
             est["memo"]))
        inv_id = cur.lastrowid
        for it in items:
            con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,item_id,taxable) VALUES(?,?,?,?,?,?)",
                        (inv_id, it["description"], it["qty"], it["unit_cents"], it["item_id"], it["taxable"]))
        con.execute("UPDATE invoices SET status='accepted', converted_invoice_id=? WHERE id=?",
                    (inv_id, estimate_id))
        con.commit()
        return RedirectResponse(f"/invoices/{inv_id}?msg=" + quote(
            f"Invoice {number} created from estimate {est['number']}."), status_code=303)
    finally:
        con.close()


# ---------- recurring transactions (issue #39) ----------

@app.get("/recurring", response_class=HTMLResponse)
def recurring_page(request: Request, msg: str = "", err: str = ""):
    con = db.connect()
    try:
        banks = con.execute("SELECT id, name FROM accounts WHERE kind IN ('bank','card') AND active=1 "
                            "ORDER BY type, name").fetchall()
        return templates.TemplateResponse(request, "recurring.html", ctx(
            request, con, items=recurring.list_all(con), banks=banks,
            cats=categories(con, ("expense", "income")),
            suggestions=recurring.detect_candidates(con), msg=msg, err=err))
    finally:
        con.close()


@app.post("/recurring")
def recurring_create(name: str = Form(...), amount: str = Form(...), flow: str = Form("expense"),
                     account_id: int = Form(...), category_id: int = Form(...),
                     frequency: str = Form("monthly"), next_date: str = Form(...), memo: str = Form("")):
    from urllib.parse import quote
    con = db.connect()
    try:
        try:
            cents = abs(ledger.parse_amount_to_cents(amount))
            nd = ledger.normalize_date(next_date)
        except ValueError as e:
            return RedirectResponse("/recurring?err=" + quote(f"Couldn't read that: {e}"), status_code=303)
        flow = "income" if flow == "income" else "expense"
        freq = frequency if frequency in ("weekly", "monthly", "yearly") else "monthly"
        con.execute(
            "INSERT INTO recurring(name,amount_cents,flow,account_id,category_id,frequency,next_date,memo) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (name.strip(), cents, flow, account_id, category_id, freq, nd, memo.strip()))
        con.commit()
        return RedirectResponse("/recurring?msg=" + quote(f"Added '{name.strip()}'."), status_code=303)
    finally:
        con.close()


@app.post("/recurring/post-all")
def recurring_post_all():
    from urllib.parse import quote
    con = db.connect()
    try:
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
        return RedirectResponse("/recurring?msg=" + quote("; ".join(parts) + "."), status_code=303)
    finally:
        con.close()


@app.post("/recurring/{rid}/post")
def recurring_post(rid: int):
    from urllib.parse import quote
    con = db.connect()
    try:
        try:
            recurring.post_occurrence(con, rid)
            con.commit()
            return RedirectResponse("/recurring?msg=" + quote(
                "Posted to the ledger and advanced to the next date."), status_code=303)
        except ValueError as e:
            return RedirectResponse("/recurring?err=" + quote(str(e)), status_code=303)
    finally:
        con.close()


@app.post("/recurring/{rid}/skip")
def recurring_skip(rid: int):
    from urllib.parse import quote
    con = db.connect()
    try:
        recurring.skip_occurrence(con, rid)
        con.commit()
        return RedirectResponse("/recurring?msg=" + quote(
            "Skipped — advanced to the next date without posting."), status_code=303)
    finally:
        con.close()


@app.post("/recurring/{rid}/toggle")
def recurring_toggle(rid: int):
    con = db.connect()
    try:
        con.execute("UPDATE recurring SET active = 1 - active WHERE id=?", (rid,))
        con.commit()
        return RedirectResponse("/recurring", status_code=303)
    finally:
        con.close()


@app.post("/recurring/{rid}/delete")
def recurring_delete(rid: int):
    con = db.connect()
    try:
        con.execute("DELETE FROM recurring WHERE id=?", (rid,))
        con.commit()
        return RedirectResponse("/recurring", status_code=303)
    finally:
        con.close()


# ---------- bank feeds (SimpleFIN, issue #43) ----------

def _feed_ai_categorize(con, txns):
    """The same categorization recipe the statement-import route uses: only ask the model when rules
    leave something uncategorized, and only when AI is available. Returns names list or None."""
    cats = {a["id"]: a["name"] for a in categories(con, ("expense", "income"))}
    uncategorized = [t for t in txns if importer.apply_rules(con, t["description"]) is None]
    if uncategorized and ai.available(con):
        return ai.categorize(con, [{"description": t["description"], "amount": t["amount_cents"]} for t in txns],
                             list(cats.values()))
    return None


@app.post("/feeds/claim")
def feeds_claim(setup_token: str = Form(...)):
    from urllib.parse import quote
    con = db.connect()
    try:
        try:
            url = feeds.claim_setup_token(setup_token)
        except ValueError as e:
            return RedirectResponse("/settings?err=" + quote(str(e)), status_code=303)
        db.set_setting(con, "simplefin_access_url", url)
        con.commit()
        try:
            n = feeds.refresh_accounts(con)
            con.commit()
            note = (f"Bank feeds connected — the bridge reports {n} account(s). Map each one to its "
                    "ShopBooks account below, then Fetch.")
        except Exception:
            note = "Bank feeds connected. Couldn't list accounts yet — try 'Fetch from bank feeds' in a moment."
        return RedirectResponse("/settings?msg=" + quote(note), status_code=303)
    finally:
        con.close()


@app.post("/feeds/map")
def feeds_map(feed_account_id: str = Form(...), account_id: str = Form(""), enabled: str = Form("")):
    con = db.connect()
    try:
        acct = int(account_id) if account_id.strip() else None
        con.execute("UPDATE feed_accounts SET account_id=?, enabled=? WHERE id=?",
                    (acct, 1 if enabled else 0, feed_account_id))
        con.commit()
        return RedirectResponse("/settings", status_code=303)
    finally:
        con.close()


@app.post("/feeds/fetch")
def feeds_fetch():
    from urllib.parse import quote
    con = db.connect()
    try:
        try:
            r = feeds.fetch(con, categorize=_feed_ai_categorize)
            con.commit()
        except ValueError as e:
            return RedirectResponse("/settings?err=" + quote(str(e)), status_code=303)
        except Exception as e:
            return RedirectResponse("/settings?err=" + quote(
                f"Couldn't reach the bank feed (it may be busy — the bridge refreshes daily): {e}"), status_code=303)
        parts = [f"Fetched {r['staged']} new transaction(s) from the bank feed"]
        if r["accounts"]:
            parts.append(", ".join(f"{a['name']}: {a['new']}" for a in r["accounts"]))
        if r["unmapped"]:
            parts.append(f"unmapped (skipped): {', '.join(r['unmapped'])} — map them in Settings")
        if r["staged"]:
            return RedirectResponse("/review?note=" + quote("; ".join(parts) + "."), status_code=303)
        return RedirectResponse("/settings?msg=" + quote("; ".join(parts) + ". Nothing new to review."), status_code=303)
    finally:
        con.close()


@app.post("/feeds/disconnect")
def feeds_disconnect():
    from urllib.parse import quote
    con = db.connect()
    try:
        db.set_setting(con, "simplefin_access_url", "")
        con.commit()
        return RedirectResponse("/settings?msg=" + quote(
            "Bank feeds disconnected. (Mappings kept; also deactivate the app on bridge.simplefin.org "
            "if you're done with it.)"), status_code=303)
    finally:
        con.close()


# ---------- tax package ----------

@app.get("/taxes", response_class=HTMLResponse)
def taxes_page(request: Request, year: int = 0, msg: str = "", err: str = ""):
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

        # Get estimated income tax rate
        est_rate_str = db.get_setting(con, "estimated_income_tax_rate", "15")
        try:
            est_rate = float(est_rate_str)
        except ValueError:
            est_rate = 15.0

        # Calculate tax details
        sch_c = insights.schedule_c_report(con, start, end)
        est_tx = insights.estimated_taxes(con, year, est_rate)

        tax_payments = con.execute(
            "SELECT * FROM tax_payments WHERE year=? ORDER BY date DESC, id DESC", (year,)).fetchall()
        return templates.TemplateResponse(request, "taxes.html", ctx(
            request, con, year=year, pnl=p, miles=miles, rate=rate,
            mileage_deduction=round(miles * rate * 100), uncat=uncat, pending=pending,
            receipts_matched=receipts_matched, receipts_unmatched=receipts_unmatched,
            missing_receipts=missing_receipts,
            schedule_c=sch_c, estimated_taxes=est_tx, tax_payments=tax_payments,
            estimated_income_tax_rate=est_rate_str,
            locked_through=ledger.locked_through(con), msg=msg, err=err))
    finally:
        con.close()


@app.post("/taxes/payment")
def taxes_payment_add(year: int = Form(...), quarter: str = Form(...), date: str = Form(...),
                      amount: str = Form(...), memo: str = Form("")):
    """Record an estimated-tax payment actually made (1040-ES). Keyed to the TAX year+quarter —
    Q4 is typically paid in January of the following calendar year."""
    from urllib.parse import quote
    con = db.connect()
    try:
        if quarter not in ("Q1", "Q2", "Q3", "Q4"):
            return RedirectResponse(f"/taxes?year={year}&err=" + quote("Pick a quarter."), status_code=303)
        try:
            d = ledger.normalize_date(date)
            cents = abs(ledger.parse_amount_to_cents(amount))
            if cents == 0:
                raise ValueError("amount is zero")
        except ValueError as e:
            return RedirectResponse(f"/taxes?year={year}&err=" + quote(f"Couldn't read that: {e}"), status_code=303)
        con.execute("INSERT INTO tax_payments(year,quarter,date,amount_cents,memo) VALUES(?,?,?,?,?)",
                    (year, quarter, d, cents, memo.strip()))
        con.commit()
        return RedirectResponse(f"/taxes?year={year}&msg=" + quote(
            f"Recorded ${ledger.fmt_cents(cents)} toward {year} {quarter}."), status_code=303)
    finally:
        con.close()


@app.post("/taxes/payment/{payment_id}/delete")
def taxes_payment_delete(payment_id: int, year: int = Form(...)):
    con = db.connect()
    try:
        con.execute("DELETE FROM tax_payments WHERE id=?", (payment_id,))
        con.commit()
        return RedirectResponse(f"/taxes?year={year}", status_code=303)
    finally:
        con.close()


@app.post("/taxes/close")
def taxes_close(through: str = Form(...)):
    con = db.connect()
    try:
        from urllib.parse import quote
        try:
            d = ledger.normalize_date(through)
        except ValueError:
            return RedirectResponse("/taxes?err=" + quote("Enter a valid date to close the books through."),
                                    status_code=303)
        db.set_setting(con, "books_locked_through", d)
        con.commit()
        return RedirectResponse("/taxes?msg=" + quote(
            f"Books closed through {d}. Transactions on or before that date are now locked."), status_code=303)
    finally:
        con.close()


@app.post("/taxes/reopen")
def taxes_reopen():
    con = db.connect()
    try:
        from urllib.parse import quote
        db.set_setting(con, "books_locked_through", "")
        con.commit()
        return RedirectResponse("/taxes?msg=" + quote("Books reopened — every period is editable again."),
                                status_code=303)
    finally:
        con.close()


@app.post("/taxes/settings")
def taxes_save_settings(estimated_income_tax_rate: str = Form(...)):
    con = db.connect()
    try:
        try:
            rate = float(estimated_income_tax_rate.strip())
            if rate < 0 or rate > 100:
                raise ValueError()
        except ValueError:
            from urllib.parse import quote
            return RedirectResponse("/taxes?err=" + quote("Tax rate must be a number between 0 and 100."), status_code=303)
        db.set_setting(con, "estimated_income_tax_rate", str(rate))
        con.commit()
        return RedirectResponse("/taxes", status_code=303)
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
        sch_c = insights.schedule_c_report(con, start, end)
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

            def schedule_c_rows(w):
                w.writerow(["IRS Schedule C Mapping Report", f"{start} to {end}"]); w.writerow([])
                w.writerow(["INCOME"])
                for item in sch_c["income"]:
                    w.writerow([item["line"], f"{item['amount']/100:.2f}"])
                    for acct in item["accounts"]:
                        w.writerow([f"  {acct['name']}", f"{acct['amount']/100:.2f}"])
                w.writerow(["Total Schedule C Income", f"{sch_c['total_income']/100:.2f}"]); w.writerow([])
                w.writerow(["EXPENSES"])
                for item in sch_c["expenses"]:
                    w.writerow([item["line"], f"{item['amount']/100:.2f}"])
                    for acct in item["accounts"]:
                        w.writerow([f"  {acct['name']}", f"{acct['amount']/100:.2f}"])
                w.writerow(["Total Schedule C Expenses", f"{sch_c['total_expenses']/100:.2f}"]); w.writerow([])
                w.writerow(["Net Schedule C Profit/Loss", f"{sch_c['net']/100:.2f}"])
                if sch_c["unmapped"]:
                    w.writerow([])
                    w.writerow(["UNMAPPED CATEGORIES (WARNING)"])
                    for acct in sch_c["unmapped"]:
                        w.writerow([acct["name"], f"{acct['balance']/100:.2f}"])
            z.writestr(f"{year}_schedule_c.csv", make_csv(schedule_c_rows))

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


SCHEDULE_C_LINES = [
    "Gross receipts or sales (Line 1)",
    "Other income (Line 6)",
    "Advertising (Line 8)",
    "Car and truck expenses (Line 9)",
    "Commissions and fees (Line 10)",
    "Contract labor (Line 11)",
    "Depletion (Line 12)",
    "Depreciation and section 179 expense (Line 13)",
    "Employee benefit programs (Line 14)",
    "Insurance (other than health) (Line 15)",
    "Interest: Mortgage (Line 16a)",
    "Interest: Other (Line 16b)",
    "Legal and professional services (Line 17)",
    "Office expense (Line 18)",
    "Pension and profit-sharing plans (Line 19)",
    "Rent or lease: Vehicles, machinery, and equipment (Line 20a)",
    "Rent or lease: Other business property (Line 20b)",
    "Repairs and maintenance (Line 21)",
    "Supplies (not included in Part III) (Line 22)",
    "Taxes and licenses (Line 23)",
    "Travel and meals: Travel (Line 24a)",
    "Travel and meals: Deductible meals (Line 24b)",
    "Utilities (Line 25)",
    "Wages (less employment credits) (Line 26)",
    "Other expenses (Line 27a)",
]


@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request, err: str = "", show_hidden: str = ""):
    con = db.connect()
    try:
        accounts = ledger.accounts_with_balances(con, include_inactive=bool(show_hidden))
        parents = [a for a in accounts if a["parent_id"] is None and a["active"]]
        hidden_count = con.execute("SELECT COUNT(*) c FROM accounts WHERE active=0").fetchone()["c"]
        return templates.TemplateResponse(request, "accounts.html", ctx(
            request, con, accounts=accounts, parents=parents, err=err,
            show_hidden=bool(show_hidden), hidden_count=hidden_count,
            schedule_c_lines=SCHEDULE_C_LINES))
    finally:
        con.close()


@app.post("/accounts/schedule_c")
def accounts_set_schedule_c(account_id: int = Form(...), schedule_c_line: str = Form(""), show_hidden: str = Form("")):
    con = db.connect()
    try:
        suffix = "?show_hidden=1" if show_hidden else ""
        val = schedule_c_line.strip()
        if not val or val not in SCHEDULE_C_LINES:
            val = None
        con.execute("UPDATE accounts SET schedule_c_line=? WHERE id=?", (val, account_id))
        con.commit()
        return RedirectResponse("/accounts" + suffix, status_code=303)
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
        bankcards = con.execute("SELECT id, name FROM accounts WHERE active=1 AND kind IN ('bank','card') "
                                "ORDER BY type, name").fetchall()
        return templates.TemplateResponse(request, "settings.html", ctx(
            request, con, s=s, key_set=bool(key),
            smtp_set=bool(db.get_setting(con, "smtp_password", "")),
            feeds_connected=feeds.connected(con), feed_accounts=feeds.list_feed_accounts(con),
            bankcards=bankcards,
            backup=backup.status(), restorable=backup.list_restorable()[:30],
            sync_status=sync.status(), watch_status=watcher.status(), msg=msg, err=err))
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
                 "email_subject", "email_body", "reminder_subject", "reminder_body",
                 "estimated_income_tax_rate", "statements_watch_folder", "receipts_watch_folder")
        for k in plain:
            if k in form:
                db.set_setting(con, k, str(form[k]).strip())
        # sales tax rate: sanitize to a non-negative number (accepts "8.25" or "8.25%")
        if "sales_tax_rate" in form:
            raw = str(form["sales_tax_rate"]).strip().rstrip("%").strip()
            try:
                rate = max(0.0, float(raw or 0))
            except ValueError:
                rate = 0.0
            db.set_setting(con, "sales_tax_rate", str(rate))
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


@app.post("/watch/scan-now")
def watch_scan_now():
    from urllib.parse import quote
    con = db.connect()
    try:
        r = watcher.run_once(con, _watch_statement, _watch_receipt)
        con.commit()
        def summarize(label, r):
            if not r["enabled"]:
                return None
            if not r["scanned"]:
                return f"{label}: nothing new"
            parts = ", ".join(f"{v} {k}" for k, v in r["counts"].items())
            return f"{label}: {parts}"
        parts = [p for p in (summarize("Statements", r["statements"]), summarize("Receipts", r["receipts"])) if p]
        note = "; ".join(parts) if parts else "No watch folders are set up yet."
        return RedirectResponse("/settings?msg=" + quote(note), status_code=303)
    finally:
        con.close()
