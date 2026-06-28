"""Tests for AR aging + overdue reminders (#36). Isolated via SHOPBOOKS_DATA_DIR.

ar_aging() is pure/deterministic (figures from the line items). The reminder helper is exercised
with SMTP + PDF stubbed out, so no email is sent and no network is touched — we only verify the
dispatch/skip/stamp logic.
"""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_artest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db          # noqa: E402
import invoicing   # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)

db.init()
con = db.connect()
con.execute("INSERT INTO customers(id,name,email) VALUES(1,'WithEmail','pay@cust.test')")
con.execute("INSERT INTO customers(id,name,email) VALUES(2,'NoEmail','')")
nid = [0]


def inv(due, status="sent", amount=10000, kind="invoice", customer=1):
    nid[0] += 1
    num = f"{kind[:3].upper()}-{1000 + nid[0]}"
    cur = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,kind) VALUES(?,?,?,?,?,?)",
                      (num, customer, "2026-01-01", due, status, kind))
    iid = cur.lastrowid
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents) VALUES(?,?,1,?)",
                (iid, "work", amount))
    return iid


TODAY = "2026-06-15"
a = inv("2026-06-20", amount=10000)              # current
b = inv("2026-06-10", amount=20000)              # 5 days -> 1-30
c = inv("2026-05-01", amount=30000)              # ~45 days -> 31-60
d = inv("2026-03-01", amount=40000)              # ~106 days -> 90+
inv("2026-01-01", status="draft", amount=99900)  # excluded (not sent)
inv("2026-01-01", status="paid", amount=88800)   # excluded (paid)
inv("2026-01-01", status="sent", amount=77700, kind="estimate")  # excluded (estimate)
h = inv("2026-05-01", amount=5000, customer=2)    # overdue but customer has no email
con.commit()

# --- aging buckets + totals ---------------------------------------------------
ag = invoicing.ar_aging(con, TODAY)
ok(ag["total"] == 105000, "total outstanding = 100+200+300+400+50 = $1,050 (drafts/paid/estimates excluded)")
ok(ag["buckets"]["current"] == 10000, "current bucket = $100")
ok(ag["buckets"]["1-30"] == 20000, "1-30 bucket = $200")
ok(ag["buckets"]["31-60"] == 35000, "31-60 bucket = $300 + $50 = $350")
ok(ag["buckets"]["61-90"] == 0, "61-90 bucket empty")
ok(ag["buckets"]["90+"] == 40000, "90+ bucket = $400")
ok(ag["overdue_total"] == 95000 and ag["overdue_count"] == 4, "overdue = everything but the current one")
ok(ag["open_count"] == 5, "five open invoices counted")
nums = {r["id"] for r in ag["rows"]}
ok(a in nums and b in nums and h in nums, "sent invoices appear")
ok(len(ag["rows"]) == 5, "draft/paid/estimate rows are not in the aging list")
bi = next(r for r in ag["rows"] if r["id"] == b)
ok(bi["days_overdue"] == 5 and bi["overdue"], "days-overdue computed per invoice")

# --- reminder dispatch (SMTP + PDF stubbed) -----------------------------------
import app  # noqa: E402
sent_log = []
invoicing.render_pdf = lambda con, i, items, total: b"%PDF-stub"
invoicing.send_invoice_email = lambda con, i, total, pdf, to, subj=None, body=None: sent_log.append(to)

ok(app._reminder_send(con, b, today=TODAY) == "sent", "an overdue invoice with an email -> sent")
con.commit()
ok(sent_log == ["pay@cust.test"], "the reminder went to the customer's email")
ok(con.execute("SELECT last_reminder_date FROM invoices WHERE id=?", (b,)).fetchone()[0] == TODAY,
   "last_reminder_date is stamped")
ok(app._reminder_send(con, b, skip_days=7, today=TODAY) == "skipped",
   "skip_days suppresses a second reminder within the window")
ok(app._reminder_send(con, b, skip_days=7, today="2026-06-25") == "sent",
   "after the window, a reminder sends again")
ok(app._reminder_send(con, h, today=TODAY) == "no_email", "an invoice whose customer has no email -> no_email")

draft_id = con.execute("SELECT id FROM invoices WHERE status='draft'").fetchone()[0]
ok(app._reminder_send(con, draft_id, today=TODAY) == "skipped", "a non-sent invoice is never reminded")
est_id = con.execute("SELECT id FROM invoices WHERE kind='estimate'").fetchone()[0]
ok(app._reminder_send(con, est_id, today=TODAY) == "skipped", "an estimate is never reminded")

con.close()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nAR AGING TESTS DONE")
