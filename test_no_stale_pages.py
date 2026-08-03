"""Pages must never be served to the browser from cache — the books change constantly.

Regression: HTML went out with NO cache headers at all, so browsers applied heuristic caching and
could re-show a page without revalidating (and back/forward restored it verbatim from the bfcache).
A paid invoice therefore kept reading "overdue" until the app was restarted, even though the ledger
was already correct. `app._no_stale_pages` now stamps `no-store` on HTML and PDF responses.

Isolated via SHOPBOOKS_DATA_DIR before importing db/app."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_nostale_")

import db  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from testutil import ok  # noqa: E402

client = TestClient(appmod.app)


def cc(resp):
    return resp.headers.get("cache-control", "")


# --- every HTML page the books are read from must be no-store -----------------------------------
for path in ("/", "/invoices", "/customers", "/checks", "/reports", "/settings"):
    r = client.get(path)
    ok(r.status_code == 200, f"{path} renders")
    ok("no-store" in cc(r), f"{path} is sent no-store (a stale page can't show stale books)")

# --- PDFs too: their URL doesn't change when the underlying data (or print alignment) does -------
con = db.connect()
bank = con.execute("SELECT id FROM accounts WHERE kind='bank' LIMIT 1").fetchone()["id"]
cat = con.execute("SELECT id FROM accounts WHERE type='expense' LIMIT 1").fetchone()["id"]
con.close()
pdf = client.get("/checks/preview.pdf", params={"account_id": bank, "payee_name": "Someone",
                 "date": "2026-08-03", "amount": "10.00", "category_id": cat, "check_number": 900})
ok(pdf.status_code == 200 and pdf.content[:4] == b"%PDF", "the check preview PDF renders")
ok("no-store" in cc(pdf),
   "PDFs are no-store, so re-previewing after a print-alignment nudge isn't served from cache")

# --- static assets are deliberately still cacheable (they're cache-busted by mtime) --------------
css = client.get("/static/style.css")
ok(css.status_code == 200, "the stylesheet serves")
ok("no-store" not in cc(css), "static assets stay cacheable — only pages/PDFs are no-store")

print("\nNO-STALE-PAGES TESTS DONE")
