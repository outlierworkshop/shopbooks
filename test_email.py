"""Invoice email: the test-email helper, SMTP error translation, and the /email/test route.
No network — invoicing._smtp_send is monkeypatched to capture the message or raise canned errors.
Isolation: SHOPBOOKS_DATA_DIR -> temp dir BEFORE importing db (mandatory)."""
import os
import smtplib
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_email_")

import db          # noqa: E402
import invoicing   # noqa: E402
from testutil import ok  # noqa: E402

db.init()
con = db.connect()

# --- not configured -----------------------------------------------------------
try:
    invoicing.send_test_email(con)
    ok(False, "send_test_email should refuse when SMTP isn't set up")
except RuntimeError as e:
    ok("set up" in str(e).lower(), "unconfigured send_test_email raises a plain 'set up' error")

# --- configured: builds a self-addressed message and calls _smtp_send ---------
db.set_setting(con, "smtp_user", "me@myco.com")
db.set_setting(con, "smtp_password", "abcd efgh ijkl mnop")
db.set_setting(con, "business_name", "Outlier Workshop")
con.commit()

captured = {}
_orig = invoicing._smtp_send
# Emails are multipart now (plain text + branded HTML, with the company logo inline when set),
# so pull the plain-text part rather than msg.get_content() (which raises on a multipart message).
invoicing._smtp_send = lambda c, msg: captured.update(
    {"from": msg["From"], "to": msg["To"], "subject": msg["Subject"],
     "body": msg.get_body(preferencelist=("plain",)).get_content(),
     "has_html": msg.get_body(preferencelist=("html",)) is not None})
try:
    to = invoicing.send_test_email(con)
    ok(to == "me@myco.com", "send_test_email returns the address it sent to")
    ok(captured["to"] == "me@myco.com" and captured["from"] == "me@myco.com",
       "test email is self-addressed (from and to the SMTP user)")
    ok(captured["subject"] == "ShopBooks test email", "test email has the expected subject")
    ok("Outlier Workshop" in captured["body"], "test email body names the business")
    ok(captured["has_html"], "test email includes an HTML alternative part")
finally:
    invoicing._smtp_send = _orig

# --- error translation --------------------------------------------------------
auth = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
msg_auth = invoicing.explain_smtp_error(auth)
ok("App Password" in msg_auth and "2-Step" in msg_auth,
   "auth failure -> App Password / 2-Step Verification guidance")
ok("App Password" in invoicing.explain_smtp_error(Exception("535 5.7.8 nope")),
   "a bare 535 string is also recognized as an auth failure")
ok("reach the mail server" in invoicing.explain_smtp_error(TimeoutError("timed out")),
   "connection/timeout -> host/port guidance")
ok(invoicing.explain_smtp_error(ValueError("weird")) == "Email failed: weird",
   "unknown errors fall through to a generic message")

# --- /email/test route (real SMTP send stubbed) -------------------------------
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
client = TestClient(appmod.app)

# success path: stub the send, expect the 'sent' message on the redirect
invoicing._smtp_send = lambda c, msg: None
r = client.post("/email/test", follow_redirects=False)
ok(r.status_code == 303 and "msg=" in r.headers["location"] and "me%40myco.com" in r.headers["location"],
   "POST /email/test success redirects with a 'sent to <user>' message")

# failure path: stub raises an auth error, expect the friendly err
invoicing._smtp_send = lambda c, msg: (_ for _ in ()).throw(
    smtplib.SMTPAuthenticationError(535, b"5.7.8 nope"))
r = client.post("/email/test", follow_redirects=False)
ok(r.status_code == 303 and "err=" in r.headers["location"],
   "POST /email/test failure redirects with an err= message")
invoicing._smtp_send = _orig

# --- rich invoice email: HTML mirrors the PDF, with a Pay button when a pay link is given ------
cust = con.execute("INSERT INTO customers(name,email) VALUES('Rich Buyer','rb@x.com')").lastrowid
inv_id = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,kind) "
                     "VALUES('INV-7001',?,?,?,'sent','invoice')", (cust, '2026-07-14', '2026-08-14')).lastrowid
con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,taxable) "
            "VALUES(?, 'Custom bracket', 2, 5000, 0)", (inv_id,))
con.commit()
inv, items, total = invoicing.get_invoice(con, inv_id)

cap = {}
invoicing._smtp_send = lambda c, msg: cap.update(
    {"plain": msg.get_body(preferencelist=("plain",)).get_content(),
     "html": msg.get_body(preferencelist=("html",)).get_content()})
try:
    invoicing.send_invoice_email(con, inv, total, b"%PDF-fake", "rb@x.com",
                                 pay_url="https://squareup.com/pay/ABC")
    ok("Custom bracket" in cap["html"], "invoice email HTML lists the line item")
    ok("Total due" in cap["html"] and "100.00" in cap["html"], "invoice email HTML shows the total")
    ok("Pay here" in cap["html"] and "squareup.com/pay/ABC" in cap["html"],
       "a Pay-here button links to the Square page when a pay_url is given")
    ok("squareup.com/pay/ABC" in cap["plain"], "plain-text fallback still includes the pay link")
    cap.clear()
    invoicing.send_invoice_email(con, inv, total, b"%PDF-fake", "rb@x.com")   # no pay link
    ok("Pay here" not in cap["html"], "no Pay button when there's no pay link")
finally:
    invoicing._smtp_send = _orig

con.close()
print("\nEMAIL TESTS DONE")
