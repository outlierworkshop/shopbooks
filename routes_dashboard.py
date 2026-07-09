"""Dashboard (home) and global search routes."""
from datetime import date as date_cls
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

import ai
import db
import insights
import invoicing
import ledger
import search
from webutil import ctx, templates

router = APIRouter()

def _briefing_facts(b):
    """Compact figures block for the optional AI day-brief narration."""
    m = ledger.fmt_cents
    L = [f"Date: {b['today']}.",
         f"Cash on hand: ${m(b['cash_on_hand'])}. Credit-card debt: ${m(b['card_debt'])}.",
         f"Receivables: ${m(b['receivables_total'])} outstanding across {b['open_invoices']} invoice(s); "
         f"${m(b['receivables_overdue'])} overdue ({b['overdue_count']} invoice(s))."]
    if b["next_tax"]:
        L.append(f"Next estimated tax: {b['next_tax']['quarter']} ~${m(b['next_tax']['amount'])} "
                 f"due {b['next_tax']['due_date']} (in {b['next_tax']['days']} days).")
    L.append(("Needs attention: " + "; ".join(a["text"] for a in b["attention"]) + ".")
             if b["attention"] else "Nothing needs attention — the books are tidy.")
    return "\n".join(L)

@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    brief: str = "",
    pl_period: str = "this-quarter",
    exp_period: str = "this-quarter",
    sales_period: str = "ytd"
):
    con = db.connect()
    try:
        accounts = ledger.accounts_with_balances(con, kinds=("bank", "card"))
        year = date_cls.today().year
        p = ledger.pnl(con, f"{year}-01-01", f"{year}-12-31")
        recent = con.execute(
            "SELECT e.*, (SELECT GROUP_CONCAT(a.name, ' / ') FROM splits s JOIN accounts a ON a.id=s.account_id "
            " WHERE s.entry_id=e.id) accts, "
            "(SELECT MAX(abs(amount_cents)) FROM splits WHERE entry_id=e.id) amt "
            "FROM entries e ORDER BY e.date DESC, e.id DESC LIMIT 12").fetchall()
        brief_data = insights.briefing(con)
        narrative = ai.analyze(con, _briefing_facts(brief_data)) if (brief and ai.available(con)) else None
        trend = insights.monthly_trend(con, f"{year}-01-01", date_cls.today().isoformat())

        # --- Helper for custom period P&L comparisons ---
        def _get_comparison(con, period_str):
            from insights import parse_period, pnl_summary, _delta
            from datetime import timedelta
            today = date_cls.today()
            
            # Resolve current period
            cs, ce, clabel = parse_period(period_str, today)
            
            # Resolve base period
            p_clean = period_str.strip().lower()
            if p_clean in ("last-30-days", "30-days"):
                bs = (today - timedelta(days=60)).isoformat()
                be = (today - timedelta(days=31)).isoformat()
                blabel = "Prev 30 Days"
            elif p_clean in ("this-month-to-date", "month-to-date", "mtd", "this-month", "month"):
                from insights import _month_end
                y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
                bs = f"{y}-{m:02d}-01"
                be = _month_end(y, m).isoformat()
                blabel = f"{y}-{m:02d}"
            elif p_clean in ("this-quarter-to-date", "fq-to-date", "qtd"):
                from insights import _month_end
                cur_q = (today.month - 1) // 3 + 1
                y, prev_q = (today.year - 1, 4) if cur_q == 1 else (today.year, cur_q - 1)
                sm = 3 * (prev_q - 1) + 1
                bs = f"{y}-{sm:02d}-01"
                be = _month_end(y, sm + 2).isoformat()
                blabel = f"{y} Q{prev_q}"
            else:
                base_str = "last-year"
                if "quarter" in p_clean:
                    base_str = "last-quarter"
                elif "month" in p_clean:
                    base_str = "last-month"
                bs, be, blabel = parse_period(base_str, today)
                
            cur = pnl_summary(con, cs, ce)
            prev = pnl_summary(con, bs, be)
            return {
                "current_label": clabel,
                "base_label": blabel,
                "income": _delta(cur["income_total"], prev["income_total"]),
                "expenses": _delta(cur["expense_total"], prev["expense_total"]),
                "net": _delta(cur["net"], prev["net"]),
            }

        # --- New Dashboard Widget Calculations ---
        # 1. P&L compare
        p_l_compare = _get_comparison(con, pl_period)

        # 2. Expense breakdown
        exp_start, exp_end, exp_label = insights.parse_period(exp_period)
        exp_compare = _get_comparison(con, exp_period)
        exp_pnl = insights.pnl_summary(con, exp_start, exp_end)
        expense_breakdown = exp_pnl["expense_by_category"]

        expense_slices = []
        top_expenses = expense_breakdown[:4]
        other_amount = sum(x["amount"] for x in expense_breakdown[4:])
        colors = ['#1c7ed6', '#37b24d', '#f59f00', '#7048e8', '#ae3ec9']
        for i, item in enumerate(top_expenses):
            if item["amount"] > 0:
                expense_slices.append({
                    "name": item["name"],
                    "amount": item["amount"],
                    "color": colors[i % len(colors)]
                })
        if other_amount > 0:
            expense_slices.append({
                "name": "Other",
                "amount": other_amount,
                "color": "#737373"
            })

        # 3. Cash Flow chart (past 8 months + 3 months forecast = 12 months)
        today_dt = date_cls.today()
        historical_cash = []
        for i in range(8, 0, -1):
            m = today_dt.month - i
            y = today_dt.year
            while m <= 0:
                m += 12
                y -= 1
            start_date = f"{y}-{m:02d}-01"
            end_date = insights._month_end(y, m).isoformat()
            bal = insights.cash_position(con, end_date)["cash_on_hand"]
            pnl_m = insights.pnl_summary(con, start_date, end_date)
            label = insights._month_end(y, m).strftime("%b '%y")
            historical_cash.append({
                "label": label,
                "balance": bal,
                "inflow": pnl_m["income_total"],
                "outflow": pnl_m["expense_total"],
                "projected": False
            })

        forecast_data = insights.cash_forecast(con, horizon_days=90)
        projected_cash = []
        for m_item in forecast_data["months"]:
            parts = m_item["label"].split()
            label = f"{parts[0]} '{parts[1][2:]}"
            projected_cash.append({
                "label": label,
                "balance": m_item["end_balance"],
                "inflow": m_item["inflow"],
                "outflow": m_item["outflow"],
                "projected": True
            })

        cash_flow_chart = historical_cash + projected_cash
        balances = [x["balance"] for x in cash_flow_chart]
        cash_flow_max = max(balances) if balances else 1000000
        cash_flow_min = min(balances) if balances else 0

        # Find maximum inflow/outflow to scale the money in/out bars
        all_flows = [x["inflow"] for x in cash_flow_chart] + [x["outflow"] for x in cash_flow_chart]
        money_flow_max = max(all_flows) if all_flows else 1000000

        # 4. Paid Last 30 days
        from datetime import timedelta
        thirty_days_ago = (today_dt - timedelta(days=30)).isoformat()
        paid_direct = con.execute(
            "SELECT COALESCE(SUM(abs(s.amount_cents)), 0) FROM splits s "
            "JOIN accounts a ON a.id=s.account_id "
            "JOIN entries e ON e.id=s.entry_id "
            "JOIN invoices i ON i.paid_entry_id=e.id "
            "WHERE a.type='income' AND e.date >= ?", (thirty_days_ago,)
        ).fetchone()[0]
        paid_matched = con.execute(
            "SELECT COALESCE(SUM(abs(s.amount_cents)), 0) FROM splits s "
            "JOIN accounts a ON a.id=s.account_id "
            "JOIN entries e ON e.id=s.entry_id "
            "JOIN invoices i ON i.matched_entry_id=e.id "
            "WHERE a.type='income' AND e.date >= ? AND i.paid_entry_id IS NULL", (thirty_days_ago,)
        ).fetchone()[0]
        paid_links = con.execute(
            "SELECT COALESCE(SUM(abs(s.amount_cents)), 0) FROM splits s "
            "JOIN accounts a ON a.id=s.account_id "
            "JOIN entries e ON e.id=s.entry_id "
            "JOIN invoice_entry_links iel ON iel.entry_id=e.id "
            "WHERE a.type='income' AND e.date >= ? "
            "AND e.id NOT IN (SELECT COALESCE(paid_entry_id, 0) FROM invoices) "
            "AND e.id NOT IN (SELECT COALESCE(matched_entry_id, 0) FROM invoices)", (thirty_days_ago,)
        ).fetchone()[0]
        paid_last_30_days = (paid_direct or 0) + (paid_matched or 0) + (paid_links or 0)

        # 5. Sales calculation for the selected sales period
        sales_start, sales_end, sales_label = insights.parse_period(sales_period)
        sales_pnl = insights.pnl_summary(con, sales_start, sales_end)
        sales_total = sales_pnl["income_total"]

        # 6. Accounts Receivable aging slices
        ar_colors = {
            "current": '#37b24d',
            "1-30": '#17becf',
            "31-60": '#7048e8',
            "61-90": '#1c7ed6',
            "90+": '#d8842a'
        }
        ar_slices = []
        aging_data = invoicing.ar_aging(con)
        for bracket, val in aging_data["buckets"].items():
            if val > 0:
                ar_slices.append({
                    "name": bracket,
                    "amount": val,
                    "color": ar_colors.get(bracket, '#737373')
                })

        return templates.TemplateResponse(request, "dashboard.html", ctx(
            request, con, accounts=accounts, pnl=p, recent=recent, year=year,
            aging=aging_data, brief=brief_data, narrative=narrative,
            briefed=bool(brief), trend=trend,
            pl_period=pl_period,
            exp_period=exp_period,
            sales_period=sales_period,
            p_l_compare=p_l_compare,
            exp_compare=exp_compare,
            expense_slices=expense_slices,
            cash_flow_chart=cash_flow_chart,
            cash_flow_min=cash_flow_min,
            cash_flow_max=cash_flow_max,
            money_flow_max=money_flow_max,
            paid_last_30_days=paid_last_30_days,
            sales_total=sales_total,
            ar_slices=ar_slices))
    finally:
        con.close()

@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = ""):
    con = db.connect()
    try:
        return templates.TemplateResponse(request, "search.html", ctx(
            request, con, q=q, results=search.run(con, q)))
    finally:
        con.close()

@router.get("/search.json")
def search_suggest(q: str = ""):
    con = db.connect()
    try:
        return search.suggest(con, q)   # FastAPI serializes the list to JSON
    finally:
        con.close()
