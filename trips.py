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
import re
import time
from datetime import datetime

from logutil import log

MAX_TRIP_HOURS = 12       # a connect with no disconnect inside this window is an orphan, not a trip
MIN_TRIP_MILES = 0.1      # closer than this AND shorter than MIN_TRIP_MINUTES = driveway blip, skip
MIN_TRIP_MINUTES = 5
ROAD_FACTOR = 1.3         # haversine straight-line -> rough road miles when routing is unavailable

OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "ShopBooks/1.0 (local bookkeeping app; single user)"
NOMINATIM_MIN_INTERVAL = 1.1   # their usage policy: at most 1 request/second

_place_cache = {}         # (round4 lat, round4 lon) -> label; in-process, resets on restart


# The phone's trip logger writes lines like:
#   [START] 8-6-26 14.28 | Location 42.3958823,-71.1172199 (Lat/Long: 42.3959688,-71.1172687)
# Dates are US M-D-YY and times are HH.MM. There are TWO coordinate pairs per line: `Location ...` is
# the trigger's anchor (it repeats verbatim across lines, so it's a cached/geofence fix) while
# `(Lat/Long: ...)` is the phone's actual position at that moment — that's the one worth measuring, so
# it's preferred and `Location` is only the fallback.
_HEAD_RE = re.compile(
    r"^\[(?P<ev>START|END|STOP)\]\s*(?P<a>\d{1,2})-(?P<b>\d{1,2})-(?P<y>\d{2,4})"
    r"\s+(?P<h>\d{1,2})[.:](?P<mi>\d{2})(?:[.:](?P<s>\d{2}))?", re.IGNORECASE)
_LATLON_RE = re.compile(r"Lat\s*/\s*Long\s*:\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_LOC_RE = re.compile(r"Location\s+(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)

_START_WORDS = {"start", "connect"}


def _mk(event, ts, lat, lon):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"event": event, "ts": ts.replace(tzinfo=None).isoformat(timespec="seconds"),
            "lat": lat, "lon": lon}


def parse_event(text):
    """One log line -> {event, ts, lat, lon}, or None if the line isn't a trip event.

    Understands both shapes the app has seen:
      * the phone trip logger's `[START] 8-6-26 14.28 | Location la,lo (Lat/Long: la,lo)`
      * the original one-event-per-file `connect,ISO-timestamp,lat,lon`
    Either way the result uses the internal `connect`/`disconnect` vocabulary, so the pairing and
    schema below are unchanged."""
    line = str(text).strip().splitlines()[0].strip() if str(text).strip() else ""
    if not line:
        return None

    m = _HEAD_RE.match(line)
    if m:
        a, b, y = int(m.group("a")), int(m.group("b")), int(m.group("y"))
        if y < 100:
            y += 2000
        month, day = a, b               # US M-D-YY
        if month > 12 and day <= 12:    # ...unless it can only be D-M (defensive; never seen here)
            month, day = day, month
        try:
            ts = datetime(y, month, day, int(m.group("h")), int(m.group("mi")),
                          int(m.group("s") or 0))
        except ValueError:
            return None
        coords = _LATLON_RE.search(line) or _LOC_RE.search(line)
        if not coords:
            return None
        event = "connect" if m.group("ev").lower() in _START_WORDS else "disconnect"
        try:
            return _mk(event, ts, float(coords.group(1)), float(coords.group(2)))
        except ValueError:
            return None

    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4 or parts[0].lower() not in ("connect", "disconnect"):
        return None
    try:
        ts = datetime.fromisoformat(parts[1].replace("Z", "+00:00"))
        return _mk(parts[0].lower(), ts, float(parts[2]), float(parts[3]))
    except ValueError:
        return None


