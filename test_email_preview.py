"""Every outgoing email has a preview step: the /preview routes render exactly what will be sent and
put NOTHING on the wire; only confirming (the real send route) actually sends. No network —
invoicing._smtp_send is monkeypatched to count messages. Isolation: SHOPBOOKS_DATA_DIR before db."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_preview_")

import db  # noqa: E402
db.init()
import invoicing  # noqa: E402
import square as sq  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from testutil import ok  # noqa: E402

client = TestClient(appmod.app)
con = db.connect()
db.set_setting(con, "smtp_user", "me@myco.com")
db.set_setting(con, "smtp_password", "app-password")
db.set_setting(con, "business_name", "Outlier Workshop")
cust = con.execute("INSERT INTO customers(name,email) VALUES('Preview Buyer','buyer@example.com')").lastrowid
con.commit()


def make(kind, number, due="2026-08-16"):
    i = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,kind,status) "
                    "VALUES(?,?,?,?,?,'sent')", (number, cust, "2026-07-16", due, kind)).lastrowid
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,taxable) "
                "VALUES(?,'Fabrication work',1,?,0)", (i, 100000))
    con.commit()
    return i


inv = make("invoice", "INV-5001")
est = make("estimate", "EST-5001")

sent = []
invoicing._smtp_send = lambda c, msg: sent.append(msg)
form = {"to_addr": "buyer@example.com", "subject": "", "body": ""}

# --- invoice email: preview sends nothing, confirm sends one --------------------------------
r = client.post(f"/invoices/{inv}/email/preview", data=form)
ok(r.status_code == 200, "invoice email preview renders")
ok(len(sent) == 0, "previewing an invoice email sends NOTHING")
ok("buyer@example.com" in r.text and "Send it now" in r.text,
   "preview shows the recipient and a Send button")
ok(f"/documents/{inv}/email.html" in r.text, "preview embeds the real email body in an iframe")
ok("Nothing has been sent yet" in r.text, "preview says nothing has gone out")

h = client.get(f"/documents/{inv}/email.html")
ok(h.status_code == 200 and "Fabrication work" in h.text and "Total due" in h.text,
   "the preview iframe renders the real invoice email (line items + total)")
ok("/settings/logo" in h.text or "Outlier Workshop" in h.text, "preview renders the branded header")
ok(len(sent) == 0, "rendering the preview body still sends nothing")

r = client.post(f"/invoices/{inv}/email", data=form, follow_redirects=False)
ok(r.status_code == 303 and len(sent) == 1, "confirming sends exactly one email")

# --- quote/estimate ---------------------------------------------------------------------------
sent.clear()
r = client.post(f"/estimates/{est}/email/preview", data=form)
ok(r.status_code == 200 and len(sent) == 0, "previewing a quote email sends nothing")
ok("EST-5001" in r.text, "quote preview names the estimate")
client.post(f"/estimates/{est}/email", data=form, follow_redirects=False)
ok(len(sent) == 1, "confirming sends the quote")

# --- single overdue reminder -------------------------------------------------------------------
sent.clear()
overdue = make("invoice", "INV-5002", due="2026-06-01")
r = client.post(f"/invoices/{overdue}/remind/preview")
ok(r.status_code == 200 and len(sent) == 0, "previewing a reminder sends nothing")
ok("reminder" in r.text.lower(), "the reminder preview is labelled as such")
client.post(f"/invoices/{overdue}/remind", follow_redirects=False)
ok(len(sent) == 1, "confirming sends the reminder")

# --- remind-all: a confirm list, then the bulk send ---------------------------------------------
sent.clear()
overdue2 = make("invoice", "INV-5003", due="2026-06-01")
r = client.post("/invoices/remind-all/preview")
ok(r.status_code == 200 and len(sent) == 0, "the remind-all confirm list sends nothing")
ok("Review overdue reminders" in r.text and "INV-5003" in r.text,
   "the confirm list names who would be reminded")
ok("will send" in r.text, "the list marks who will actually receive one")
client.post("/invoices/remind-all", follow_redirects=False)
ok(len(sent) >= 1, "confirming the list sends the reminders")

# --- Square: creating the pay page must not email; its email previews first ---------------------
sent.clear()
db.set_setting(con, "square_access_token", "tok")
db.set_setting(con, "square_location_id", "LOC")
con.commit()
sq._post = lambda c, p, b: (
    {"customer": {"id": "C1"}} if p == "/v2/customers" else
    {"order": {"id": "O1"}} if p == "/v2/orders" else
    {"invoice": {"id": "SI1", "version": 0, "status": "DRAFT"}} if p == "/v2/invoices" else
    {"invoice": {"id": "SI1", "version": 1, "status": "UNPAID",
                 "public_url": "https://squareup.com/pay/xyz"}})

r = client.post(f"/invoices/{inv}/square-send", follow_redirects=False)
ok(r.status_code == 303 and len(sent) == 0,
   "Collect online creates the payment page and sends NO email on its own")
r = client.post(f"/invoices/{inv}/square-email/preview")
ok(r.status_code == 200 and len(sent) == 0, "previewing the pay-link email sends nothing")
ok("pay_url=https" in r.text, "the pay-link preview carries the real Square URL")
client.post(f"/invoices/{inv}/square-email", follow_redirects=False)
ok(len(sent) == 1, "confirming sends the pay-link email")
html = [p.get_content() for p in sent[0].walk() if p.get_content_type() == "text/html"][0]
ok("Pay here" in html and "squareup.com/pay/xyz" in html, "the sent email carries the Pay-here button")

con.close()
print("\nEMAIL PREVIEW TESTS DONE")
