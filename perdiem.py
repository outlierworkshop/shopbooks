"""Per-diem travel: GSA rate lookup and the per-diem-vs-actuals comparison.

Tax shape (Schedule C / sole proprietor — confirm specifics with the tax advisor):
  - Self-employed filers may use the GSA **M&IE** (meals & incidental expenses) per diem instead of
    tracking actual meal costs, with the first and last travel day at 75% of the daily rate.
  - **Lodging per diem is NOT allowed for the self-employed** — lodging must be actual receipts.
    The GSA lodging figure is stored/shown only as a reasonableness reference.
  - Business meals are generally 50% deductible under EITHER method, so comparing gross totals
    (per-diem M&IE vs actual meal spending) still identifies the more advantageous election.

Rates come from GSA's public per-diem API (https://open.gsa.gov/api/perdiem/) keyed by federal
fiscal year (Oct–Sep). Network is optional everywhere: a failed/missing lookup falls back to the
standard CONUS rate, and the trip form takes a manual rate — the calculation itself is
deterministic and local (numbers are never invented; see CLAUDE.md invariant 7's spirit).
"""
from datetime import datetime

import db
from logutil import log

# Standard CONUS fallback (FY2025/FY2026 published rates). Only used when the GSA lookup fails or
# no locality was given; the trip stores whatever rate it was created with and it can be
# overridden per trip, so a stale constant is correctable in the UI.
STANDARD_MIE_CENTS = 6800
STANDARD_LODGING_CENTS = 11000

GSA_BASE = "https://api.gsa.gov/travel/perdiem/v2/rates"
DEMO_KEY = "DEMO_KEY"   # api.data.gov shared key: fine for occasional personal use (rate-limited)


def fiscal_year_for(d):
    """GSA rates are keyed by federal fiscal year: Oct 1 starts the NEXT year's rates."""
    if isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d").date()
    return d.year + 1 if d.month >= 10 else d.year


def _parse_gsa(payload, for_month):
    """Pull (mie_cents, lodging_cents_for_month, locality_note) out of a GSA API response.
    Returns None when the response has no locality (GSA returns empty rates for unknown places)."""
    try:
        rates = payload.get("rates") or []
        if not rates:
            return None
        rate = (rates[0].get("rate") or [])
        if not rate:
            return None
        r = rate[0]
        mie_cents = round(float(r["meals"]) * 100)
        lodging_cents = None
        for m in (r.get("months") or {}).get("month", []):
            if int(m.get("number", 0)) == for_month:
                lodging_cents = round(float(m.get("value", 0)) * 100)
                break
        city = (r.get("city") or "").strip()
        county = (r.get("county") or "").strip()
        std = str(r.get("standardRate", "")).lower() == "true"
        note = "GSA standard CONUS rate" if std else f"GSA locality: {city or county}"
        return {"mie_cents": mie_cents, "lodging_cents": lodging_cents, "note": note}
    except (KeyError, TypeError, ValueError) as e:
        log.warning("GSA per-diem response parse failed: %s", e)
        return None


def fetch_gsa(con, city, state, zip_code, start_date):
    """Look up GSA per-diem rates for a trip's start date. ZIP wins over city/state.
    Returns the _parse_gsa dict or None on any failure (network optional, never raises)."""
    import httpx
    fy = fiscal_year_for(start_date)
    month = datetime.strptime(start_date, "%Y-%m-%d").month
    key = db.get_setting(con, "gsa_api_key", "").strip() or DEMO_KEY
    if zip_code:
        url = f"{GSA_BASE}/zip/{zip_code}/year/{fy}"
    elif city and state:
        url = f"{GSA_BASE}/city/{city}/state/{state}/year/{fy}"
    else:
        return None
    try:
        r = httpx.get(url, params={"api_key": key}, timeout=10)
        r.raise_for_status()
        return _parse_gsa(r.json(), month)
    except Exception as e:
        log.warning("GSA per-diem lookup failed (%s): %s", url, e)
        return None


