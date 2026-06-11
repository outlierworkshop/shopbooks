"""Statement year-reconciliation tests (pure logic; no DB/network)."""
import os
import tempfile
from datetime import date

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_years_")
import importer  # noqa: E402

ok = lambda c, w: print(("PASS" if c else "FAIL"), w)
TODAY = date(2026, 6, 11)


def years(txns, end="", today=TODAY):
    return [t["date"] for t in importer.reconcile_years([{"date": d} for d in txns], end, today)]


# the reported bug: AI put 2028 on every line; closing date in header is 2026-01-15
ok(years(["2028-12-20", "2028-01-05"], end="2026-01-15") == ["2025-12-20", "2026-01-05"],
   "closing date 2026-01-15: Dec->2025, Jan->2026 (rollover correct), 2028 ignored")

# no closing date found -> anchor to today; future years pulled into the past, never future
ok(years(["2028-03-10"]) == ["2026-03-10"], "no period: Mar (before today) -> current year 2026")
ok(years(["2028-08-20"]) == ["2025-08-20"], "no period: Aug (after today) -> last year 2025")

# a hallucinated FUTURE closing date is rejected; falls back to today
ok(years(["2028-12-20"], end="2028-01-15") == ["2025-12-20"], "future closing date ignored -> today anchor")

# old statements with a correct closing date are NOT disturbed
ok(years(["2024-11-15", "2024-10-30"], end="2024-11-30") == ["2024-11-15", "2024-10-30"],
   "old statement with real closing date stays put")

# nothing in the output is ever in the future
out = years(["2028-12-31", "2027-07-01", "2026-09-09"], end="2026-01-15")
ok(all(d <= TODAY.isoformat() for d in out), f"no future dates remain: {out}")

# regex-fallback clamp
ok([t["date"] for t in importer.clamp_future_dates([{"date": "2028-03-10"}], TODAY)] == ["2026-03-10"],
   "clamp_future_dates pulls a future date back to <= today")
ok([t["date"] for t in importer.clamp_future_dates([{"date": "2025-02-01"}], TODAY)] == ["2025-02-01"],
   "clamp_future_dates leaves a valid past date alone")

print("\nYEAR-RECONCILIATION TESTS DONE")
