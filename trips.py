"""Automatic mileage capture: phone Bluetooth events -> paired trips -> the mileage log.

The phone (MacroDroid/Tasker on Android — see docs/mileage-automation.md) drops one tiny text file
per car-Bluetooth event into a Dropbox folder that the ShopBooks folder watcher scans:

    connect,2026-07-14T08:32:11,36.1234,-86.5678

A `connect` followed by the next `disconnect` within MAX_TRIP_HOURS becomes a **trip candidate**:
road distance is routed via the public OSRM server, with a haversine x ROAD_FACTOR estimate as the
offline fallback (network optional everywhere — nothing raises, per the perdiem.py pattern), and the
endpoints are reverse-geocoded via Nominatim for a readable "where to where". Candidates wait on the
Mileage page for approval; approving inserts a normal `mileage` row. Records-only — no ledger impact.
"""
import math
from datetime import datetime

from logutil import log

MAX_TRIP_HOURS = 12       # a connect with no disconnect inside this window is an orphan, not a trip
MIN_TRIP_MILES = 0.1      # closer than this AND shorter than MIN_TRIP_MINUTES = driveway blip, skip
MIN_TRIP_MINUTES = 5
ROAD_FACTOR = 1.3         # haversine straight-line -> rough road miles when routing is unavailable

OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "ShopBooks/1.0 (local bookkeeping app; single user)"

_place_cache = {}         # (round4 lat, round4 lon) -> label; in-process, resets on restart


def parse_event(text):
    """One event line -> dict, or None if it isn't one. Format:
    `connect,ISO-timestamp,lat,lon` — extra trailing fields are tolerated (future-proofing)."""
    parts = [p.strip() for p in str(text).strip().splitlines()[0].split(",")] if str(text).strip() else []
    if len(parts) < 4:
        return None
    event = parts[0].lower()
    if event not in ("connect", "disconnect"):
        return None
    try:
        ts = datetime.fromisoformat(parts[1].replace("Z", "+00:00"))
        lat, lon = float(parts[2]), float(parts[3])
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"event": event, "ts": ts.replace(tzinfo=None).isoformat(timespec="seconds"),
            "lat": lat, "lon": lon}


