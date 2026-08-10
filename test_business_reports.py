"""The business-insight reports: what to set aside for tax, which services earn, who the customers
are, what's quoted but unbilled, whether jobs bill out at quote, and where the money goes.

Isolated via SHOPBOOKS_DATA_DIR before importing db."""
import os
import tempfile
from datetime import date

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_reports_")

import db  # noqa: E402
import chat  # noqa: E402
import insights  # noqa: E402
import invoicing  # noqa: E402
import ledger  # noqa: E402
from testutil import ok  # noqa: E402

db.init()
con = db.connect()
TODAY = date(2026, 8, 20)
YEAR = "2026"

bank = con.execute("SELECT id FROM accounts WHERE kind='bank' LIMIT 1").fetchone()["id"]
inc = con.execute("SELECT id FROM accounts WHERE type='income' LIMIT 1").fetchone()["id"]
exp = con.execute("SELECT id FROM accounts WHERE type='expense' LIMIT 1").fetchone()["id"]
fab = con.execute("INSERT INTO accounts(name,kind,type,active) VALUES('Fabrication','category','income',1)").lastrowid
i_mill = con.execute("INSERT INTO items(name,unit_cents,income_account_id,active) "
                     "VALUES('Milling',10000,?,1)", (fab,)).lastrowid
con.execute("INSERT INTO customers(name) VALUES('Big Client')")
con.execute("INSERT INTO customers(name) VALUES('Small Client')")
big, small = [r["id"] for r in con.execute("SELECT id FROM customers ORDER BY id")]
con.commit()

n = [4000]


def inv(cust, d, lines, kind="invoice", status="sent", est_id=None):
    n[0] += 1
    num = ("EST-" if kind == "estimate" else "INV-") + str(n[0])
    con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status,memo,kind,estimate_id) "
                "VALUES(?,?,?,?,?,'',?,?)", (num, cust, d, d, status, kind, est_id))
    iid = con.execute("SELECT id FROM invoices WHERE number=?", (num,)).fetchone()["id"]
    for item_id, desc, qty, cents in lines:
        con.execute("INSERT INTO invoice_items(invoice_id,item_id,description,qty,unit_cents,taxable) "
                    "VALUES(?,?,?,?,?,0)", (iid, item_id, desc, qty, cents))
    con.commit()
    return iid, num


# Big Client: two invoices, both paid quickly. Small Client: one, paid slowly, part-paid.
b1, _ = inv(big, "2026-03-01", [(i_mill, "Milling", 5, 20000)])          # 1,000.00
b2, _ = inv(big, "2026-04-01", [(None, "Design work", 1, 50000)])        #   500.00
s1, _ = inv(small, "2026-03-01", [(i_mill, "Milling", 1, 20000)])        #   200.00
invoicing.record_invoice_payment(con, b1, into_account_id=bank, income_id=fab,
                                 amount_cents=100000, date="2026-03-05")
invoicing.record_invoice_payment(con, b2, into_account_id=bank, income_id=fab,
                                 amount_cents=50000, date="2026-04-03")
invoicing.record_invoice_payment(con, s1, into_account_id=bank, income_id=fab,
                                 amount_cents=5000, date="2026-03-31")
con.commit()

# --- service lines -------------------------------------------------------------------------------
sl = insights.service_lines(con, "2026-01-01", "2026-12-31")
by = {s["service"]: s for s in sl["services"]}
ok(sl["total_revenue_cents"] == 170000, "service revenue totals the invoiced work")
ok(by["Milling"]["revenue_cents"] == 120000, "a catalog item groups across invoices")
ok(by["Milling"]["income_account"] == "Fabrication", "and shows the income account it posts to")
ok(by["Milling"]["quantity"] == 6 and by["Milling"]["avg_price_cents"] == 20000,
   "with quantity and an average price")
ok("Design work" in by, "a hand-typed line still appears (grouped by its description)")
ok(by["Milling"]["share_pct"] > by["Design work"]["share_pct"], "shares rank the services")
ok(abs(sum(s["share_pct"] for s in sl["services"]) - 100) < 0.5, "shares add up to ~100%")

# --- customer scorecard --------------------------------------------------------------------------
cs = insights.customer_scorecard(con, "2026-01-01", "2026-12-31")
cm = {c["customer"]: c for c in cs["customers"]}
ok(cm["Big Client"]["invoiced_cents"] == 150000, "invoiced per customer")
ok(cm["Big Client"]["collected_cents"] == 150000, "collected per customer")
ok(cm["Small Client"]["outstanding_cents"] == 15000, "and what's still owed")
ok(cs["customers"][0]["customer"] == "Big Client", "ranked by money collected")
ok(cm["Big Client"]["share_of_collected_pct"] > 90, "revenue concentration is surfaced")
ok("Big Client" in (cs["concentration_note"] or ""), "with a plain-language note naming the risk")
ok(cm["Big Client"]["avg_days_to_pay"] == 3.0, "average days-to-pay is computed from the payments")
ok(cm["Small Client"]["avg_days_to_pay"] == 30.0, "a slow payer shows a longer figure")

