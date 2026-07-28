"""Importing a statement SCREENSHOT (PNG/JPG): read by AI vision into the Review queue, so you can
fill gaps a downloadable statement doesn't cover. Covers the vision path end-to-end, the AI-off hard
stop (there's no text to regex), unreadable images, and temp-file cleanup. No network — ai.available
/ ai.extract_statement_image are stubbed. Isolated via SHOPBOOKS_DATA_DIR."""
import io
import os
import tempfile
from pathlib import Path

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_imgimport_")

import ai  # noqa: E402
import db  # noqa: E402
db.init()
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
client = TestClient(appmod.app)

PNG = open("static/app-icon.png", "rb").read()          # a real image to upload
acct_id = db.connect().execute(
    "SELECT id FROM accounts WHERE kind IN ('bank','card') ORDER BY id LIMIT 1").fetchone()["id"]


def upload(name="screenshot.png", blob=PNG, **data):
    return client.post("/import", files={"file": (name, io.BytesIO(blob), "image/png")},
                       data=data, follow_redirects=False)


# ---- with AI off, an image is a hard stop (no text to fall back on) ----
_avail, _extract = ai.available, ai.extract_statement_image
ai.available = lambda con: False
r = upload()
ok(r.status_code == 200 and "needs AI" in r.text,
   "an image upload with AI off explains that vision is required (no silent failure)")
ok(not list(db.DOCS.glob("temp_stmt_*")), "the rejected screenshot isn't left behind in docs/")

# ---- an unsupported type still tells you what's accepted, now including images ----
ai.available = lambda con: True
r = client.post("/import", files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
                follow_redirects=False)
ok(r.status_code == 200 and "image" in r.text.lower(), "an unsupported file names images as an option")

# ---- vision reads the screenshot -> confirm screen with the transactions ----
ai.extract_statement_image = lambda con, path, account_name: [
    {"date": "2026-03-04", "description": "LOWES #1234", "amount": 84.10},
    {"date": "2026-03-05", "description": "PAYMENT THANK YOU", "amount": -200.00},
]
r = upload()
ok(r.status_code == 200 and "LOWES #1234" in r.text, "the screenshot's transactions reach the confirm screen")
ok("double-check dates" in r.text, "the confirm screen warns that a screen grab has no year")

# ---- confirm stages them into Review ----
import json  # noqa: E402
import re  # noqa: E402
tmp_path = re.search(r'name="temp_file_path" value="([^"]+)"', r.text).group(1)
txns_json = json.loads(re.search(r'name="txns_json" value=\'([^\']+)\'', r.text).group(1)) \
    if re.search(r'name="txns_json" value=\'([^\']+)\'', r.text) else None
ok(Path(tmp_path).exists() and "temp_stmt_" in tmp_path, "the screenshot is held as a temp working file")

r2 = client.post("/import/confirm", data={
    "filename": "screenshot.png", "temp_file_path": tmp_path, "account_id": str(acct_id),
    "txns_json": json.dumps([
        {"date": "2026-03-04", "description": "LOWES #1234", "amount_cents": 8410},
        {"date": "2026-03-05", "description": "PAYMENT THANK YOU", "amount_cents": -20000},
    ]), "note": ""}, follow_redirects=False)
ok(r2.status_code == 303, "confirming an image import redirects to Review")
staged = db.connect().execute(
    "SELECT description, amount_cents FROM staged WHERE status='pending' ORDER BY date").fetchall()
ok([s["description"] for s in staged] == ["LOWES #1234", "PAYMENT THANK YOU"],
   "both screenshot rows are staged for review")
ok([s["amount_cents"] for s in staged] == [8410, -20000],
   "money-out stays positive and money-in negative (staged sign convention)")
ok(not Path(tmp_path).exists(), "the temp screenshot is cleaned up after import")

# ---- an unreadable image says so instead of importing nothing ----
ai.extract_statement_image = lambda con, path, account_name: []
r = upload("blurry.png")
ok(r.status_code == 200 and "read any transactions" in r.text,   # apostrophe is HTML-escaped
   "an unreadable screenshot gets a helpful message")
ok(not list(db.DOCS.glob("temp_stmt_*")), "the unreadable screenshot is cleaned up too")

# ---- the real dispatcher is AI-optional: no key -> None (never raises) ----
ai.available, ai.extract_statement_image = _avail, _extract
ok(ai.extract_statement_image(db.connect(), "nope.png", "Checking") is None,
   "extract_statement_image returns None when AI is unavailable (AI-optional invariant)")

print("\nIMAGE STATEMENT IMPORT TESTS DONE")
