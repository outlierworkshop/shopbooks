"""Financial reports, insights, forecast, and the AI assistant routes."""
import io
from datetime import date as date_cls
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

import ai
import chat
import db
import insights
import ledger
import timetracking
from webutil import _write_account_section, ctx, get_con, templates

router = APIRouter()

@router.get("/reports", response_class=HTMLResponse)
def reports(request: Request, start: str = "", end: str = "", con=Depends(get_con)):
    year = date_cls.today().year
    start = start or f"{year}-01-01"
    end = end or f"{year}-12-31"
    p = ledger.pnl(con, start, end)
    bs = ledger.balance_sheet(con, end)
    rate = float(db.get_setting(con, "mileage_rate", "0.70"))
    miles = con.execute("SELECT COALESCE(SUM(miles),0) m FROM mileage WHERE date BETWEEN ? AND ?",
                        (start, end)).fetchone()["m"]
    return templates.TemplateResponse(request, "reports.html", ctx(
        request, con, pnl=p, bs=bs, start=start, end=end, miles=miles, rate=rate,
        mileage_deduction=round(miles * rate * 100)))

@router.get("/reports/pnl.csv")
def pnl_csv(start: str, end: str, con=Depends(get_con)):
    p = ledger.pnl(con, start, end)
    buf = io.StringIO()
    w = __import__("csv").writer(buf)
    w.writerow(["Profit & Loss", f"{start} to {end}"])
    w.writerow([])
    w.writerow(["INCOME"])
    _write_account_section(w, p["income"])
    w.writerow(["Total Income", f"{p['total_income']/100:.2f}"])
    w.writerow([])
    w.writerow(["EXPENSES"])
    _write_account_section(w, p["expenses"])
    w.writerow(["Total Expenses", f"{p['total_expenses']/100:.2f}"])
    w.writerow([])
    w.writerow(["Net Profit", f"{p['net']/100:.2f}"])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=pnl_{start}_{end}.csv"})

@router.get("/reports/transactions.csv")
def transactions_csv(start: str, end: str, con=Depends(get_con)):
    rows = con.execute(
        "SELECT e.date, e.payee, e.memo, a.name account, s.amount_cents "
        "FROM entries e JOIN splits s ON s.entry_id=e.id JOIN accounts a ON a.id=s.account_id "
        "WHERE e.date BETWEEN ? AND ? ORDER BY e.date, e.id", (start, end)).fetchall()
    buf = io.StringIO()
    w = __import__("csv").writer(buf)
    w.writerow(["Date", "Payee", "Memo", "Account", "Amount"])
    for r in rows:
        w.writerow([r["date"], r["payee"], r["memo"], r["account"], f"{r['amount_cents']/100:.2f}"])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=transactions_{start}_{end}.csv"})

def _insights_facts(label, growth, exp, jobs, cash, health):
    """Compact block of the exact figures (dollars) for the AI narration."""
    m = ledger.fmt_cents
    pct = lambda x: f"{x:+.1f}%" if x is not None else "n/a"
    L = [f"Period: {label} (vs {growth['base_label']})."]
    for k, lbl in (("income", "Income"), ("expenses", "Expenses"), ("net", "Net profit")):
        g = growth[k]
        L.append(f"{lbl}: ${m(g['current'])} vs ${m(g['previous'])} ({pct(g['pct_change'])}).")
    movers = [r for r in exp["rows"] if r["delta"] != 0][:6]
    if movers:
        L.append("Biggest expense changes: " + "; ".join(
            f"{r['name']} ${m(r['current'])} ({'+' if r['delta'] >= 0 else '-'}${m(abs(r['delta']))})" for r in movers))
    prof = [j for j in jobs if j["net_cash"]][:5]
    if prof:
        L.append("Job net profit: " + "; ".join(f"{j['name']} ${m(j['net_cash'])}" for j in prof))
    L.append(f"Cash on hand: ${m(cash['cash_on_hand'])}. Credit-card debt: ${m(cash['card_debt'])}.")
    if health["issues"]:
        L.append("Needs attention: " + "; ".join(health["issues"]) + ".")
    return "\n".join(L)