def mie_breakdown(start_date, end_date, mie_cents):
    """The M&IE per-diem math: first and last travel day at 75%, full rate in between.
    A single-day trip counts as one 75% travel day (flag it to the user — per diem generally
    requires overnight travel). Raises ValueError on a backwards range."""
    s = datetime.strptime(start_date, "%Y-%m-%d").date()
    e = datetime.strptime(end_date, "%Y-%m-%d").date()
    days = (e - s).days + 1
    if days < 1:
        raise ValueError("The trip's end date is before its start date.")
    travel_day_cents = round(mie_cents * 0.75)
    if days == 1:
        return {"days": 1, "travel_days": 1, "full_days": 0,
                "travel_day_cents": travel_day_cents, "total_cents": travel_day_cents}
    full_days = days - 2
    return {"days": days, "travel_days": 2, "full_days": full_days,
            "travel_day_cents": travel_day_cents,
            "total_cents": travel_day_cents * 2 + full_days * mie_cents}


MEAL_NAME_PATTERNS = ("meal", "food", "dining", "restaurant", "refreshment")


def meal_account_ids(con):
    """Expense accounts that look like meal categories (by name), for the 'actual meals' side."""
    rows = con.execute("SELECT id, name FROM accounts WHERE type='expense'").fetchall()
    return {r["id"]: r["name"] for r in rows
            if any(p in r["name"].lower() for p in MEAL_NAME_PATTERNS)}


def trip_actuals(con, start_date, end_date):
    """What was actually spent (posted entries) and documented (receipts) during the stay.
    Deterministic ledger sums — meals matched by category name, everything else listed for context."""
    meals = meal_account_ids(con)
    rows = con.execute(
        "SELECT e.id entry_id, e.date, e.payee, s.amount_cents, a.id account_id, a.name account "
        "FROM entries e JOIN splits s ON s.entry_id=e.id JOIN accounts a ON a.id=s.account_id "
        "WHERE a.type='expense' AND s.amount_cents > 0 AND e.date BETWEEN ? AND ? "
        "ORDER BY e.date, e.id", (start_date, end_date)).fetchall()
    meal_rows = [dict(r) for r in rows if r["account_id"] in meals]
    other_rows = [dict(r) for r in rows if r["account_id"] not in meals]
    receipts = con.execute(
        "SELECT id, vendor, doc_date, amount_cents, status FROM documents "
        "WHERE kind='receipt' AND doc_date BETWEEN ? AND ? ORDER BY doc_date", (start_date, end_date)).fetchall()
    return {"meal_rows": meal_rows,
            "meals_total_cents": sum(r["amount_cents"] for r in meal_rows),
            "other_rows": other_rows,
            "other_total_cents": sum(r["amount_cents"] for r in other_rows),
            "receipts": receipts,
            "meal_categories": sorted(meals.values())}


def trip_summary(con, trip):
    """Everything the detail page (and list row) needs: the per-diem math, the actuals, and the
    verdict. `trip` is a travel_trips row."""
    breakdown = mie_breakdown(trip["start_date"], trip["end_date"], trip["mie_cents"])
    actuals = trip_actuals(con, trip["start_date"], trip["end_date"])
    perdiem_total = breakdown["total_cents"]
    meals_total = actuals["meals_total_cents"]
    return {"breakdown": breakdown, "actuals": actuals,
            "perdiem_total_cents": perdiem_total,
            "advantage_cents": perdiem_total - meals_total,
            "winner": "perdiem" if perdiem_total >= meals_total else "actual"}


def list_trips(con):
    return con.execute("SELECT * FROM travel_trips ORDER BY start_date DESC, id DESC").fetchall()


def get_trip(con, trip_id):
    return con.execute("SELECT * FROM travel_trips WHERE id=?", (trip_id,)).fetchone()
