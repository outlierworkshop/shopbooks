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
(inbox / "w1.txt").write_text("connect,2026-07-15T10:00:00,36.1627,-86.7816")
(inbox / "w2.txt").write_text("disconnect,2026-07-15T10:35:00,35.9251,-86.8689")
con.commit()
r = watcher.run_once(con, lambda *a: ("skipped", ""), lambda *a: ("skipped", ""), trips._watch_trip_event)
ok(r["trips"]["enabled"] and r["trips"]["scanned"] == 2, "watcher scans the trips folder")
ok(con.execute("SELECT COUNT(*) c FROM trip_candidates WHERE start_ts LIKE '2026-07-15%'").fetchone()["c"] == 1,
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
