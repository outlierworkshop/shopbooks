"""The weekly "what did I work on and what got paid" question, and the assistant plumbing behind it.

Three things were in the way of answering "which jobs did I work on and get paid for last week?":
  1. `parse_period` had NO week (or exact-window) support — nothing went narrower than a month;
  2. there was no invoice-level tool at all, so "sent vs paid" was unanswerable;
  3. `jobs_overview` is all-time only, so it couldn't be scoped to a week.
Plus the chat form re-sent the question if a second submit landed while a reply was in flight.

Isolated via SHOPBOOKS_DATA_DIR before importing db."""
import os
import tempfile
from datetime import date

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_weekly_")

import db  # noqa: E402
import chat  # noqa: E402
import insights  # noqa: E402
import invoicing  # noqa: E402
import timetracking  # noqa: E402
from testutil import ok  # noqa: E402

db.init()
con = db.connect()

# --- periods: weeks and exact windows -----------------------------------------------------------
WED = date(2026, 8, 5)          # a Wednesday
s, e, label = insights.parse_period("last-week", WED)
ok((s, e) == ("2026-07-27", "2026-08-02"), f"last-week is the previous Mon-Sun ({s}..{e})")
ok(label == "Last Week", "and is labelled for the reader")
s, e, _ = insights.parse_period("this-week", WED)
ok((s, e) == ("2026-08-03", "2026-08-05"), "this-week runs Monday to today")
s, e, _ = insights.parse_period("last-7-days", WED)
ok((s, e) == ("2026-07-30", "2026-08-05"), "last-7-days is a rolling window including today")
s, e, lab = insights.parse_period("2026-03-01..2026-03-15", WED)
ok((s, e) == ("2026-03-01", "2026-03-15") and "2026-03-01" in lab, "an exact window is accepted")
s, e, _ = insights.parse_period("2026-03-15..2026-03-01", WED)
ok((s, e) == ("2026-03-01", "2026-03-15"), "a backwards window is put the right way round")
try:
    insights.parse_period("last-fortnight", WED)
    ok(False, "junk periods still raise")
except ValueError:
    ok(True, "junk periods still raise")

# --- data: two invoices, one paid inside the window, one outside --------------------------------
bank = con.execute("SELECT id FROM accounts WHERE kind='bank' LIMIT 1").fetchone()["id"]
inc = con.execute("SELECT id FROM accounts WHERE type='income' LIMIT 1").fetchone()["id"]
con.execute("INSERT INTO customers(name,email) VALUES('Lloyd Basses','l@t.local')")
con.execute("INSERT INTO customers(name,email) VALUES('Collin G','c@t.local')")
c1, c2 = [r["id"] for r in con.execute("SELECT id FROM customers ORDER BY id")]


def invoice(num, cust, d, cents):
    con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,memo,kind) "
                "VALUES(?,?,?,?,'sent','','invoice')", (num, cust, d, d))
    iid = con.execute("SELECT id FROM invoices WHERE number=?", (num,)).fetchone()["id"]
    con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,taxable) "
                "VALUES(?,'work',1,?,0)", (iid, cents))
    return iid


i_in = invoice("INV-3001", c1, "2026-07-28", 250000)     # sent inside last week
i_out = invoice("INV-3002", c2, "2026-07-01", 100000)    # sent before it
con.commit()
# INV-3002 (old) is paid DURING last week; INV-3001 is paid after it
invoicing.record_invoice_payment(con, i_out, into_account_id=bank, income_id=inc,
                                 amount_cents=100000, date="2026-07-30")
invoicing.record_invoice_payment(con, i_in, into_account_id=bank, income_id=inc,
                                 amount_cents=250000, date="2026-08-04")
con.commit()

act = insights.invoice_activity(con, "2026-07-27", "2026-08-02")
sent_nums = [x["number"] for x in act["sent"]]
paid_nums = [x["number"] for x in act["paid"]]
ok(sent_nums == ["INV-3001"], f"only the invoice RAISED in the window is 'sent' ({sent_nums})")
ok(paid_nums == ["INV-3002"], f"only the payment RECEIVED in the window is 'paid' ({paid_nums})")
ok(act["sent_total_cents"] == 250000 and act["paid_total_cents"] == 100000,
   "sent and paid totals are tracked separately — they're different questions")
