"""Automatic mileage capture: event parsing, pairing, distance fallback, watcher ingest, and the
/mileage approval flow. No network — route_miles/reverse_place are monkeypatched (their offline
fallbacks are what's under test). Isolation: SHOPBOOKS_DATA_DIR -> temp dir BEFORE importing db."""
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_trips_")

import db        # noqa: E402
import trips     # noqa: E402
import watcher   # noqa: E402
from testutil import ok  # noqa: E402

db.init()
con = db.connect()

# Kill the network for the whole test: routing falls back deterministically, geocoding returns coords.
trips.route_miles = lambda a, b, c, d: (round(trips.haversine_miles(a, b, c, d) * trips.ROAD_FACTOR, 1), "estimate")
trips.reverse_place = lambda lat, lon: f"{lat:.4f}, {lon:.4f}"

# --- parsing -------------------------------------------------------------------
ev = trips.parse_event("connect,2026-07-14T08:32:11,36.1234,-86.5678")
ok(ev and ev["event"] == "connect" and ev["lat"] == 36.1234, "happy-path event parses")
ok(trips.parse_event("connect,2026-07-14T08:32:11Z,36.1,-86.5") is not None, "Z-suffixed timestamps accepted")
ok(trips.parse_event("hello world") is None, "garbage is rejected")
ok(trips.parse_event("connect,not-a-date,36.1,-86.5") is None, "bad timestamp rejected")
ok(trips.parse_event("connect,2026-07-14T08:32:11,99.9,-86.5") is None, "out-of-range latitude rejected")
ok(trips.parse_event("connect,2026-07-14T08:32:11,36.1,-86.5,extra,fields") is not None,
   "trailing fields tolerated (future-proofing)")

# --- the phone trip logger's own format ------------------------------------------
# Real line shape (see docs/mileage-automation.md): US M-D-YY date, HH.MM time, and TWO coordinate
# pairs — `Location` is the trigger's cached anchor, `(Lat/Long: ...)` the phone's actual position.
LINE = "[START] 8-6-26 14.28 | Location 42.39588236901909,-71.11721995286644 (Lat/Long: 42.3959688,-71.1172687)"
ev = trips.parse_event(LINE)
ok(ev is not None, "a [START] log line parses")
ok(ev["event"] == "connect", "[START] maps to the internal 'connect'")
ok(ev["ts"] == "2026-08-06T14:28:00", "M-D-YY + HH.MM -> 2026-08-06T14:28:00")
ok((ev["lat"], ev["lon"]) == (42.3959688, -71.1172687),
   "the precise (Lat/Long: ...) fix wins over the cached Location anchor")
ok(trips.parse_event(LINE.replace("[START]", "[END]"))["event"] == "disconnect",
   "[END] maps to 'disconnect'")
ok(trips.parse_event(LINE.replace("[START]", "[STOP]"))["event"] == "disconnect",
   "[STOP] is accepted as an end too")
ok(trips.parse_event("[START] 8-6-26 14.28 | Location 42.3958823,-71.1172199")["lat"] == 42.3958823,
   "a line with only the Location anchor still parses (fallback)")
ok(trips.parse_event("[START] 8-6-26 not-a-time | Lat/Long: 42.1,-71.1") is None,
   "an unparseable time is rejected")
ok(trips.parse_event("[START] 8-6-26 14.28 | no coordinates here") is None,
   "a line with no coordinates is rejected")

# one appending log holding MANY events: every line lands, and re-reading adds nothing (the watcher
# re-reads the whole file each time the phone appends to it)
LOG = b"""[START] 8-6-26 20.44 | Location 42.3995,-71.1206 (Lat/Long: 42.3993994,-71.1204718)
[END] 8-6-26 20.54 | Location 42.3995,-71.1206 (Lat/Long: 42.3814879,-71.1366357)
"""
before = con.execute("SELECT COUNT(*) c FROM trip_events").fetchone()["c"]
s, note = trips.ingest_event_file(con, Path("triplog.txt"), LOG)
ok(s == "imported" and "2 new" in note, f"a multi-line log ingests every event ({note})")
ok(con.execute("SELECT COUNT(*) c FROM trip_events").fetchone()["c"] == before + 2,
   "both lines were stored")
