"""Regression: the dashboard cash-flow chart axis must not assume a fixed 12-month length.

`routes_dashboard` builds cash_flow_chart as 8 historical months + a VARIABLE number of forecast
months (the 90-day horizon yields 3 or 4 depending on today's date), so the list is often 11 long.
The axis template used to hardcode `cash_flow_chart[11]`, which raised Jinja's
`UndefinedError: list object has no element 11` and 500'd the ENTIRE dashboard (the home page) --
"dev shopbooks won't open". The axis is now loop-driven and safe for any length.

Isolated via SHOPBOOKS_DATA_DIR (imports nothing that touches real books, but keep the guard)."""
import os
import re
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_axis_")

import jinja2  # noqa: E402
from testutil import ok  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "templates", "dashboard.html"), encoding="utf-8").read()

# 1. the fragile fixed-index axis must be gone (this exact index is what crashed)
ok("cash_flow_chart[11]" not in src, "axis no longer hardcodes cash_flow_chart[11]")

# 2. extract the real <div class="chart-axis"> ... </div> block and render it in isolation over a
#    range of list lengths -- it must never raise and must always end with the final month's label.
m = re.search(r'<div class="chart-axis">.*?</div>', src, re.DOTALL)
ok(m is not None, "found the chart-axis block in dashboard.html")
axis_tmpl = jinja2.Environment().from_string(m.group(0))

for n in (3, 8, 11, 12, 13):
    chart = [{"label": f"M{i}"} for i in range(n)]
    try:
        html = axis_tmpl.render(cash_flow_chart=chart)
        rendered_ok = True
    except Exception as e:  # an IndexError/UndefinedError here is the bug
        html = ""
        rendered_ok = False
    ok(rendered_ok, f"axis renders without error for a {n}-element chart")
    ok(f">M{n - 1}<" in html, f"axis always shows the last month for a {n}-element chart")

# 3. length 12 still behaves like the original (every other month + the last): 0,2,4,6,8,10,11
html12 = axis_tmpl.render(cash_flow_chart=[{"label": f"M{i}"} for i in range(12)])
shown = re.findall(r">M(\d+)<", html12)
ok(shown == ["0", "2", "4", "6", "8", "10", "11"],
   "12-month chart shows the same axis labels as before (every other month + last)")

print("\nDASHBOARD CASHFLOW AXIS TESTS DONE")