ok(act["paid"][0]["customer"] == "Collin G", "a payment names the customer who paid")

# a PARTIAL payment reports what actually landed, not the invoice face value
i_part = invoice("INV-3003", c1, "2026-07-29", 400000)
con.commit()
invoicing.record_invoice_payment(con, i_part, into_account_id=bank, income_id=inc,
                                 amount_cents=150000, date="2026-07-31")
con.commit()
act2 = insights.invoice_activity(con, "2026-07-27", "2026-08-02")
part = [p for p in act2["paid"] if p["number"] == "INV-3003"][0]
ok(part["amount_cents"] == 150000, "a partial payment reports the amount received, not the total")
outstanding = [x for x in act2["sent"] if x["number"] == "INV-3003"][0]["outstanding_cents"]
ok(outstanding == 250000, "the sent list shows what's still owed on it")

# --- work_by_job: hours worked alongside money received -----------------------------------------
job = timetracking.add_job(con, "Bass plate", customer_id=c1)
timetracking.add_entry(con, "2026-07-28", 6.0, job_id=job, category="milling", note="arch")
timetracking.add_entry(con, "2026-07-30", 3.5, job_id=job, category="milling", note="grads")
timetracking.add_entry(con, "2026-08-04", 8.0, job_id=job, category="milling", note="after the window")
con.commit()

w = insights.work_by_job(con, "2026-07-27", "2026-08-02")
ok(round(w["total_hours"], 2) == 9.5, f"only hours inside the window count ({w['total_hours']})")
ok(any(j["job"] == "Bass plate" and j["hours"] == 9.5 for j in w["jobs"]),
   "hours are broken out per job")
paid_customers = {c["customer"]: c["paid_cents"] for c in w["paid_by_customer"]}
ok(paid_customers.get("Collin G") == 100000 and paid_customers.get("Lloyd Basses") == 150000,
   f"money received is grouped per customer ({paid_customers})")
ok(w["paid_total_cents"] == 250000, "with a period total")

# --- the model must never see raw cents ----------------------------------------------------------
d = chat._to_dollars({"paid_total_cents": 250000, "rows": [{"amount_cents": 1999}], "hours": 9.5})
ok(d["paid_total"] == 2500.0, "a _cents key is converted to dollars AND renamed")
ok("paid_total_cents" not in d, "the raw-cents key is gone, so it can't be misread as dollars")
ok(d["rows"][0]["amount"] == 19.99, "nested _cents values convert too")
ok(d["hours"] == 9.5, "non-money numbers are left alone")

# --- the tools are actually wired up --------------------------------------------------------------
names = {t["name"] for t in chat.TOOLS}
ok({"invoice_activity", "work_by_job"} <= names, "both new tools are offered to the model")
ok(set(chat._HANDLERS) == names, "every advertised tool has a handler (and vice versa)")
ok("last-week" in chat.PERIOD_DESC and "YYYY-MM-DD..YYYY-MM-DD" in chat.PERIOD_DESC,
   "the model is told weeks and exact windows are available")
out = chat._HANDLERS["work_by_job"](con, WED, period="last-week")
ok(out["period"] == "Last Week" and round(out["total_hours"], 2) == 9.5,
   "the work_by_job handler resolves 'last-week' end to end")
out2 = chat._HANDLERS["invoice_activity"](con, WED, period="last-week")
ok(out2["paid_count"] == 2 and out2["sent_count"] == 2,
   "the invoice_activity handler resolves 'last-week' end to end")
# --- who owes me (AR aging) -----------------------------------------------------------------------
owed = chat._HANDLERS["who_owes_me"](con, date(2026, 8, 20))
nums = {i["number"]: i for i in owed["invoices"]}
ok("INV-3003" in nums, "the part-paid invoice shows in receivables")
ok(nums["INV-3003"]["outstanding_cents"] == 250000, "with only the unpaid remainder owing")
ok("INV-3001" not in nums and "INV-3002" not in nums, "fully-paid invoices are not in receivables")
ok(nums["INV-3003"]["days_overdue"] > 0 and nums["INV-3003"]["overdue"],
   "an invoice past its due date is flagged overdue with a day count")
ok(owed["overdue_total_cents"] == 250000, "the overdue total is reported")
ok(all(b["amount_cents"] for b in owed["buckets"]), "empty aging buckets are dropped")