s, note = trips.ingest_event_file(con, Path("triplog.txt"), LOG)
ok(s == "duplicate", "re-reading the same log adds nothing (idempotent)")
ok(con.execute("SELECT COUNT(*) c FROM trip_events").fetchone()["c"] == before + 2,
   "the event count is unchanged after a re-read")

# the log grows: only the appended line is new
GROWN = LOG + b"[START] 8-6-26 21.10 | Location 42.3815,-71.1366 (Lat/Long: 42.3814879,-71.1366357)\n"
s, note = trips.ingest_event_file(con, Path("triplog.txt"), GROWN)
ok(s == "imported" and "1 new" in note and "2 already logged" in note,
   f"an appended log ingests only the new line ({note})")
ok(trips.pair_events(con) >= 1, "the logged drive pairs into a candidate")

# --- street-address labels -------------------------------------------------------
# Real Nominatim shapes (zoom=18) for the coordinates in Ben's own trip log.
ok(trips.address_label({
    "house_number": "14", "road": "William Street", "neighbourhood": "West Somerville",
    "suburb": "Ball Square", "city": "Somerville", "county": "Middlesex County",
    "state": "Massachusetts", "ISO3166-2-lvl4": "US-MA", "postcode": "02144",
}) == "14 William Street, Somerville, MA", "a street address is built from house number + road")

# OSM multi-address nodes give ranges like "319;321" — showing that verbatim looks broken
ok(trips.address_label({
    "house_number": "319;321", "road": "Huron Avenue", "city": "Cambridge",
    "ISO3166-2-lvl4": "US-MA",
}) == "319 Huron Avenue, Cambridge, MA", "a house-number RANGE shows only the first number")
ok(trips._house_number("11;13") == "11" and trips._house_number("") == "",
   "house-number ranges are trimmed to the first value")

ok(trips.address_label({"road": "Huron Avenue", "city": "Cambridge", "ISO3166-2-lvl4": "US-MA"})
   == "Huron Avenue, Cambridge, MA", "a road with no house number still reads as a street")
ok(trips.address_label({"suburb": "Ball Square", "city": "Somerville", "ISO3166-2-lvl4": "US-MA"})
   == "Ball Square, Somerville, MA", "no road (a park/lot) falls back to the neighbourhood")
ok(trips.address_label({"road": "Main Street", "city": "Concord", "state": "New Hampshire"})
   == "Main Street, Concord, New Hampshire", "the full state name is used when there's no ISO code")
ok(trips.address_label({"city": "Somerville", "town": "Somerville", "ISO3166-2-lvl4": "US-MA"})
   == "Somerville, MA", "a repeated name isn't printed twice")
ok(trips.address_label({}) == "", "an empty address yields an empty label (caller falls back to coords)")

# --- refreshing the labels on already-captured trips ------------------------------
trips.NOMINATIM_MIN_INTERVAL = 0        # don't actually pace inside the test
trips.reverse_place = lambda lat, lon: f"{lat:.4f} Somewhere St, Testville, MA"
before = con.execute("SELECT id, start_place FROM trip_candidates WHERE status='pending' "
                     "ORDER BY id DESC LIMIT 1").fetchone()
if before:
    changed = trips.refresh_places(con)
    con.commit()
    ok(changed >= 1, "refresh_places re-labels pending candidates")
    after = con.execute("SELECT start_place, end_place FROM trip_candidates WHERE id=?",
                        (before["id"],)).fetchone()
    ok("Somewhere St" in after["start_place"] and "Somewhere St" in after["end_place"],
       "both endpoints get the new address label")
    ok(trips.refresh_places(con) == 0, "a second refresh changes nothing (already up to date)")
# restore the coordinate-style stub for the rest of the file
trips.reverse_place = lambda lat, lon: f"{lat:.4f}, {lon:.4f}"

# --- haversine sanity ----------------------------------------------------------
# Nashville downtown to Franklin TN is ~18 mi straight-line
d = trips.haversine_miles(36.1627, -86.7816, 35.9251, -86.8689)
ok(16 < d < 20, f"haversine in the right ballpark ({d:.1f} mi)")

# --- ingest + dedup ------------------------------------------------------------
s, note = trips.ingest_event_file(con, Path("evt1.txt"), b"connect,2026-07-14T08:00:00,36.1627,-86.7816")
ok(s == "imported", "event file ingests")
s, _ = trips.ingest_event_file(con, Path("evt1b.txt"), b"connect,2026-07-14T08:00:00,36.1627,-86.7816")
ok(s == "duplicate", "same event+timestamp dedups")
s, _ = trips.ingest_event_file(con, Path("bad.txt"), b"not an event")
ok(s == "error", "non-event file reports error")

