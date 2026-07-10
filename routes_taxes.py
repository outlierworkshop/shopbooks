"""Tax planner, estimated payments, year-end close, tax package routes."""
import io
import zipfile
from datetime import date as date_cls
from pathlib import Path
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

import db
import insights
import ledger
from webutil import _write_account_section, ctx, get_con, safe_redirect, templates

router = APIRouter()

@router.get("/taxes", response_class=HTMLResponse)
def taxes_page(request: Request, year: int = 0, msg: str = "", err: str = "", con=Depends(get_con)):
    year = year or date_cls.today().year
    start, end = f"{year}-01-01", f"{year}-12-31"
    p = ledger.pnl(con, start, end)
    rate = float(db.get_setting(con, "mileage_rate", "0.70"))
    miles = con.execute("SELECT COALESCE(SUM(miles),0) m FROM mileage WHERE date BETWEEN ? AND ?",
                        (start, end)).fetchone()["m"]
    uncat = con.execute(
        "SELECT COUNT(DISTINCT e.id) c FROM entries e JOIN splits s ON s.entry_id=e.id "
        "JOIN accounts a ON a.id=s.account_id WHERE a.name='Uncategorized Expense' "
        "AND e.date BETWEEN ? AND ?", (start, end)).fetchone()["c"]
    pending = con.execute("SELECT COUNT(*) c FROM staged WHERE status='pending'").fetchone()["c"]
    receipts_matched = con.execute(
        "SELECT COUNT(*) c FROM documents d JOIN entries e ON e.id=d.entry_id "
        "WHERE d.status='matched' AND e.date BETWEEN ? AND ?", (start, end)).fetchone()["c"]
    receipts_unmatched = con.execute("SELECT COUNT(*) c FROM documents WHERE status='unmatched'").fetchone()["c"]
    missing_receipts = len(insights.missing_receipts(con, start, end))

    # Get estimated income tax rate
    est_rate_str = db.get_setting(con, "estimated_income_tax_rate", "15")
    try:
        est_rate = float(est_rate_str)
    except ValueError:
        est_rate = 15.0

    # Calculate tax details
    sch_c = insights.schedule_c_report(con, start, end)
    est_tx = insights.estimated_taxes(con, year, est_rate)

    tax_payments = con.execute(
        "SELECT * FROM tax_payments WHERE year=? ORDER BY date DESC, id DESC", (year,)).fetchall()
    return templates.TemplateResponse(request, "taxes.html", ctx(
        request, con, year=year, pnl=p, miles=miles, rate=rate,
        mileage_deduction=round(miles * rate * 100), uncat=uncat, pending=pending,
        receipts_matched=receipts_matched, receipts_unmatched=receipts_unmatched,
        missing_receipts=missing_receipts,
        schedule_c=sch_c, estimated_taxes=est_tx, tax_payments=tax_payments,
        estimated_income_tax_rate=est_rate_str,
        locked_through=ledger.locked_through(con), msg=msg, err=err))

@router.post("/taxes/payment")
def taxes_payment_add(year: int = Form(...), quarter: str = Form(...), date: str = Form(...),
                      amount: str = Form(...), memo: str = Form(""), con=Depends(get_con)):
    """Record an estimated-tax payment actually made (1040-ES). Keyed to the TAX year+quarter —
    Q4 is typically paid in January of the following calendar year."""
    back = f"/taxes?year={year}"
    if quarter not in ("Q1", "Q2", "Q3", "Q4"):
        return safe_redirect(back, err="Pick a quarter.")
    try:
        d = ledger.normalize_date(date)
        cents = abs(ledger.parse_amount_to_cents(amount))
        if cents == 0:
            raise ValueError("amount is zero")
    except ValueError as e:
        return safe_redirect(back, err=f"Couldn't read that: {e}")
    con.execute("INSERT INTO tax_payments(year,quarter,date,amount_cents,memo) VALUES(?,?,?,?,?)",
                (year, quarter, d, cents, memo.strip()))
    con.commit()
    return safe_redirect(back, msg=f"Recorded ${ledger.fmt_cents(cents)} toward {year} {quarter}.")

@router.post("/taxes/payment/{payment_id}/delete")
def taxes_payment_delete(payment_id: int, year: int = Form(...), con=Depends(get_con)):
    con.execute("DELETE FROM tax_payments WHERE id=?", (payment_id,))
    con.commit()
    return RedirectResponse(f"/taxes?year={year}", status_code=303)

@router.post("/taxes/close")
def taxes_close(through: str = Form(...), con=Depends(get_con)):
    try:
        d = ledger.normalize_date(through)
    except ValueError:
        return safe_redirect("/taxes", err="Enter a valid date to close the books through.")
    db.set_setting(con, "books_locked_through", d)
    con.commit()
    return safe_redirect("/taxes", msg=f"Books closed through {d}. Transactions on or before that date are now locked.")

@router.post("/taxes/reopen")
def taxes_reopen(con=Depends(get_con)):
    db.set_setting(con, "books_locked_through", "")
    con.commit()
    return safe_redirect("/taxes", msg="Books reopened — every period is editable again.")

@router.post("/taxes/settings")
def taxes_save_settings(estimated_income_tax_rate: str = Form(...), con=Depends(get_con)):
    try:
        rate = float(estimated_income_tax_rate.strip())
        if rate < 0 or rate > 100:
            raise ValueError()
    except ValueError:
        return safe_redirect("/taxes", err="Tax rate must be a number between 0 and 100.")
    db.set_setting(con, "estimated_income_tax_rate", str(rate))
    con.commit()
    return RedirectResponse("/taxes", status_code=303)

