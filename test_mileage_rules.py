"""Standing mileage rules: classify a detected trip by WHERE it went, and keep personal miles out of
the tax deduction.

Rules match on coordinates within a radius (address text drifts between neighbouring house numbers),
a route rule (start AND end) outranks a destination rule, and a rule marked trusted (`auto_log`)
skips the approval queue. `mileage.business=0` means "logged, but not deducted".

Isolated via SHOPBOOKS_DATA_DIR before importing db."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_mrules_")

import db  # noqa: E402
import trips  # noqa: E402
from testutil import ok  # noqa: E402

trips.route_miles = lambda a, b, c, d: (round(trips.haversine_miles(a, b, c, d) * 1.3, 1), "estimate")
trips.reverse_place = lambda lat, lon: f"{lat:.4f},{lon:.4f}"

db.init()
con = db.connect()

SHOP = (42.3993994, -71.1204718)          # real coordinates from Ben's own trip log
CLIENT = (42.3814879, -71.1366357)
HOME = (42.3959688, -71.1172687)


def rule(name, dest, *, kind="destination", start=None, purpose="", business=1, auto=0, radius=150):
    return con.execute(
        "INSERT INTO mileage_rules(name,match_kind,dest_lat,dest_lon,start_lat,start_lon,radius_m,"
        "purpose,business,auto_log) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (name, kind, dest[0], dest[1], start[0] if start else None, start[1] if start else None,
         radius, purpose, business, auto)).lastrowid


def candidate(start, end, miles=2.0):
    return con.execute(
        "INSERT INTO trip_candidates(start_ts,end_ts,start_lat,start_lon,end_lat,end_lon,miles,"
        "distance_source,start_place,end_place) "
        "VALUES('2026-08-06T09:00:00','2026-08-06T09:20:00',?,?,?,?,?,'osrm','A','B')",
        (start[0], start[1], end[0], end[1], miles)).lastrowid


# --- destination matching, with a radius --------------------------------------------------------
r_client = rule("Client site", CLIENT, purpose="Client visit")
con.commit()
m = trips.match_rule(con, SHOP[0], SHOP[1], CLIENT[0], CLIENT[1])
ok(m and m["id"] == r_client, "a trip ending at the destination matches the rule")
ok(trips.match_rule(con, SHOP[0], SHOP[1], HOME[0], HOME[1]) is None,
   "a trip ending somewhere else matches nothing")

# ~90 m away still counts (GPS drift / a different parking spot); ~1 km does not
near = (CLIENT[0] + 0.0008, CLIENT[1])
far = (CLIENT[0] + 0.009, CLIENT[1])
ok(trips.match_rule(con, SHOP[0], SHOP[1], near[0], near[1]) is not None,
   "a point ~90m away is still 'here' (default 150m radius)")
ok(trips.match_rule(con, SHOP[0], SHOP[1], far[0], far[1]) is None,
   "a point ~1km away is not")

# --- a route rule outranks a destination rule ----------------------------------------------------
r_commute = rule("Shop to home", HOME, kind="route", start=SHOP, purpose="Commute", business=0)
r_home_any = rule("Anything ending home", HOME, purpose="Errand", business=1)
con.commit()
m = trips.match_rule(con, SHOP[0], SHOP[1], HOME[0], HOME[1])
ok(m and m["id"] == r_commute, "the route rule wins over the destination rule when both fit")
ok(m["business"] == 0, "...so this drive is classified personal")
m2 = trips.match_rule(con, CLIENT[0], CLIENT[1], HOME[0], HOME[1])
ok(m2 and m2["id"] == r_home_any,
   "coming home from somewhere ELSE falls back to the destination rule")

# a tighter destination rule beats a broad one
r_broad = rule("Whole industrial park", CLIENT, radius=5000, purpose="Broad")
con.commit()
m3 = trips.match_rule(con, SHOP[0], SHOP[1], CLIENT[0], CLIENT[1])
ok(m3 and m3["id"] == r_client, "the nearer/tighter destination rule wins over a broad one")
con.execute("DELETE FROM mileage_rules WHERE id=?", (r_broad,))
con.commit()

# --- applying a rule: pre-fill vs auto-log --------------------------------------------------------
c1 = candidate(SHOP, CLIENT)
con.commit()
logged = trips.apply_rule(con, c1, con.execute("SELECT * FROM mileage_rules WHERE id=?", (r_client,)).fetchone())
con.commit()
ok(logged is False, "an untrusted rule does NOT auto-log")
row = con.execute("SELECT status, rule_id FROM trip_candidates WHERE id=?", (c1,)).fetchone()
ok(row["status"] == "pending" and row["rule_id"] == r_client,
   "the trip stays in the approval queue, tagged with the rule")
pend = [p for p in trips.pending_candidates(con) if p["id"] == c1][0]
ok(pend["rule_purpose"] == "Client visit" and pend["suggested_business"] == 1,
   "the page gets the rule's purpose and type to pre-fill")

con.execute("UPDATE mileage_rules SET auto_log=1 WHERE id=?", (r_commute,))
c2 = candidate(SHOP, HOME, miles=1.4)
con.commit()
logged = trips.apply_rule(con, c2, con.execute("SELECT * FROM mileage_rules WHERE id=?", (r_commute,)).fetchone())
con.commit()
ok(logged is True, "a TRUSTED rule auto-logs the trip")
row = con.execute("SELECT status, mileage_id FROM trip_candidates WHERE id=?", (c2,)).fetchone()
ok(row["status"] == "approved" and row["mileage_id"], "the candidate is approved and linked to a log row")
mrow = con.execute("SELECT * FROM mileage WHERE id=?", (row["mileage_id"],)).fetchone()
ok(mrow["business"] == 0 and mrow["purpose"] == "Commute",
   "the auto-logged trip carries the rule's purpose and personal classification")
ok(mrow["miles"] == 1.4 and mrow["date"] == "2026-08-06", "with the trip's own miles and date")

# --- a new rule sorts out trips already waiting ---------------------------------------------------
c3 = candidate(CLIENT, SHOP)
con.commit()
ok(con.execute("SELECT rule_id FROM trip_candidates WHERE id=?", (c3,)).fetchone()["rule_id"] is None,
   "a trip with no matching rule is untagged")
rule("Back to the shop", SHOP, purpose="Return")
con.commit()
matched, auto = trips.apply_rules_to_pending(con)
con.commit()
ok(matched >= 1, "adding a rule re-classifies trips already waiting for approval")
ok(con.execute("SELECT rule_id FROM trip_candidates WHERE id=?", (c3,)).fetchone()["rule_id"] is not None,
   "the waiting trip is now tagged")

# --- deleting a logged trip that came from a detected candidate ------------------------------------
# trip_candidates.mileage_id points at the mileage row, so the delete route must clear it first or
# the FK constraint 500s — the auto-logged trip above is exactly that case.
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
client = TestClient(appmod.app)
auto_mid = con.execute("SELECT mileage_id FROM trip_candidates WHERE id=?", (c2,)).fetchone()["mileage_id"]
r = client.post("/mileage/delete", data={"trip_id": auto_mid}, follow_redirects=False)
ok(r.status_code == 303, "deleting an auto-logged trip doesn't error")
ok(con.execute("SELECT COUNT(*) c FROM mileage WHERE id=?", (auto_mid,)).fetchone()["c"] == 0,
   "the mileage row is gone")
ok(con.execute("SELECT mileage_id FROM trip_candidates WHERE id=?", (c2,)).fetchone()["mileage_id"] is None,
   "the candidate's link is cleared rather than left dangling")

# --- personal miles never reach the deduction -----------------------------------------------------
con.execute("UPDATE trip_candidates SET mileage_id=NULL")
con.execute("DELETE FROM mileage")
con.execute("INSERT INTO mileage(date,miles,purpose,business) VALUES('2026-03-01',100,'Job',1)")
con.execute("INSERT INTO mileage(date,miles,purpose,business) VALUES('2026-03-02',40,'Groceries',0)")
con.commit()
biz = con.execute("SELECT COALESCE(SUM(miles),0) m FROM mileage WHERE date LIKE '2026%' AND business=1").fetchone()["m"]
allm = con.execute("SELECT COALESCE(SUM(miles),0) m FROM mileage WHERE date LIKE '2026%'").fetchone()["m"]
ok(biz == 100 and allm == 140, "business miles are 100 of 140 total")

page = client.get("/taxes?year=2026").text
ok("70.00" in page, "the deduction uses the 100 BUSINESS miles (100 x $0.70), not all 140")
ok("98.00" not in page, "the personal 40 miles are not deducted")

# a trip logged before the column existed still counts as business (migration default)
con.execute("INSERT INTO mileage(date,miles,purpose) VALUES('2026-03-03',10,'Legacy row')")
con.commit()
ok(con.execute("SELECT business FROM mileage WHERE purpose='Legacy row'").fetchone()["business"] == 1,
   "a row inserted without a type defaults to business (past deductions are unchanged)")

ok(client.get("/mileage").status_code == 200, "the mileage page renders with rules")

# --- editing a logged trip in place ---------------------------------------------------------------
mid = con.execute("SELECT id FROM mileage WHERE purpose='Job'").fetchone()["id"]
r = client.post("/mileage/update", follow_redirects=False, data={
    "trip_id": mid, "date": "2026-03-04", "miles": "12.5", "purpose": "Client delivery",
    "from_loc": "14 William Street, Somerville, MA", "to_loc": "319 Huron Avenue, Cambridge, MA",
    "business": "1"})
ok(r.status_code == 303, "editing a logged trip redirects")
row = con.execute("SELECT * FROM mileage WHERE id=?", (mid,)).fetchone()
ok(row["miles"] == 12.5 and row["purpose"] == "Client delivery", "miles and purpose are updated")
ok(row["date"] == "2026-03-04", "the date is updated")
ok(row["from_loc"].startswith("14 William"), "from/to can be corrected to street addresses")

# switching a logged trip to personal pulls it out of the deduction straight away
before = con.execute("SELECT COALESCE(SUM(miles),0) m FROM mileage WHERE business=1").fetchone()["m"]
client.post("/mileage/update", follow_redirects=False, data={
    "trip_id": mid, "date": row["date"], "miles": row["miles"], "purpose": row["purpose"],
    "from_loc": row["from_loc"], "to_loc": row["to_loc"], "business": "0"})
after = con.execute("SELECT COALESCE(SUM(miles),0) m FROM mileage WHERE business=1").fetchone()["m"]
ok(con.execute("SELECT business FROM mileage WHERE id=?", (mid,)).fetchone()["business"] == 0,
   "a logged trip can be reclassified as personal")
ok(after == before - 12.5, "...and its miles leave the deductible total immediately")

r = client.post("/mileage/update", follow_redirects=False,
                data={"trip_id": mid, "date": "2026-03-04", "miles": "0", "business": "1"})
ok("err=" in r.headers["location"], "zero miles is refused rather than silently saved")
ok(client.get("/mileage").text.count('action="/mileage/update"') >= 1,
   "the log rows are editable in place on the page")
# --- personal is the default: a mile is only deducted when something says it's business ----------
plain = candidate((10.0, 10.0), (11.0, 11.0))     # nowhere near any rule
con.commit()
pend = [p for p in trips.pending_candidates(con) if p["id"] == plain][0]
ok(pend["rule_id"] is None, "a trip matching no rule is untagged")
ok(pend["suggested_business"] == 0,
   "an unmatched trip defaults to PERSONAL, so nothing is deducted unless you say so")
ok('value="0" selected>Personal' in client.get("/mileage").text,
   "the manual Add-a-trip form offers Personal first")

r = client.post("/mileage", follow_redirects=False, data={
    "date": "2026-04-01", "miles": "5", "purpose": "unspecified"})   # no business field at all
ok(r.status_code == 303, "adding a trip without stating a type works")
ok(con.execute("SELECT business FROM mileage WHERE purpose='unspecified'").fetchone()["business"] == 0,
   "...and it is logged personal, not silently deducted")

r = client.post("/mileage/trip/%d/approve" % plain, follow_redirects=False,
                data={"miles": "3", "purpose": "no type given"})     # no business field
ok(r.status_code == 303, "approving without stating a type works")
ok(con.execute("SELECT business FROM mileage WHERE purpose='no type given'").fetchone()["business"] == 0,
   "...and that trip is personal too")

# a rule still overrides the default
rule("Client site business", CLIENT, purpose="Client visit", business=1)
con.commit()
cb = candidate(SHOP, CLIENT)
con.commit()
trips.apply_rules_to_pending(con)
con.commit()
pb = [p for p in trips.pending_candidates(con) if p["id"] == cb][0]
ok(pb["suggested_business"] == 1, "a business rule still marks its trips business")

# --- the Apply-rules button sweeps trips captured BEFORE the rule existed -------------------------
orphan_c = candidate((41.0, -71.5), (41.2, -71.6))
con.commit()
ok([p for p in trips.pending_candidates(con) if p["id"] == orphan_c][0]["rule_id"] is None,
   "a trip with no matching rule starts untagged")
r = client.post("/mileage/rules/apply", follow_redirects=False)
ok(r.status_code == 303, "Apply rules runs against the waiting trips")
rule("Far away", (41.2, -71.6), purpose="Delivery", business=1, radius=500)
con.commit()
r = client.post("/mileage/rules/apply", follow_redirects=False)
ok(r.status_code == 303 and "msg=" in r.headers["location"], "and reports what it matched")
ok([p for p in trips.pending_candidates(con) if p["id"] == orphan_c][0]["rule_id"] is not None,
   "a rule written AFTER the trip was captured now classifies it")
ok("/mileage/rules/apply" in client.get("/mileage").text, "the page offers the Apply rules button")

con.close()
print("\nMILEAGE RULES TESTS DONE")