def ingest_event_file(con, path, data):
    """Watcher callback: (con, Path, bytes) -> (status, note). One file = one event."""
    try:
        text = data.decode("utf-8-sig", errors="replace")
    except Exception:
        return "error", "unreadable file"
    ev = parse_event(text)
    if not ev:
        return "error", "not a trip event (want: connect,ISO-time,lat,lon)"
    dup = con.execute("SELECT 1 FROM trip_events WHERE event=? AND ts=?", (ev["event"], ev["ts"])).fetchone()
    if dup:
        return "duplicate", "event already ingested"
    con.execute("INSERT INTO trip_events(event, ts, lat, lon, raw) VALUES(?,?,?,?,?)",
                (ev["event"], ev["ts"], ev["lat"], ev["lon"], text.strip()[:200]))
    return "imported", f"{ev['event']} @ {ev['ts']}"


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles."""
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def route_miles(lat1, lon1, lat2, lon2):
    """(miles, source): road distance from the public OSRM router, or the haversine x ROAD_FACTOR
    estimate when routing is unavailable. Never raises."""
    try:
        import httpx
        url = f"{OSRM_URL}/{lon1},{lat1};{lon2},{lat2}"
        r = httpx.get(url, params={"overview": "false"}, timeout=10,
                      headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        routes = r.json().get("routes") or []
        if routes:
            return round(routes[0]["distance"] / 1609.344, 1), "osrm"
    except Exception as e:
        log.warning("OSRM routing failed, falling back to estimate: %s", e)
    return round(haversine_miles(lat1, lon1, lat2, lon2) * ROAD_FACTOR, 1), "estimate"


def reverse_place(lat, lon):
    """Short human label for a coordinate via Nominatim (cached; polite User-Agent per the usage
    policy). Falls back to the raw coordinates. Never raises."""
    key = (round(lat, 4), round(lon, 4))
    if key in _place_cache:
        return _place_cache[key]
    label = f"{lat:.4f}, {lon:.4f}"
    try:
        import httpx
        r = httpx.get(NOMINATIM_URL, params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 14},
                      timeout=10, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        a = r.json().get("address") or {}
        town = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or a.get("county") or ""
        spot = a.get("suburb") or a.get("neighbourhood") or a.get("road") or ""
        pretty = ", ".join(p for p in (spot, town) if p)
        if pretty:
            label = pretty
    except Exception as e:
        log.warning("reverse geocode failed for %s: %s", key, e)
    _place_cache[key] = label
    return label


def _minutes_between(ts1, ts2):
    return (datetime.fromisoformat(ts2) - datetime.fromisoformat(ts1)).total_seconds() / 60.0


def pair_events(con):
    """Chronologically pair pending connect -> next disconnect into trip candidates. Driveway blips
    (barely moved, barely any time) are consumed silently; a connect with no partner inside
    MAX_TRIP_HOURS is marked orphan once the window has passed. Returns candidates created."""
    pending = con.execute(
        "SELECT * FROM trip_events WHERE status='pending' ORDER BY ts, id").fetchall()
    created = 0
    used = set()
    for i, ev in enumerate(pending):
        if ev["id"] in used or ev["event"] != "connect":
            continue
        partner = None
        for nxt in pending[i + 1:]:
            if nxt["id"] in used:
                continue
            if nxt["event"] == "connect":
                break   # a newer drive started; this connect never got its disconnect
            if _minutes_between(ev["ts"], nxt["ts"]) <= MAX_TRIP_HOURS * 60:
                partner = nxt
            break
        if partner is None:
            # no disconnect (yet). Orphan it only once the pairing window has passed.
            age_min = _minutes_between(ev["ts"], datetime.now().isoformat(timespec="seconds"))
            if age_min > MAX_TRIP_HOURS * 60:
                con.execute("UPDATE trip_events SET status='orphan' WHERE id=?", (ev["id"],))
            continue
        used.add(ev["id"])
        used.add(partner["id"])
        con.execute("UPDATE trip_events SET status='paired' WHERE id IN (?,?)", (ev["id"], partner["id"]))
        crow = haversine_miles(ev["lat"], ev["lon"], partner["lat"], partner["lon"])
        mins = _minutes_between(ev["ts"], partner["ts"])
        if crow < MIN_TRIP_MILES and mins < MIN_TRIP_MINUTES:
            continue   # phone reconnected in the driveway; not a trip
        miles, source = route_miles(ev["lat"], ev["lon"], partner["lat"], partner["lon"])
        con.execute(
            "INSERT INTO trip_candidates(start_ts,end_ts,start_lat,start_lon,end_lat,end_lon,"
            "miles,distance_source,start_place,end_place) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ev["ts"], partner["ts"], ev["lat"], ev["lon"], partner["lat"], partner["lon"],
             miles, source, reverse_place(ev["lat"], ev["lon"]),
             reverse_place(partner["lat"], partner["lon"])))
        created += 1
    # disconnects that never found a connect and are old news
    for ev in pending:
        if ev["id"] in used or ev["event"] != "disconnect":
            continue
        age_min = _minutes_between(ev["ts"], datetime.now().isoformat(timespec="seconds"))
        if age_min > MAX_TRIP_HOURS * 60:
            con.execute("UPDATE trip_events SET status='orphan' WHERE id=?", (ev["id"],))
    return created


def _watch_trip_event(con, path, data):
    """The watcher's (con, path, data) -> (status, note) callback for the trips folder."""
    status, note = ingest_event_file(con, path, data)
    if status == "imported":
        pair_events(con)
    return status, note


def pending_candidates(con):
    return con.execute(
        "SELECT * FROM trip_candidates WHERE status='pending' ORDER BY start_ts DESC, id DESC").fetchall()


def approve(con, cand_id, miles, purpose, from_loc, to_loc):
    """Turn a candidate into a real mileage-log row. Returns the mileage id, or None if gone."""
    c = con.execute("SELECT * FROM trip_candidates WHERE id=? AND status='pending'", (cand_id,)).fetchone()
    if not c:
        return None
    date = c["start_ts"][:10]
    cur = con.execute("INSERT INTO mileage(date,miles,purpose,from_loc,to_loc) VALUES(?,?,?,?,?)",
                      (date, miles, purpose, from_loc, to_loc))
    con.execute("UPDATE trip_candidates SET status='approved', mileage_id=? WHERE id=?",
                (cur.lastrowid, cand_id))
    return cur.lastrowid


def dismiss(con, cand_id):
    con.execute("UPDATE trip_candidates SET status='dismissed' WHERE id=? AND status='pending'", (cand_id,))
