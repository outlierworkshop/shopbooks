"""Per-diem travel: fiscal-year math, GSA response parsing, M&IE breakdown, actuals comparison,
and the /travel flow. No network — GSA lookups are either skipped (manual/standard paths) or
monkeypatched. Isolation: SHOPBOOKS_DATA_DIR -> temp dir BEFORE importing db (mandatory)."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_perdiem_")

import db        # noqa: E402
import ledger    # noqa: E402
import perdiem   # noqa: E402
from testutil import ok  # noqa: E402

db.init()
con = db.connect()

# --- fiscal year (GSA rates run Oct–Sep) -------------------------------------
ok(perdiem.fiscal_year_for("2026-09-30") == 2026, "September belongs to the same fiscal year")
ok(perdiem.fiscal_year_for("2026-10-01") == 2027, "October 1 rolls to the next fiscal year")

# --- GSA response parsing ------------------------------------------------------
GSA_PAYLOAD = {"rates": [{"rate": [{
    "meals": 79,
    "city": "Nashville",
    "county": "Davidson",
    "standardRate": "false",
    "months": {"month": [{"number": 7, "value": 199}, {"number": 8, "value": 189}]},
}]}]}
p = perdiem._parse_gsa(GSA_PAYLOAD, for_month=7)
ok(p["mie_cents"] == 7900, "M&IE dollars convert to cents")
ok(p["lodging_cents"] == 19900, "lodging picks the trip's start month")
ok("Nashville" in p["note"], "locality note names the GSA city")
ok(perdiem._parse_gsa({"rates": []}, 7) is None, "unknown locality (empty rates) returns None")

# --- M&IE breakdown (first/last day at 75%) -----------------------------------
b = perdiem.mie_breakdown("2026-07-10", "2026-07-13", 8000)  # 4 days
ok(b["days"] == 4 and b["full_days"] == 2, "4-day trip = 2 travel days + 2 full days")
ok(b["total_cents"] == 6000 * 2 + 8000 * 2, "75% on the first/last day, full rate between")
b1 = perdiem.mie_breakdown("2026-07-10", "2026-07-10", 8000)
ok(b1["total_cents"] == 6000 and b1["travel_days"] == 1, "single-day trip = one 75% travel day")
b2 = perdiem.mie_breakdown("2026-07-10", "2026-07-11", 8000)
ok(b2["total_cents"] == 12000 and b2["full_days"] == 0, "2-day trip = two 75% travel days")
try:
    perdiem.mie_breakdown("2026-07-13", "2026-07-10", 8000)
    ok(False, "backwards date range should raise")
except ValueError:
    ok(True, "backwards date range raises ValueError")

# --- actuals: meal categories + in-range spending -----------------------------
def acct(name, typ):
    return con.execute("INSERT INTO accounts(name,kind,type,active) VALUES(?, 'category', ?, 1)",
                       (name, typ)).lastrowid

chk = con.execute("INSERT INTO accounts(name,kind,type,active) VALUES('TripChecking','bank','asset',1)").lastrowid
meals = acct("Road Meals (test)", "expense")
fuel = acct("Trip Fuel (test)", "expense")
ledger.post_entry(con, "2026-07-11", "DINER A", [(meals, 3500), (chk, -3500)])
ledger.post_entry(con, "2026-07-12", "BBQ JOINT", [(meals, 6200), (chk, -6200)])
ledger.post_entry(con, "2026-07-12", "GAS STOP", [(fuel, 5000), (chk, -5000)])
ledger.post_entry(con, "2026-07-20", "DINER LATER", [(meals, 9900), (chk, -9900)])  # outside range
con.execute("INSERT INTO documents(filename,path,vendor,doc_date,amount_cents) "
            "VALUES('r1.jpg','/x','BBQ Joint','2026-07-12',6200)")
con.commit()

ok(meals in perdiem.meal_account_ids(con), "meal category detected by name")
ok(fuel not in perdiem.meal_account_ids(con), "non-meal expense category not counted as meals")
a = perdiem.trip_actuals(con, "2026-07-10", "2026-07-13")
ok(a["meals_total_cents"] == 3500 + 6200, "actual meals sum only in-range meal transactions")
ok(a["other_total_cents"] == 5000, "other in-range expenses listed for context")
ok(len(a["receipts"]) == 1, "receipts dated in the stay are attached")

# --- /travel flow (manual + standard-fallback + mocked GSA; no network) --------
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
client = TestClient(appmod.app)

ok(client.get("/travel").status_code == 200, "travel page renders")

r = client.post("/travel", data={"destination": "Nashville, TN", "city": "", "state": "", "zip_code": "",
                                 "start_date": "2026-07-10", "end_date": "2026-07-13",
                                 "purpose": "trade show", "manual_mie": "80.00"}, follow_redirects=False)
ok(r.status_code == 303, "trip with a manual rate creates without any lookup")
trip = con.execute("SELECT * FROM travel_trips ORDER BY id DESC LIMIT 1").fetchone()
ok(trip["mie_cents"] == 8000 and trip["rate_source"] == "manual", "manual rate stored as entered")

r = client.post("/travel", data={"destination": "Somewhere", "city": "", "state": "", "zip_code": "",
                                 "start_date": "2026-08-01", "end_date": "2026-08-02",
                                 "purpose": "", "manual_mie": ""}, follow_redirects=False)
trip2 = con.execute("SELECT * FROM travel_trips ORDER BY id DESC LIMIT 1").fetchone()
ok(trip2["rate_source"] == "standard" and trip2["mie_cents"] == perdiem.STANDARD_MIE_CENTS,
   "no locality -> standard CONUS fallback, no network attempted")

_orig = perdiem.fetch_gsa
perdiem.fetch_gsa = lambda *a, **k: {"mie_cents": 7900, "lodging_cents": 19900, "note": "GSA locality: Nashville"}
try:
    client.post("/travel", data={"destination": "Nashville again", "city": "Nashville", "state": "TN",
                                 "zip_code": "", "start_date": "2026-07-10", "end_date": "2026-07-13",
                                 "purpose": "", "manual_mie": ""}, follow_redirects=False)
    trip3 = con.execute("SELECT * FROM travel_trips ORDER BY id DESC LIMIT 1").fetchone()
    ok(trip3["rate_source"] == "gsa" and trip3["mie_cents"] == 7900 and trip3["lodging_cents"] == 19900,
       "locality trip stores the (mocked) GSA rate + lodging reference")
finally:
    perdiem.fetch_gsa = _orig

d = client.get(f"/travel/{trip['id']}")
ok(d.status_code == 200 and b"Actual meals posted" in d.content, "detail page renders the comparison")
# 4 days @ $80: 2 travel days at $60 + 2 full = $280 per diem vs $97 actual meals -> per diem wins
ok(b"Per diem" in d.content, "verdict is shown")

r = client.post(f"/travel/{trip['id']}/rate", data={"mie": "68.00"}, follow_redirects=False)
t = con.execute("SELECT * FROM travel_trips WHERE id=?", (trip["id"],)).fetchone()
ok(t["mie_cents"] == 6800 and t["rate_source"] == "manual", "per-trip rate override sticks")

client.post(f"/travel/{trip['id']}/delete", follow_redirects=False)
ok(con.execute("SELECT COUNT(*) c FROM travel_trips WHERE id=?", (trip["id"],)).fetchone()["c"] == 0,
   "trip delete removes the record (no ledger impact)")
bad = con.execute("SELECT COUNT(*) FROM (SELECT entry_id FROM splits GROUP BY entry_id "
                  "HAVING SUM(amount_cents)!=0)").fetchone()[0]
ok(bad == 0, "ledger untouched and balanced (travel is records-only)")

con.close()
print("\nPER DIEM TESTS DONE")
