"""ShopBooks — local double-entry accounting for a one-person business.

This is the composition root ONLY: it creates the FastAPI app, runs the launch sequence
(db.init → sync fast-forward → backup snapshot), wires the folder watchers, and includes one
router per domain. Route handlers live in routes_*.py; shared web glue (templates/ctx/categories)
in webutil.py; the ingest→match→post engine in staging.py; pure domain logic in the modules
listed in CLAUDE.md. Add new routes to the matching routes_* module — not here.
"""
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import backup
import db
import sync
import watcher

db.init()
sync.import_on_boot()  # if cloud sync is on: fast-forward from the other machine (never clobbers)
backup.snapshot()      # protect the books on every launch (local + cloud mirror)

# Routers import webutil/staging, which is why the launch sequence above runs first.
import staging  # noqa: E402
import routes_dashboard  # noqa: E402
import routes_review  # noqa: E402
import routes_entries  # noqa: E402
import routes_receipts  # noqa: E402
import routes_time  # noqa: E402
import routes_reconcile  # noqa: E402
import routes_reports  # noqa: E402
import routes_migrate  # noqa: E402
import routes_customers  # noqa: E402
import routes_items  # noqa: E402
import routes_invoices  # noqa: E402
import routes_estimates  # noqa: E402
import routes_recurring  # noqa: E402
import routes_feeds  # noqa: E402
import routes_taxes  # noqa: E402
import routes_travel  # noqa: E402
import routes_checks  # noqa: E402
import routes_square  # noqa: E402
import routes_help  # noqa: E402
import routes_settings  # noqa: E402
from webutil import BASE  # noqa: E402

app = FastAPI(title="ShopBooks")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
for _mod in (routes_dashboard, routes_review, routes_entries, routes_receipts, routes_time,
             routes_reconcile, routes_reports, routes_migrate, routes_customers, routes_items,
             routes_invoices, routes_estimates, routes_recurring, routes_feeds, routes_taxes,
             routes_travel, routes_checks, routes_square, routes_help, routes_settings):
    app.include_router(_mod.router)


@app.on_event("startup")
def _start_watchers():
    # Deferred to the startup event (not called at import time). TestClient(app.app) used without
    # `with` (the pattern this repo's tests use) never fires this, so tests never spin up a real
    # background thread — they call watcher.run_once(...) directly instead.
    import trips
    watcher.start(staging._watch_statement, staging._watch_receipt, trips._watch_trip_event)


@app.on_event("shutdown")
def _sync_on_close():
    watcher.stop()
    sync.export_on_close()  # push this machine's books to the cloud copy on a clean exit


@app.get("/favicon.ico")
def favicon():
    return FileResponse(BASE / "static" / "favicon.ico")


# Backwards-compatible re-exports: tests (and possibly user scripts) reach into `app.<name>` for
# these; keep them importable from here even though they now live in their domain modules.
import importer  # noqa: E402,F401
import invoicing  # noqa: E402,F401
from staging import staged_receipt_matches, staged_invoice_matches, _post_staged  # noqa: E402,F401
from staging import _watch_statement, _watch_receipt, _categorize_from_receipts  # noqa: E402,F401
from routes_review import _categorize_from_invoices  # noqa: E402,F401
from webutil import categories, ctx, templates  # noqa: E402,F401
from routes_invoices import _match_invoice_to_entry, invoice_deposit_candidates  # noqa: E402,F401
from routes_invoices import _invoice_rows, _reminder_send, _update_document_status  # noqa: E402,F401
