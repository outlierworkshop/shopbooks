"""Shared web-layer glue: templates env, per-page context, account/category option lists, and the
route plumbing helpers (get_con dependency, safe_redirect).
Imported by every routes_* module (and staging); must never import staging or a routes_* module,
keeping the web-layer import graph acyclic: webutil ← staging ← routes_* ← app. Startup side
effects (db.init, sync, backup snapshot) live in app.py, the composition root — not here."""
from datetime import date as date_cls
from pathlib import Path
from urllib.parse import quote

from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import ai
import backup
import db
import ledger
import sync

BASE = Path(__file__).resolve().parent
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


def get_con():
    """FastAPI dependency: one DB connection per request, always closed. Use as
    `con=Depends(get_con)` in a route signature instead of hand-rolling
    `con = db.connect() / try / finally: con.close()`. Handlers still call `con.commit()`
    explicitly — with SQLite and cash-basis writes, the explicit commit is a feature, not
    boilerplate: nothing persists unless the handler says so."""
    con = db.connect()
    try:
        yield con
    finally:
        con.close()


def safe_redirect(back, fallback="/", msg=None, err=None):
    """303 redirect to an in-app path only. `back` usually comes from a form field, so an absolute
    URL (open-redirect) falls back to `fallback`. Optional msg/err are appended as query params,
    URL-quoted — replaces the copy-pasted `back if back.startswith("/") else "/"` +
    inline `quote()` imports scattered across the route modules."""
    dest = back if (back or "").startswith("/") else fallback
    if msg:
        dest += ("&" if "?" in dest else "?") + "msg=" + quote(str(msg))
    if err:
        dest += ("&" if "?" in dest else "?") + "err=" + quote(str(err))
    return RedirectResponse(dest, status_code=303)


def ctx(request, con, **kw):
    pending = con.execute("SELECT COUNT(*) c FROM staged WHERE status='pending'").fetchone()["c"]
    unmatched = con.execute("SELECT COUNT(*) c FROM documents WHERE status='unmatched'").fetchone()["c"]
    # bank/card accounts for the nav "Registers" dropdown (every page shows the nav)
    nav_accounts = con.execute(
        "SELECT id, name, kind FROM accounts WHERE kind IN ('bank','card') AND active=1 "
        "ORDER BY kind, name").fetchall()
    # income accounts for the "+ New service" mini-form in the invoice/estimate line editor
    income_accounts = con.execute(
        "SELECT id, name FROM accounts WHERE type='income' AND active=1 ORDER BY name").fetchall()
    return {"request": request, "pending_count": pending, "unmatched_count": unmatched,
            "nav_accounts": nav_accounts, "income_accounts": income_accounts,
            "cloud_sync_on": db.get_setting(con, "sync_enabled", "0") == "1",
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

def _active_jobs(con):
    return con.execute("SELECT id, name FROM jobs WHERE status='active' ORDER BY created_at DESC").fetchall()

def _entry_sources(con):
    """Bank/card accounts (the money account an entry moves through), tree order."""
    return ledger.accounts_with_balances(con, kinds=("bank", "card"))

_INLINE_MEDIA = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
}