# --- pipeline ------------------------------------------------------------------------------------
e_sent, _ = inv(big, "2026-05-01", [(i_mill, "Milling", 10, 20000)], kind="estimate", status="sent")
e_acc, e_acc_num = inv(big, "2026-05-02", [(i_mill, "Milling", 10, 20000)],
                       kind="estimate", status="accepted")
inv(big, "2026-05-10", [(None, "50% deposit", 1, 100000)], est_id=e_acc)   # half of 2,000
con.commit()
p = insights.pipeline(con)
pn = {r["number"]: r for r in p["estimates"]}
ok(p["open_quoted_cents"] == 200000, "quotes still out are totalled")
ok(p["accepted_unbilled_cents"] == 100000, "and accepted-but-unbilled work is separated from them")
ok(pn[e_acc_num]["billed_cents"] == 100000 and pn[e_acc_num]["remaining_cents"] == 100000,
   "an accepted estimate shows billed vs left to bill")

# --- quote accuracy ------------------------------------------------------------------------------
qa = insights.quote_accuracy(con)
ok(qa["in_progress"] == 1, "a part-billed job is counted as in progress, not as a shortfall")
ok(qa["compared"] == 0 and qa["variance_pct"] == 0.0,
   "so it does NOT drag the headline variance (progress billing isn't under-quoting)")
inv(big, "2026-06-01", [(None, "balance", 1, 110000)], est_id=e_acc)   # over-bill the remainder
con.commit()
qa2 = insights.quote_accuracy(con)
done = [r for r in qa2["estimates"] if r["fully_billed"]]
ok(qa2["compared"] == 1 and done, "once fully billed it counts towards accuracy")
ok(qa2["variance_cents"] == 10000 and qa2["variance_pct"] == 5.0,
   "billing $100 over a $2,000 quote reads as +5%")

# --- vendor spend --------------------------------------------------------------------------------
ledger.post_entry(con, "2026-02-01", "McMaster-Carr", [(exp, 30000), (bank, -30000)])
ledger.post_entry(con, "2026-02-08", "McMaster-Carr", [(exp, 20000), (bank, -20000)])
ledger.post_entry(con, "2026-02-09", "Small Supplier", [(exp, 5000), (bank, -5000)])
con.commit()
vs = insights.vendor_spend(con, "2026-01-01", "2026-12-31")
ok(vs["vendors"][0]["vendor"] == "McMaster-Carr", "vendors rank by spend")
ok(vs["vendors"][0]["spend_cents"] == 50000, "spend is summed across their transactions")
ok(vs["vendors"][0]["transactions"] == 2, "with a transaction count")
ok(vs["total_spend_cents"] == 55000, "and a period total")

# --- tax position --------------------------------------------------------------------------------
con.execute("INSERT INTO mileage(date,miles,purpose,business) VALUES('2026-03-01',100,'job',1)")
con.execute("INSERT INTO mileage(date,miles,purpose,business) VALUES('2026-03-02',40,'personal',0)")
con.commit()
tp = insights.tax_position(con, 2026, today=TODAY)
ok(tp["business_miles"] == 100 and tp["total_miles"] == 140, "business and total miles both shown")
ok(tp["mileage_deduction_cents"] == int(round(100 * tp["mileage_rate"] * 100)),
   "the deduction uses BUSINESS miles at the configured rate")
ok(isinstance(tp["quarters"], list) and tp["quarters"], "estimated tax is broken out by quarter")
ok(all("due_date" in q and "total_due_cents" in q for q in tp["quarters"]),
   "each quarter carries its due date and what's owed")
ok("sales_tax_owed_cents" in tp, "sales tax held for the state is reported separately from income")

# --- every figure reaches the model as dollars, never raw cents ----------------------------------
for name in ("tax_position", "service_lines", "customer_scorecard", "pipeline",
             "quote_accuracy", "vendor_spend"):
    raw = chat._HANDLERS[name](con, TODAY)
    conv = chat._to_dollars(raw)

    def leftover(o):
        if isinstance(o, dict):
            return any(k.endswith("_cents") for k in o) or any(leftover(v) for v in o.values())
        if isinstance(o, list):
            return any(leftover(x) for x in o)
        return False

    ok(not leftover(conv), f"{name}: no raw-cents key survives into the model's view")

names = {t["name"] for t in chat.TOOLS}
ok({"tax_position", "service_lines", "customer_scorecard", "pipeline", "quote_accuracy",
    "vendor_spend"} <= names, "all six reports are offered to the assistant")
ok(set(chat._HANDLERS) == names, "every advertised tool has a handler (and vice versa)")
con.close()
print("\nBUSINESS REPORTS TESTS DONE")
