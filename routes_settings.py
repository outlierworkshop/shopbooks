"""Chart of accounts, rules, application settings, backup/restore, sync routes."""
import sqlite3
from datetime import date as date_cls
from pathlib import Path
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

import ai
import backup
import db
import feeds
import ledger
import sync
import watcher
from staging import _watch_receipt, _watch_statement
from webutil import categories, ctx, get_con, safe_redirect, templates

router = APIRouter()

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

@router.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request, err: str = "", show_hidden: str = "", con=Depends(get_con)):
    accounts = ledger.accounts_with_balances(con, include_inactive=bool(show_hidden))
    parents = [a for a in accounts if a["parent_id"] is None and a["active"]]
    hidden_count = con.execute("SELECT COUNT(*) c FROM accounts WHERE active=0").fetchone()["c"]
    return templates.TemplateResponse(request, "accounts.html", ctx(
        request, con, accounts=accounts, parents=parents, err=err,
        show_hidden=bool(show_hidden), hidden_count=hidden_count,
        schedule_c_lines=SCHEDULE_C_LINES))

@router.post("/accounts/schedule_c")
def accounts_set_schedule_c(account_id: int = Form(...), schedule_c_line: str = Form(""), show_hidden: str = Form(""),
                            con=Depends(get_con)):
    suffix = "?show_hidden=1" if show_hidden else ""
    val = schedule_c_line.strip()
    if not val or val not in SCHEDULE_C_LINES:
        val = None
    con.execute("UPDATE accounts SET schedule_c_line=? WHERE id=?", (val, account_id))
    con.commit()
    return RedirectResponse("/accounts" + suffix, status_code=303)

@router.post("/accounts/active")
def accounts_set_active(account_id: int = Form(...), active: int = Form(...), show_hidden: str = Form(""),
                        con=Depends(get_con)):
    back = "/accounts?show_hidden=1" if show_hidden else "/accounts"
    if not active:  # hiding: protect reports — refuse if the account has history or active children
        if con.execute("SELECT 1 FROM splits WHERE account_id=? LIMIT 1", (account_id,)).fetchone():
            return safe_redirect(back, err="Can't hide an account that has transactions — it would drop from reports.")
        if con.execute("SELECT 1 FROM accounts WHERE parent_id=? AND active=1 LIMIT 1", (account_id,)).fetchone():
            return safe_redirect(back, err="Hide or move its sub-accounts first.")
    con.execute("UPDATE accounts SET active=? WHERE id=?", (1 if active else 0, account_id))
    con.commit()
    return RedirectResponse(back, status_code=303)

@router.post("/accounts")
def accounts_add(name: str = Form(...), type: str = Form("expense"), kind: str = Form("category"),
                 parent_id: str = Form(""), con=Depends(get_con)):
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
        return safe_redirect("/accounts", err=f"An account named '{name.strip()}' already exists (names must be unique).")
    except ValueError as e:
        return safe_redirect("/accounts", err=str(e))

@router.post("/accounts/rename")
def accounts_rename(account_id: int = Form(...), name: str = Form(...), con=Depends(get_con)):
    con.execute("UPDATE accounts SET name=? WHERE id=?", (name.strip(), account_id))
    con.commit()
    return RedirectResponse("/accounts", status_code=303)

@router.post("/accounts/parent")
def accounts_set_parent(account_id: int = Form(...), parent_id: str = Form(""), con=Depends(get_con)):
    try:
        _set_parent(con, account_id, int(parent_id) if parent_id else None)
        con.commit()
        return RedirectResponse("/accounts", status_code=303)
    except ValueError as e:
        return safe_redirect("/accounts", err=str(e))

@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, con=Depends(get_con)):
    rules = con.execute(
        "SELECT r.*, a.name account FROM rules r JOIN accounts a ON a.id=r.account_id ORDER BY r.pattern").fetchall()
    return templates.TemplateResponse(request, "rules.html", ctx(request, con, rules=rules, cats=categories(con)))

@router.post("/rules")
def rules_add(pattern: str = Form(...), account_id: int = Form(...), con=Depends(get_con)):
    con.execute("INSERT INTO rules(pattern,account_id) VALUES(?,?)", (pattern.strip(), account_id))
    con.commit()
    return RedirectResponse("/rules", status_code=303)

@router.post("/rules/delete")
def rules_delete(rule_id: int = Form(...), con=Depends(get_con)):
    con.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    con.commit()
    return RedirectResponse("/rules", status_code=303)

@router.get("/settings/browse-folder")
def browse_folder(path: str = ""):
    """List subdirectories of a local path, for the folder-picker widget on Settings (statement/
    receipt watcher folders, the extra backup folder). Local-only app (CLAUDE.md invariant #8: no
    auth, binds 127.0.0.1 only) - this reads directory NAMES only, never file contents, and grants
    no more filesystem reach than the plain-text path fields it replaces already hand the watcher/
    backup features. Never raises: an unreadable or missing path falls back to the home directory."""
    p = Path(path).expanduser() if path.strip() else Path.home()
    try:
        p = p.resolve()
        if not p.is_dir():
            p = Path.home().resolve()
    except Exception:
        p = Path.home().resolve()
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    dirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue  # broken symlink / permission error on stat - skip, don't fail the listing
    except PermissionError:
        pass
    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "dirs": dirs}

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = "", err: str = "", con=Depends(get_con)):
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