# --- pairing -------------------------------------------------------------------
trips.ingest_event_file(con, Path("evt2.txt"), b"disconnect,2026-07-14T08:40:00,35.9251,-86.8689")
created = trips.pair_events(con)
ok(created == 1, "connect+disconnect pair into one candidate")
c = con.execute("SELECT * FROM trip_candidates ORDER BY id DESC LIMIT 1").fetchone()
ok(c["distance_source"] == "estimate" and 20 < c["miles"] < 26,
   f"routed-fallback distance = haversine x1.3 ({c['miles']} mi)")
ok(c["start_place"].startswith("36.16"), "place label falls back to coordinates")

# driveway blip: reconnect a few feet / a minute later -> consumed, no candidate
trips.ingest_event_file(con, Path("blip1.txt"), b"connect,2026-07-14T09:00:00,36.16270,-86.78160")
trips.ingest_event_file(con, Path("blip2.txt"), b"disconnect,2026-07-14T09:02:00,36.16271,-86.78161")
before = con.execute("SELECT COUNT(*) c FROM trip_candidates").fetchone()["c"]
trips.pair_events(con)
after = con.execute("SELECT COUNT(*) c FROM trip_candidates").fetchone()["c"]
ok(after == before, "driveway blip (tiny distance, tiny duration) makes no candidate")

# dangling connect stays pending inside the window, orphans once it's stale
fresh = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
stale = (datetime.now() - timedelta(hours=30)).isoformat(timespec="seconds")
trips.ingest_event_file(con, Path("dang1.txt"), f"connect,{fresh},36.2,-86.7".encode())
trips.ingest_event_file(con, Path("dang2.txt"), f"connect,{stale},36.3,-86.6".encode())
trips.pair_events(con)
st_fresh = con.execute("SELECT status FROM trip_events WHERE ts=?", (fresh,)).fetchone()["status"]
st_stale = con.execute("SELECT status FROM trip_events WHERE ts=?", (stale,)).fetchone()["status"]
ok(st_fresh == "pending", "recent dangling connect stays pending (disconnect may still come)")
ok(st_stale == "orphan", "stale dangling connect is orphaned after the 12h window")
con.commit()

# --- watcher ingest ------------------------------------------------------------
inbox = Path(tempfile.mkdtemp(prefix="trips_inbox_"))
db.set_setting(con, "trips_watch_folder", str(inbox))
# Timestamps must be RECENT, not a fixed date: the watcher ingests w1 then w2, and pair_events
# orphans a connect that has no partner once it's older than MAX_TRIP_HOURS. A hardcoded past date
# orphaned the connect before its disconnect file was scanned, so this only passed on that one day.
w_start = (datetime.now() - timedelta(hours=2)).replace(microsecond=0)
w_end = w_start + timedelta(minutes=35)
(inbox / "w1.txt").write_text(f"connect,{w_start.isoformat(timespec='seconds')},36.1627,-86.7816")
(inbox / "w2.txt").write_text(f"disconnect,{w_end.isoformat(timespec='seconds')},35.9251,-86.8689")
con.commit()
r = watcher.run_once(con, lambda *a: ("skipped", ""), lambda *a: ("skipped", ""), trips._watch_trip_event)
ok(r["trips"]["enabled"] and r["trips"]["scanned"] == 2, "watcher scans the trips folder")
ok(con.execute("SELECT COUNT(*) c FROM trip_candidates WHERE start_ts=?",
               (w_start.isoformat(timespec="seconds"),)).fetchone()["c"] == 1,
   "watcher-ingested events paired into a candidate")
r2 = watcher.run_once(con, lambda *a: ("skipped", ""), lambda *a: ("skipped", ""), trips._watch_trip_event)
ok(r2["trips"]["scanned"] == 0, "re-scan is idempotent (watched_files dedup)")
r3 = watcher.run_once(con, lambda *a: ("skipped", ""), lambda *a: ("skipped", ""))
ok("trips" not in r3, "run_once without trip_fn keeps the old shape (back-compat)")
con.commit()  # release the write lock before TestClient opens its own connections

# --- /mileage flow -------------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
client = TestClient(appmod.app)