@router.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request, period: str = "this-year", base: str = "last-year", explain: str = "",
                  con=Depends(get_con)):
    try:
        start, end, label = insights.parse_period(period)
    except ValueError:
        period, (start, end, label) = "this-year", insights.parse_period("this-year")
    pnl = insights.pnl_summary(con, start, end)
    growth = insights.compare(con, period, base)
    trend = insights.monthly_trend(con, start, end)
    exp = insights.expense_changes(con, period, base)
    cash = insights.cash_position(con, end)
    health = insights.bookkeeping_health(con, start, end)
    jobs = [j for j in timetracking.jobs_overview(con) if j["net_cash"] or j["hours"]]
    narrative = None
    if explain and ai.available(con):
        narrative = ai.analyze(con, _insights_facts(label, growth, exp, jobs, cash, health))
    return templates.TemplateResponse(request, "insights.html", ctx(
        request, con, period=period, base=base, label=label, pnl=pnl, growth=growth,
        trend=trend, exp=exp, cash=cash, health=health, jobs=jobs[:8],
        narrative=narrative, explained=bool(explain)))

def _forecast_facts(f):
    """Compact figures block for the optional AI forecast narration."""
    m = ledger.fmt_cents
    L = [f"Cash-flow forecast as of {f['today']} ({f['horizon_days']} days).",
         f"Starting cash: ${m(f['starting_cash'])}. Projected cash at the end: ${m(f['projected_end'])}.",
         f"Estimated monthly burn: ${m(f['avg_monthly_expense'])} "
         f"(of which ${m(f['recurring_monthly_expense'])} is known recurring bills).",
         f"Expected invoice collections over the horizon: ${m(f['expected_inflow_total'])}; "
         f"recurring income: ${m(f['recurring_income_total'])}."]
    L.append(f"Projected low point: ${m(f['low_point']['balance'])} around {f['low_point']['label']}."
             + (" Cash is projected to go NEGATIVE." if f["goes_negative"] else ""))
    L.append("By month: " + "; ".join(
        f"{mo['label']} in ${m(mo['inflow'])} / out ${m(mo['outflow'])} -> ${m(mo['end_balance'])}" for mo in f["months"]))
    return "\n".join(L)

@router.get("/forecast", response_class=HTMLResponse)
def forecast_page(request: Request, horizon: int = 90, explain: str = "", con=Depends(get_con)):
    horizon = horizon if horizon in (30, 60, 90, 180) else 90
    f = insights.cash_forecast(con, horizon_days=horizon)
    narrative = ai.analyze(con, _forecast_facts(f)) if (explain and ai.available(con)) else None
    return templates.TemplateResponse(request, "forecast.html", ctx(
        request, con, f=f, horizon=horizon, narrative=narrative, explained=bool(explain)))

CHAT_HISTORY = []  # in-memory transcript for the assistant (single local user; resets on restart)

@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request, con=Depends(get_con)):
    return templates.TemplateResponse(request, "chat.html", ctx(
        request, con, history=CHAT_HISTORY, err=None))

@router.post("/chat", response_class=HTMLResponse)
def chat_send(request: Request, message: str = Form(""), clear: str = Form(""), con=Depends(get_con)):
    if clear:
        CHAT_HISTORY.clear()
        return RedirectResponse("/chat", status_code=303)
    err = None
    msg = message.strip()
    if msg:
        CHAT_HISTORY.append({"role": "user", "content": msg})
        reply, err = chat.ask(con, CHAT_HISTORY)
        if reply:
            CHAT_HISTORY.append({"role": "assistant", "content": reply})
    return templates.TemplateResponse(request, "chat.html", ctx(
        request, con, history=CHAT_HISTORY, err=err))
