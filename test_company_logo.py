"""Company logo: upload in Settings, stored in the data dir, and shown on invoice PDFs and in the
HTML email body. Covers upload/serve/remove, validation, the invoice embed, and the email parts.
Isolated via SHOPBOOKS_DATA_DIR."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_logo_")

import db  # noqa: E402
db.init()
import app as appmod  # noqa: E402
import invoicing  # noqa: E402
from email.message import EmailMessage  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
client = TestClient(appmod.app)


def con():
    return db.connect()


# ---- Settings page exposes the logo section, initially empty ----
r = client.get("/settings")
ok("Company logo" in r.text and 'action="/settings/logo"' in r.text, "settings page has the logo upload form")
ok("No logo uploaded yet" in r.text, "shows the empty state before any upload")
ok(db.company_logo_path(con()) is None, "company_logo_path is None before upload")

# ---- Upload a real PNG (reuse the bundled app icon) ----
png = open("static/app-icon.png", "rb").read()
r = client.post("/settings/logo", files={"file": ("logo.png", png, "image/png")}, follow_redirects=False)
ok(r.status_code == 303, "upload redirects back to settings")
ok(db.company_logo_path(con()) is not None, "company_logo_path resolves after upload")
r = client.get("/settings/logo")
ok(r.status_code == 200 and r.content[:4] == b"\x89PNG", "GET /settings/logo serves the stored image")
r = client.get("/settings")
ok("/settings/logo" in r.text and "Remove" in r.text, "settings now shows the preview + Remove")

# ---- Non-image is rejected, no crash, no logo stored ----
client.post("/settings/logo/remove")
r = client.post("/settings/logo", files={"file": ("x.png", b"totally not an image", "image/png")},
                follow_redirects=True)
ok("must be a PNG" in r.text, "a non-image upload is rejected with a clear message")
ok(db.company_logo_path(con()) is None, "nothing is stored after a rejected upload")

# ---- Invoice PDF embeds the logo when one is set (and renders fine without) ----
client.post("/settings/logo", files={"file": ("logo.png", png, "image/png")}, follow_redirects=False)
c = con()
db.set_setting(c, "sales_tax_rate", "7.0")
c.execute("INSERT INTO customers(name,email) VALUES('Marcus','m@example.com')")
c.commit()
cid = c.execute("SELECT id FROM customers").fetchone()[0]
c.execute("INSERT INTO invoices(number,customer_id,date,due_date,memo,kind,status) "
          "VALUES('INV-9',?,?,?,?,?,?)", (cid, "2026-07-12", "2026-07-26", "m", "invoice", "sent"))
iid = c.execute("SELECT id FROM invoices WHERE number='INV-9'").fetchone()[0]
c.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,item_id,taxable) "
          "VALUES(?,?,?,?,?,?)", (iid, "Mandolin — A", 1, 320000, None, 1))
c.commit()
inv, items, total = invoicing.get_invoice(c, iid)
pdf_with = invoicing.render_pdf(c, inv, items, total)
db.set_setting(c, "company_logo", ""); c.commit()
pdf_without = invoicing.render_pdf(c, inv, items, total)
ok(pdf_with[:4] == b"%PDF" and len(pdf_with) > len(pdf_without) + 1000,
   f"invoice PDF embeds the logo (+{len(pdf_with) - len(pdf_without)} bytes)")

# ---- Email body gains an HTML alternative with the logo as an inline image ----
db.set_setting(c, "company_logo", "company_logo.png"); c.commit()  # file still present from the upload
msg = EmailMessage()
invoicing._apply_email_body(msg, con(), "Hello\nyour invoice is attached.")
types = [p.get_content_type() for p in msg.walk()]
ok("text/plain" in types and "text/html" in types, "email has both plain-text and HTML parts")
ok(any(t.startswith("image/") for t in types), "email carries the logo as an inline image part")

print("\nCOMPANY LOGO TESTS DONE")