# --- cash forecast ----------------------------------------------------------------------------------
f = chat._HANDLERS["cash_forecast"](con, date(2026, 8, 20), horizon_days=90)
ok(f["horizon_days"] == 90 and "starting_cash_cents" in f, "the forecast reports a starting cash figure")
ok(isinstance(f["months"], list) and all("end_balance_cents" in m for m in f["months"]),
   "each projected month carries an end balance")
ok(isinstance(f["goes_negative"], bool), "it says plainly whether cash goes negative")

# --- books consistency ------------------------------------------------------------------------------
c = insights.books_consistency(con)
ok(c["clean"] is (c["issue_count"] == 0), "'clean' agrees with the issue count")
before = c["issue_count"]

# --- weekly review: one call, everything -------------------------------------------------------------
# (run BEFORE the bad-data fixture below, which lands inside this same week and would skew the counts)
wr = chat._HANDLERS["weekly_review"](con, WED, period="last-week")
ok(wr["period"] == "Last Week", "weekly_review resolves the period")
ok(wr["invoiced_count"] == 2 and wr["collected_count"] == 2,
   "it reports what was invoiced and what was collected")
ok(wr["collected_total_cents"] == 250000, "with the cash actually collected")
ok(round(wr["hours_worked"], 2) == 9.5, "hours worked in the window")
ok("outstanding_total_cents" in wr and "cash_on_hand_cents" in wr,
   "plus receivables and cash, so it stands alone as a weekly report")
d = chat._to_dollars(wr)
ok("collected_total" in d and d["collected_total"] == 2500.0,
   "weekly_review figures reach the model in dollars")
ok(not any(k.endswith("_cents") for k in d), "no raw-cents key survives into the model's view")

# a transfer wearing an invoice: payment entry whose legs are both assets, no income booked.
# This is the real INV-1006 case — Square +$10 / bank -$10 recorded as a sale.
bank2 = con.execute("INSERT INTO accounts(name,kind,type,active) VALUES('Square Clearing','bank','asset',1)").lastrowid
i_fake = invoice("INV-3009", c1, "2026-08-01", 1000)
con.commit()
eid = ledger_post = con.execute(
    "INSERT INTO entries(date,payee,memo) VALUES('2026-08-01','Square payment - INV-3009','test')").lastrowid
con.execute("INSERT INTO splits(entry_id,account_id,amount_cents) VALUES(?,?,1000)", (eid, bank2))
con.execute("INSERT INTO splits(entry_id,account_id,amount_cents) VALUES(?,?,-1000)", (eid, bank))
con.execute("UPDATE invoices SET status='paid', paid_entry_id=? WHERE id=?", (eid, i_fake))
con.commit()

c2r = insights.books_consistency(con)
ok(c2r["issue_count"] > before, "the transfer-as-a-sale is caught")
kinds = " ".join(i["issue"] for i in c2r["issues"])
ok("no income was booked" in kinds, "it explains that no income was booked")
ok(any(i["number"] == "INV-3009" and "still shows a balance" in i["issue"] for i in c2r["issues"]),
   "and that the invoice reads paid while still showing a balance")
ok(c2r["clean"] is False, "the books are not reported clean while that stands")

con.close()

# --- the chat form must not send the same question twice ------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
import routes_reports  # noqa: E402
client = TestClient(appmod.app)

r = client.post("/chat", data={"message": "hello"}, follow_redirects=False)
ok(r.status_code == 303, "POST /chat redirects (so a refresh can't re-send the question)")

routes_reports.CHAT_HISTORY.clear()
routes_reports.CHAT_HISTORY.append({"role": "user", "content": "what did I make last week"})
before = len(routes_reports.CHAT_HISTORY)
r = client.post("/chat", data={"message": "what did I make last week"}, follow_redirects=False)
ok(r.status_code == 303 and len(routes_reports.CHAT_HISTORY) == before,
   "an identical question arriving while the first is still in flight is ignored")

routes_reports.CHAT_HISTORY.clear()
r = client.post("/chat", data={"clear": "1"}, follow_redirects=False)
ok(r.status_code == 303, "clearing still works")
ok(client.get("/chat").status_code == 200, "the chat page renders")

print("\nWEEKLY REPORT TESTS DONE")