def ingest_event_file(con, path, data):
    """Watcher callback: (con, Path, bytes) -> (status, note).

    A file may hold ONE event (the original one-file-per-event drop) or MANY — the phone's trip
    logger appends every event to a single `triplog.txt` that grows over time. The watcher re-reads a
    file whenever its mtime/size change, so this re-reads the whole log and inserts only the lines it
    hasn't seen: events are de-duplicated on (event, timestamp), which is what makes re-reading a
    growing log safe and idempotent."""
    try:
        text = data.decode("utf-8-sig", errors="replace")
    except Exception:
        return "error", "unreadable file"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "error", "empty file"
    new = dup = unparsed = 0
    for ln in lines:
        ev = parse_event(ln)
        if not ev:
            unparsed += 1
            continue
        if con.execute("SELECT 1 FROM trip_events WHERE event=? AND ts=?",
                       (ev["event"], ev["ts"])).fetchone():
            dup += 1
            continue
        con.execute("INSERT INTO trip_events(event, ts, lat, lon, raw) VALUES(?,?,?,?,?)",
                    (ev["event"], ev["ts"], ev["lat"], ev["lon"], ln.strip()[:200]))
        new += 1
    if new:
        note = f"{new} new event(s)"
        if dup:
            note += f", {dup} already logged"
        return "imported", note
    if dup:
        return "duplicate", f"no new events ({dup} already logged)"
    return "error", ("no trip events found — expected [START]/[END] lines "
                     "or connect,ISO-time,lat,lon")


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


def _house_number(raw):
    """First number of an OSM house_number. Multi-address nodes carry ranges like `319;321` or
    `11;13`, which would render as a broken address, so only the first is shown."""
    return str(raw or "").split(";")[0].split(",")[0].strip()


def address_label(a):
    """Nominatim `address` dict -> "14 William Street, Somerville, MA".

    A mileage log wants the street address, so `road` (+ house number) leads. Where OSM has no road
    for the point — a park, a parking lot, open country — it falls back to the neighbourhood/suburb
    name so the label still says something useful rather than nothing. State comes from the ISO code
    (`US-MA` -> `MA`) to keep the line short; the full state name is the fallback."""
    street = ""
    if a.get("road"):
        street = " ".join(p for p in (_house_number(a.get("house_number")), a["road"]) if p)
    if not street:
        street = a.get("neighbourhood") or a.get("suburb") or a.get("hamlet") or ""
    town = (a.get("city") or a.get("town") or a.get("village") or a.get("hamlet")
            or a.get("county") or "")
    iso = a.get("ISO3166-2-lvl4") or ""
    state = iso.split("-")[-1] if "-" in iso else (a.get("state") or "")
    # dict.fromkeys keeps order while dropping a repeat (e.g. street fell back to the town name)
    return ", ".join(dict.fromkeys(p for p in (street, town, state) if p))


def reverse_place(lat, lon):
    """Street address for a coordinate via Nominatim (cached; polite User-Agent per the usage
    policy). Falls back to the raw coordinates. Never raises — geocoding is optional, like every
    other network call here."""
    key = (round(lat, 5), round(lon, 5))   # ~1m: distinct addresses must not share a cache slot
    if key in _place_cache:
        return _place_cache[key]
    label = f"{lat:.4f}, {lon:.4f}"
    try:
        import httpx
        r = httpx.get(NOMINATIM_URL,
                      params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18,
                              "addressdetails": 1},
                      timeout=10, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        pretty = address_label(r.json().get("address") or {})
        if pretty:
            label = pretty
    except Exception as e:
        log.warning("reverse geocode failed for %s: %s", key, e)
    _place_cache[key] = label
    return label


def _paced_place(lat, lon):
    """reverse_place, but waits out Nominatim's 1-request-per-second usage policy before a lookup
    that will actually hit the network. Cached coordinates (a home address you leave from every day)
    cost nothing."""
    if (round(lat, 5), round(lon, 5)) not in _place_cache:
        time.sleep(NOMINATIM_MIN_INTERVAL)
    return reverse_place(lat, lon)