page = client.get("/mileage")
ok(page.status_code == 200 and b"Trips waiting for approval" in page.content,
   "mileage page shows the pending-trips section")
ok(b"/mileage/scan" in page.content, "the page offers a 'Check for new trips' button")

# --- the update button: pull new drives now, without waiting for the ~60s watcher tick ----------
w2_start = (datetime.now() - timedelta(hours=3)).replace(microsecond=0)
w2_end = w2_start + timedelta(minutes=25)
(inbox / "w3.txt").write_text(f"connect,{w2_start.isoformat(timespec='seconds')},36.1627,-86.7816")
(inbox / "w4.txt").write_text(f"disconnect,{w2_end.isoformat(timespec='seconds')},35.9251,-86.8689")
before = con.execute("SELECT COUNT(*) c FROM trip_candidates").fetchone()["c"]
r = client.post("/mileage/scan", follow_redirects=False)
ok(r.status_code == 303 and "msg=" in r.headers["location"],
   "the update button redirects with a summary of what it found")
after = con.execute("SELECT COUNT(*) c FROM trip_candidates").fetchone()["c"]
ok(after == before + 1, "it picks up a drive dropped since the last check")
r = client.post("/mileage/scan", follow_redirects=False)
ok(r.status_code == 303 and "err=" not in r.headers["location"],
   "checking again with nothing new is harmless")
ok(con.execute("SELECT COUNT(*) c FROM trip_candidates").fetchone()["c"] == after,
   "...and doesn't duplicate the trips it already has")

# with no folder configured the button explains itself instead of silently doing nothing
db.set_setting(con, "trips_watch_folder", "")
con.commit()
r = client.post("/mileage/scan", follow_redirects=False)
ok(r.status_code == 303 and "err=" in r.headers["location"],
   "with no trips folder set, the button says so")
ok(b"/mileage/scan" not in client.get("/mileage").content,
   "the button is hidden entirely when no trips folder is set up")
db.set_setting(con, "trips_watch_folder", str(inbox))
con.commit()

cand = con.execute("SELECT * FROM trip_candidates WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
client.post(f"/mileage/trip/{cand['id']}/approve", data={"miles": "24.5", "purpose": "supplier run"},
            follow_redirects=False)
m = con.execute("SELECT * FROM mileage ORDER BY id DESC LIMIT 1").fetchone()
ok(m["miles"] == 24.5 and m["purpose"] == "supplier run" and m["date"] == cand["start_ts"][:10],
   "approve creates a mileage row with the edited miles and the trip's date")
ok(con.execute("SELECT status, mileage_id FROM trip_candidates WHERE id=?", (cand["id"],)).fetchone()["mileage_id"] == m["id"],
   "candidate links to the created log row")

cand2 = con.execute("SELECT * FROM trip_candidates WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
client.post(f"/mileage/trip/{cand2['id']}/dismiss", follow_redirects=False)
ok(con.execute("SELECT status FROM trip_candidates WHERE id=?", (cand2["id"],)).fetchone()["status"] == "dismissed",
   "dismiss marks the candidate without touching the log")

# --- saved routes ---------------------------------------------------------------
client.post("/mileage", data={"date": "2026-07-16", "miles": "23.4", "purpose": "McMaster pickup",
                              "from_loc": "Shop", "to_loc": "McMaster", "save_route": "1"},
            follow_redirects=False)
r = con.execute("SELECT * FROM saved_routes WHERE name='McMaster pickup'").fetchone()
ok(r is not None and r["miles"] == 23.4, "save-as-route remembers the trip")
n_before = con.execute("SELECT COUNT(*) c FROM mileage").fetchone()["c"]
client.post("/mileage/routes/log", data={"route_id": r["id"]}, follow_redirects=False)
last = con.execute("SELECT * FROM mileage ORDER BY id DESC LIMIT 1").fetchone()
ok(con.execute("SELECT COUNT(*) c FROM mileage").fetchone()["c"] == n_before + 1
   and last["purpose"] == "McMaster pickup" and last["miles"] == 23.4,
   "one-click route log adds today's trip")
client.post(f"/mileage/routes/{r['id']}/delete", follow_redirects=False)
ok(con.execute("SELECT COUNT(*) c FROM saved_routes WHERE id=?", (r["id"],)).fetchone()["c"] == 0,
   "saved route deletes (logged trips kept)")

con.close()
print("\nTRIP AUTOMATION TESTS DONE")