@router.get("/taxes/package.zip")
def tax_package(year: int, con=Depends(get_con)):
    start, end = f"{year}-01-01", f"{year}-12-31"
    csvmod = __import__("csv")

    def make_csv(write_rows):
        buf = io.StringIO()
        write_rows(csvmod.writer(buf))
        return buf.getvalue()

    p = ledger.pnl(con, start, end)
    bs = ledger.balance_sheet(con, end)
    sch_c = insights.schedule_c_report(con, start, end)
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        def pnl_rows(w):
            w.writerow(["Profit & Loss", f"{start} to {end}"]); w.writerow([])
            w.writerow(["INCOME"]); _write_account_section(w, p["income"])
            w.writerow(["Total Income", f"{p['total_income']/100:.2f}"]); w.writerow([])
            w.writerow(["EXPENSES"]); _write_account_section(w, p["expenses"])
            w.writerow(["Total Expenses", f"{p['total_expenses']/100:.2f}"]); w.writerow([])
            w.writerow(["Net Profit", f"{p['net']/100:.2f}"])
        z.writestr(f"{year}_profit_and_loss.csv", make_csv(pnl_rows))

        def schedule_c_rows(w):
            w.writerow(["IRS Schedule C Mapping Report", f"{start} to {end}"]); w.writerow([])
            w.writerow(["INCOME"])
            for item in sch_c["income"]:
                w.writerow([item["line"], f"{item['amount']/100:.2f}"])
                for acct in item["accounts"]:
                    w.writerow([f"  {acct['name']}", f"{acct['amount']/100:.2f}"])
            w.writerow(["Total Schedule C Income", f"{sch_c['total_income']/100:.2f}"]); w.writerow([])
            w.writerow(["EXPENSES"])
            for item in sch_c["expenses"]:
                w.writerow([item["line"], f"{item['amount']/100:.2f}"])
                for acct in item["accounts"]:
                    w.writerow([f"  {acct['name']}", f"{acct['amount']/100:.2f}"])
            w.writerow(["Total Schedule C Expenses", f"{sch_c['total_expenses']/100:.2f}"]); w.writerow([])
            w.writerow(["Net Schedule C Profit/Loss", f"{sch_c['net']/100:.2f}"])
            if sch_c["unmapped"]:
                w.writerow([])
                w.writerow(["UNMAPPED CATEGORIES (WARNING)"])
                for acct in sch_c["unmapped"]:
                    w.writerow([acct["name"], f"{acct['balance']/100:.2f}"])
        z.writestr(f"{year}_schedule_c.csv", make_csv(schedule_c_rows))

        def bs_rows(w):
            w.writerow(["Balance Sheet", f"as of {end}"]); w.writerow([])
            for section, items_, tot in (("ASSETS", bs["assets"], bs["total_assets"]),
                                         ("LIABILITIES", bs["liabilities"], bs["total_liabilities"]),
                                         ("EQUITY", bs["equity"], bs["total_equity"])):
                w.writerow([section]); _write_account_section(w, items_)
                w.writerow([f"Total {section.title()}", f"{tot/100:.2f}"]); w.writerow([])
        z.writestr(f"{year}_balance_sheet.csv", make_csv(bs_rows))

        def txn_rows(w):
            w.writerow(["Date", "Payee", "Memo", "Account", "Amount", "Receipt file"])
            for r in con.execute(
                    "SELECT e.id eid, e.date, e.payee, e.memo, a.name account, s.amount_cents "
                    "FROM entries e JOIN splits s ON s.entry_id=e.id JOIN accounts a ON a.id=s.account_id "
                    "WHERE e.date BETWEEN ? AND ? ORDER BY e.date, e.id", (start, end)):
                doc = con.execute("SELECT filename FROM documents WHERE entry_id=?", (r["eid"],)).fetchone()
                w.writerow([r["date"], r["payee"], r["memo"], r["account"],
                            f"{r['amount_cents']/100:.2f}", f"receipts/{doc['filename']}" if doc else ""])
        z.writestr(f"{year}_transactions.csv", make_csv(txn_rows))

        def mile_rows(w):
            rate = float(db.get_setting(con, "mileage_rate", "0.70"))
            w.writerow(["Date", "Miles", "Purpose", "From", "To"])
            tot = 0.0
            for t in con.execute("SELECT * FROM mileage WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)):
                w.writerow([t["date"], t["miles"], t["purpose"], t["from_loc"], t["to_loc"]])
                tot += t["miles"]
            w.writerow([]); w.writerow(["Total miles", f"{tot:.1f}"])
            w.writerow(["Rate", f"{rate:.2f}"]); w.writerow(["Deduction", f"{tot*rate:.2f}"])
        z.writestr(f"{year}_mileage.csv", make_csv(mile_rows))

        for d in con.execute(
                "SELECT d.* FROM documents d LEFT JOIN entries e ON e.id=d.entry_id "
                "WHERE d.entry_id IS NULL OR e.date BETWEEN ? AND ?", (start, end)):
            fp = Path(d["path"])
            if fp.exists():
                z.write(fp, f"receipts/{d['filename']}")
    zbuf.seek(0)
    return StreamingResponse(iter([zbuf.getvalue()]), media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename=tax_package_{year}.zip"})