@router.get("/backup.zip")
def backup_zip():
    data = backup.zip_bytes()
    ts = date_cls.today().isoformat()
    return StreamingResponse(iter([data]), media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename=shopbooks_backup_{ts}.zip"})

@router.post("/backup/now")
def backup_now(back: str = Form("/settings")):
    backup.snapshot()
    dest = back if back.startswith("/") else "/settings"
    sep = "&" if "?" in dest else "?"
    return RedirectResponse(f"{dest}{sep}saved=1", status_code=303)

@router.post("/backup/restore")
def backup_restore(name: str = Form(...)):
    had_data = not backup.looks_fresh(db.DB_PATH)
    try:
        backup.restore(name)
    except FileNotFoundError:
        return safe_redirect("/settings", err="That backup could not be found.")
    note = f"Restored from {name}." + (" Your previous data was saved as a pre-restore backup." if had_data else "")
    return safe_redirect("/settings", msg=note)

@router.post("/sync/enable")
def sync_enable(on: str = Form("0"), con=Depends(get_con)):
    db.set_setting(con, "sync_enabled", "1" if on == "1" else "0")
    con.commit()
    if on == "1" and not sync.cloud():
        return safe_redirect("/settings", err=
            "Sync turned on, but no cloud folder is set. Set a Backup folder (in a synced "
            "Dropbox/OneDrive location) above, then it will sync there.")
    msg = "Cloud sync turned on." if on == "1" else "Cloud sync turned off."
    return safe_redirect("/settings", msg=msg)

@router.post("/sync/now")
def sync_now():
    r = sync.export_on_close()
    s = r.get("status")
    if s == "exported":
        note = f"Synced to the cloud (version {r['version']})."
    elif s == "unchanged":
        note = "Already in sync - nothing to push."
    elif s == "blocked_cloud_newer":
        return safe_redirect("/settings", err=
            "The cloud copy is newer than your last sync - the other computer pushed changes. "
            "Use 'Pull from cloud now' to get them, or 'Keep this computer's books' to overwrite.")
    elif s == "no_cloud":
        return safe_redirect("/settings", err="No cloud folder set - set a Backup folder in a synced location first.")
    elif s == "disabled":
        return safe_redirect("/settings", err="Turn cloud sync on first.")
    else:
        note = f"Sync: {s}" + (f" ({r['error']})" if r.get("error") else "")
    return safe_redirect("/settings", msg=note)

@router.post("/sync/pull")
def sync_pull():
    r = sync.pull()
    s = r.get("status")
    if r.get("imported"):
        return safe_redirect("/settings", msg=f"Pulled the latest books from the cloud (version {r.get('cloud_version')}).")
    if s == "up_to_date":
        note = "Already up to date with the cloud - nothing to pull."
    elif s == "cloud_unavailable":
        return safe_redirect("/settings", err=
            "The cloud copy hasn't finished downloading yet. Open your sync folder in Finder/Explorer "
            "to force it to download, then try Pull again.")
    elif s == "conflict":
        return safe_redirect("/settings", err=
            "Both this computer and the cloud changed - choose 'Take the cloud copy' or "
            "'Keep this computer's books' below.")
    elif s == "local_ahead":
        return safe_redirect("/settings", err="Your books here are newer than the cloud copy - nothing to pull.")
    elif s == "no_cloud":
        return safe_redirect("/settings", err="No cloud folder set - set a Backup folder in a synced location first.")
    elif s == "disabled":
        return safe_redirect("/settings", err="Turn cloud sync on first.")
    else:
        note = f"Sync: {s}" + (f" ({r['error']})" if r.get("error") else "")
    return safe_redirect("/settings", msg=note)

@router.post("/sync/resolve")
def sync_resolve(choice: str = Form(...)):
    if choice == "cloud":
        r = sync.take_cloud()
        note = "Took the cloud copy; this computer's unsynced changes were saved as a pre-sync backup."
    elif choice == "local":
        r = sync.keep_local()
        note = "Kept this computer's books and overwrote the cloud copy."
    else:
        return safe_redirect("/settings", err="Unknown choice.")
    if r.get("status") in ("no_cloud", "error"):
        return safe_redirect("/settings", err="Could not resolve: " + r.get("error", r.get("status", "")))
    return safe_redirect("/settings", msg=note)

@router.post("/ollama/test")
def ollama_test(con=Depends(get_con)):
    st = ai.ollama_status(con)
    if not st["reachable"]:
        return safe_redirect("/settings", err=f"Can't reach Ollama at {ai.ollama_url(con)} - is it running? ({st.get('error','')})")
    if not st["model_present"]:
        have = ", ".join(st["models"]) or "none"
        return safe_redirect("/settings", err=
            f"Ollama is running but model '{st['model']}' isn't installed. "
            f"Run:  ollama pull {st['model']}   (installed: {have})")
    return safe_redirect("/settings", msg=f"Ollama OK - reached {ai.ollama_url(con)}, model '{st['model']}' is ready.")

@router.post("/settings")
async def settings_save(request: Request, con=Depends(get_con)):
    form = await request.form()
    plain = ("mileage_rate", "default_hourly_rate", "ai_backend", "ai_model", "categorize_model",
             "ollama_url", "ollama_model", "business_name", "backup_dir", "business_address", "business_email",
             "business_phone", "invoice_terms", "smtp_host", "smtp_port", "smtp_user",
             "email_subject", "email_body", "reminder_subject", "reminder_body",
             "estimated_income_tax_rate", "statements_watch_folder", "receipts_watch_folder",
             "gsa_api_key")
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
    new_dir = str(form.get("backup_dir", "")).strip()
    if new_dir:
        if backup.check_writable(new_dir):
            backup.snapshot()
            return safe_redirect("/settings", msg="Settings saved. Backup folder set and a backup was written there.")
        return safe_redirect("/settings", err="Settings saved, but that backup folder is not writable - check the path. Falling back to auto-detect.")
    return safe_redirect("/settings", msg="Settings saved.")

@router.post("/watch/scan-now")
def watch_scan_now(con=Depends(get_con)):
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
    return safe_redirect("/settings", msg=note)