def refresh_places(con, limit=25):
    """Re-label pending trip candidates from their stored coordinates, for trips captured before the
    labels became street addresses. Returns how many candidates changed. Bounded by `limit` because
    each uncached point costs a paced network call — the page would otherwise hang on a long backlog."""
    rows = con.execute(
        "SELECT id, start_lat, start_lon, end_lat, end_lon, start_place, end_place "
        "FROM trip_candidates WHERE status='pending' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    changed = 0
    for r in rows:
        start = _paced_place(r["start_lat"], r["start_lon"])
        end = _paced_place(r["end_lat"], r["end_lon"])
        if start != r["start_place"] or end != r["end_place"]:
            con.execute("UPDATE trip_candidates SET start_place=?, end_place=? WHERE id=?",
                        (start, end, r["id"]))
            changed += 1
    return changed


def _minutes_between(ts1, ts2):
    return (datetime.fromisoformat(ts2) - datetime.fromisoformat(ts1)).total_seconds() / 60.0


def _within(lat1, lon1, lat2, lon2, radius_m):
    """True if two points are inside `radius_m` metres of each other."""
    return haversine_miles(lat1, lon1, lat2, lon2) * 1609.344 <= radius_m


def match_rule(con, start_lat, start_lon, end_lat, end_lon):
    """The standing rule that best describes this drive, or None.

    Rules match on COORDINATES, not the address text — OSM labels differ between neighbouring house
    numbers, so "14 William Street" today can read "16 William Street" tomorrow for the same driveway.

    A **route** rule (start AND destination both in range) beats a **destination** rule, because it's
    the more specific statement: "shop -> home is personal" should win over "anything ending at home
    is a commute". Within a kind, the rule whose destination is nearest wins, so a tight rule around
    one loading dock beats a broad one around the whole industrial park."""
    best = None
    best_key = None
    for r in con.execute("SELECT * FROM mileage_rules WHERE active=1"):
        if not _within(end_lat, end_lon, r["dest_lat"], r["dest_lon"], r["radius_m"]):
            continue
        is_route = r["match_kind"] == "route" and r["start_lat"] is not None
        if is_route and not _within(start_lat, start_lon, r["start_lat"], r["start_lon"], r["radius_m"]):
            continue
        dist = haversine_miles(end_lat, end_lon, r["dest_lat"], r["dest_lon"])
        key = (0 if is_route else 1, dist)      # route first, then nearest destination
        if best_key is None or key < best_key:
            best, best_key = r, key
    return best


def apply_rule(con, cand_id, rule):
    """Attach a matched rule to a candidate, and — if the rule is marked trusted (`auto_log`) — log
    the trip immediately instead of parking it in the approval queue. Returns True if it auto-logged."""
    con.execute("UPDATE trip_candidates SET rule_id=? WHERE id=?", (rule["id"], cand_id))
    if not rule["auto_log"]:
        return False
    c = con.execute("SELECT * FROM trip_candidates WHERE id=?", (cand_id,)).fetchone()
    approve(con, cand_id, c["miles"], rule["purpose"], c["start_place"], c["end_place"],
            business=rule["business"])
    return True


def apply_rules_to_pending(con):
    """Re-match every waiting trip against the current rules. Returns (matched, auto_logged).

    Called after a rule is added so the trip you built the rule from is sorted out immediately,
    instead of the rule only affecting drives you haven't taken yet."""
    matched = logged = 0
    for c in con.execute("SELECT * FROM trip_candidates WHERE status='pending'").fetchall():
        rule = match_rule(con, c["start_lat"], c["start_lon"], c["end_lat"], c["end_lon"])
        if not rule:
            continue
        matched += 1
        if apply_rule(con, c["id"], rule):
            logged += 1
    return matched, logged


def pair_events(con):
    """Chronologically pair pending connect -> disconnect into trip candidates. Driveway blips
    (barely moved, barely any time) are consumed silently; a connect with no partner inside
    MAX_TRIP_HOURS is marked orphan once the window has passed. Returns candidates created.

    A connect is NOT simply paired with the very next disconnect: the phone's trip logger writes a
    spurious [END] in the same minute as each [START] (same spot, zero minutes), with the real [END]
    arriving when the drive actually finishes. So the partner is the first disconnect before the next
    connect that isn't a blip relative to the start; the skipped blips are consumed as noise of the
    same trip. When every disconnect in the span is a blip, the last one is taken and the whole thing
    falls through to the existing driveway-blip rule — consumed, no candidate."""
    pending = con.execute(
        "SELECT * FROM trip_events WHERE status='pending' ORDER BY ts, id").fetchall()
    created = 0
    used = set()
    for i, ev in enumerate(pending):
        if ev["id"] in used or ev["event"] != "connect":
            continue
        partner = None
        noise = []   # blip disconnects between the connect and its real partner
        for nxt in pending[i + 1:]:
            if nxt["id"] in used:
                continue
            if nxt["event"] == "connect":
                break   # a newer drive started; whatever we have is this trip's best end
            if _minutes_between(ev["ts"], nxt["ts"]) > MAX_TRIP_HOURS * 60:
                break   # too far out to belong to this drive
            is_blip = (haversine_miles(ev["lat"], ev["lon"], nxt["lat"], nxt["lon"]) < MIN_TRIP_MILES
                       and _minutes_between(ev["ts"], nxt["ts"]) < MIN_TRIP_MINUTES)
            if partner is not None:
                noise.append(partner)
            partner = nxt
            if not is_blip:
                break   # the real end of the drive
        if partner is None:
            # no disconnect (yet). Orphan it only once the pairing window has passed.
            age_min = _minutes_between(ev["ts"], datetime.now().isoformat(timespec="seconds"))
            if age_min > MAX_TRIP_HOURS * 60:
                con.execute("UPDATE trip_events SET status='orphan' WHERE id=?", (ev["id"],))
            continue
        consumed = [ev["id"], partner["id"]] + [n["id"] for n in noise]
        used.update(consumed)
        con.execute("UPDATE trip_events SET status='paired' WHERE id IN (%s)"
                    % ",".join("?" * len(consumed)), consumed)
        crow = haversine_miles(ev["lat"], ev["lon"], partner["lat"], partner["lon"])
        mins = _minutes_between(ev["ts"], partner["ts"])
        if crow < MIN_TRIP_MILES and mins < MIN_TRIP_MINUTES:
            continue   # phone reconnected in the driveway; not a trip
        miles, source = route_miles(ev["lat"], ev["lon"], partner["lat"], partner["lon"])
        cur = con.execute(
            "INSERT INTO trip_candidates(start_ts,end_ts,start_lat,start_lon,end_lat,end_lon,"
            "miles,distance_source,start_place,end_place) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ev["ts"], partner["ts"], ev["lat"], ev["lon"], partner["lat"], partner["lon"],
             miles, source, reverse_place(ev["lat"], ev["lon"]),
             reverse_place(partner["lat"], partner["lon"])))
        rule = match_rule(con, ev["lat"], ev["lon"], partner["lat"], partner["lon"])
        if rule:
            apply_rule(con, cur.lastrowid, rule)   # auto-logs when the rule is trusted
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
    """Pending trips, each carrying the matched rule's suggestion (rule_name/purpose/business) so the
    page can pre-fill it. An unmatched trip suggests **personal**: a drive only becomes a deduction
    when you say so, which is the safe direction to be wrong in. A rule that says otherwise wins."""
    return con.execute(
        "SELECT c.*, r.name rule_name, r.purpose rule_purpose, "
        "       COALESCE(r.business, 0) suggested_business "
        "FROM trip_candidates c LEFT JOIN mileage_rules r ON r.id = c.rule_id "
        "WHERE c.status='pending' ORDER BY c.start_ts DESC, c.id DESC").fetchall()


def approve(con, cand_id, miles, purpose, from_loc, to_loc, business=0):
    """Turn a candidate into a real mileage-log row. Returns the mileage id, or None if gone.
    Defaults to PERSONAL (`business=0`): a trip is only deducted when something — you, or a rule —
    positively says it's business. `business=0` still logs the trip, it just isn't deducted."""
    c = con.execute("SELECT * FROM trip_candidates WHERE id=? AND status='pending'", (cand_id,)).fetchone()
    if not c:
        return None
    date = c["start_ts"][:10]
    cur = con.execute(
        "INSERT INTO mileage(date,miles,purpose,from_loc,to_loc,business) VALUES(?,?,?,?,?,?)",
        (date, miles, purpose, from_loc, to_loc, 1 if business else 0))
    con.execute("UPDATE trip_candidates SET status='approved', mileage_id=? WHERE id=?",
                (cur.lastrowid, cand_id))
    return cur.lastrowid


def dismiss(con, cand_id):
    con.execute("UPDATE trip_candidates SET status='dismissed' WHERE id=? AND status='pending'", (cand_id,))
