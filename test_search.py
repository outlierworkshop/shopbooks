"""Global search tests. Isolation: SHOPBOOKS_DATA_DIR -> temp dir BEFORE importing db (mandatory)."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="searchtest_")

import db        # noqa: E402
import ledger    # noqa: E402
import search    # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)

db.init()
con = db.connect()


def acct(name, kind, typ):
    return con.execute("INSERT INTO accounts(name,kind,type,active) VALUES(?,?,?,1)", (name, kind, typ)).lastrowid


chk = acct("Checking", "bank", "asset")
mat = acct("Materials", "category", "expense")
cust = con.execute("INSERT INTO customers(name,email,phone) VALUES('Acme Widgets','a@acme.com','555-1212')").lastrowid
# transaction: HOME DEPOT $45.00
ledger.post_entry(con, "2026-02-01", "HOME DEPOT #4", [(mat, 4500), (chk, -4500)], memo="lumber")
# invoice INV-1001 total $123.00
inv = con.execute("INSERT INTO invoices(number,customer_id,date,due_date,status) VALUES('INV-1001',?,?,?,'sent')",
                  (cust, "2026-02-02", "2026-03-02")).lastrowid
con.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,taxable) VALUES(?,?,?,?,0)",
            (inv, "Bench work", 1, 12300))
# receipt: Staples $45.00
con.execute("INSERT INTO documents(filename,path,vendor,doc_date,amount_cents) VALUES('r.jpg','/x','Staples','2026-02-01',4500)")
# staged (pending): AMAZON $45.00
bid = con.execute("INSERT INTO batches(account_id,filename) VALUES(?,'s.csv')", (chk,)).lastrowid
con.execute("INSERT INTO staged(batch_id,date,description,amount_cents,status) VALUES(?,?,?,?, 'pending')",
            (bid, "2026-02-03", "AMAZON MARKETPLACE", 4500))
job = con.execute("INSERT INTO jobs(name,status) VALUES('Bench build','active')").lastrowid
con.execute("INSERT INTO mileage(date,miles,purpose,from_loc,to_loc) VALUES('2026-02-01',12.5,'Client visit','Shop','Site')")
con.commit()

# --- amount search: 45.00 -> transaction + receipt + staged ---
r = search.run(con, "45.00")
ok(any(t["payee"] == "HOME DEPOT #4" for t in r["transactions"]), "amount 45.00 finds the transaction")
ok(r["transactions"][0]["acct"] == "Checking" and r["transactions"][0]["acct_id"] == chk,
   "transaction links to the bank/card leg (Checking)")
ok(any(d["vendor"] == "Staples" for d in r["receipts"]), "amount 45.00 finds the Staples receipt")
ok(any(s["description"] == "AMAZON MARKETPLACE" for s in r["review"]), "amount 45.00 finds the staged line")
ok(r["total"] >= 3, "amount search totals the groups")

# --- amount search over invoices (computed total) ---
r = search.run(con, "123.00")
ok(any(i["number"] == "INV-1001" for i in r["invoices"]), "amount 123.00 finds invoice by computed total")

# --- text searches ---
ok(any(t["payee"] == "HOME DEPOT #4" for t in search.run(con, "home depot")["transactions"]),
   "payee text search is case-insensitive")
ok(any(c["name"] == "Acme Widgets" for c in search.run(con, "acme")["customers"]), "customer name search")
ok(any(i["number"] == "INV-1001" for i in search.run(con, "INV-1001")["invoices"]), "invoice number search")
ok(any(a["name"] == "Checking" for a in search.run(con, "check")["accounts"]), "account name search")
ok(any(j["name"] == "Bench build" for j in search.run(con, "bench")["jobs"]), "job name search")
ok(any(m["purpose"] == "Client visit" for m in search.run(con, "client")["mileage"]), "mileage purpose search")

# --- empty + no-match + injection safety ---
ok(search.run(con, "")["total"] == 0, "empty query returns nothing")
ok(search.run(con, "zzz-no-such-thing")["total"] == 0, "no-match query returns nothing")
ok(search.run(con, "o'brien %_")["total"] == 0, "quote/wildcard query is safe and matches nothing")

con.close()
import shutil  # noqa: E402
shutil.rmtree(os.environ["SHOPBOOKS_DATA_DIR"], ignore_errors=True)
print("\nSEARCH TESTS DONE")
