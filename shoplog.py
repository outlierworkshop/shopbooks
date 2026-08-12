"""Shop-log time import: the ShopLog folder's daily CSVs -> time_entries rows.

Ben's end-of-day shop-log routine writes one CSV per day into a synced Dropbox folder
(`ShopLog/data/YYYY-MM-DD.csv`), header:

    date,start,end,minutes,client,job,work_type,billable,notes,friction

Those rows are already human-confirmed — the routine builds them WITH Ben — so unlike bank
statements they import straight into the time log. Records-only, no ledger impact (same class as
mileage rows), so the human-confirmed-posting invariant is untouched. `start`/`end` clock times and
`friction` stay in the ShopLog folder — ShopBooks' reports only use hours.

Corrections in that folder are new FILES, not edits: `YYYY-MM-DD-rev2.csv` supersedes the plain
file, `rev3` supersedes `rev2`, and concatenating blindly double-counts a corrected day. Importing
therefore keys every row on its source day via `time_entries.source` = `shoplog:<date>:rev<N>`:
a higher rev REPLACES that day's previously imported rows, a lower rev is skipped as superseded,
an equal rev re-imports idempotently (the watcher re-reads changed files) — and manually typed
entries (empty source) are never touched.

Jobs are auto-created from the log's job slugs. On creation only, the log's client name is matched
against existing customers (every word of the client name present in the customer's, exactly one
customer matching) — a missing or wrong link is editable on the Time page and never overwritten here.
"""
import csv
import io
import re

_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-rev(\d+))?\.csv$", re.IGNORECASE)
_REQUIRED = {"date", "minutes", "job", "work_type", "billable", "notes"}
SOURCE_PREFIX = "shoplog:"


def _words(name):
    """Name -> lowercase word list, splitting CamelCase too: 'NickLloyd' -> ['nick', 'lloyd']."""
    return [w.lower() for w in
            re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", str(name or ""))]


def match_customer(con, client):
    """The one customer whose name contains every word of the log's client name, or None.

    'NickLloyd' -> 'Nick Lloyd Basses LLC', 'Moriarty' -> 'JP Moriarty'. Requiring ALL words keeps
    'AndrewRyan' off 'Ryan L. Soltis'; requiring a UNIQUE hit refuses to guess between two matches.
    'Internal' is the shop itself, never a customer."""
    words = _words(client)
    if not words or words == ["internal"]:
        return None
    hits = [c["id"] for c in con.execute("SELECT id, name FROM customers")
            if all(w in _words(c["name"]) for w in words)]
    return hits[0] if len(hits) == 1 else None


def _imported_rev(con, date):
    """The highest rev already imported for a day, or 0."""
    best = 0
    for r in con.execute("SELECT DISTINCT source FROM time_entries WHERE source LIKE ?",
                         (f"{SOURCE_PREFIX}{date}:%",)):
        m = re.search(r":rev(\d+)$", r["source"] or "")
        if m:
            best = max(best, int(m.group(1)))
    return best


def _job_id(con, slug, client, cache):
    """Find-or-create the job for a log slug (case-insensitive); '' -> no job (a client with no
    job slug yet, e.g. a first contact). The customer link is set only when the job is CREATED."""
    slug = str(slug or "").strip()
    if not slug:
        return None
    key = slug.lower()
    if key in cache:
        return cache[key]
    row = con.execute("SELECT id FROM jobs WHERE lower(name)=?", (key,)).fetchone()
    if row:
        cache[key] = row["id"]
        return row["id"]
    cur = con.execute("INSERT INTO jobs(name,customer_id,notes) VALUES(?,?,?)",
                      (slug, match_customer(con, client), "from the shop log"))
    cache[key] = cur.lastrowid
    return cur.lastrowid


def ingest_csv(con, path, data):
    """Watcher callback: (con, Path, bytes) -> (status, note). One shop-log day file -> time rows."""
    m = _NAME_RE.match(path.name)
    if not m:
        return "error", "not a shop-log day file (expected YYYY-MM-DD.csv or YYYY-MM-DD-revN.csv)"
    date, rev = m.group(1), int(m.group(2) or 1)

    have = _imported_rev(con, date)
    if have > rev:
        return "duplicate", f"{date} already imported at rev{have} (this file is superseded)"

    try:
        text = data.decode("utf-8-sig", errors="replace")
    except Exception:
        return "error", "unreadable file"
    reader = csv.DictReader(io.StringIO(text))
    missing = _REQUIRED - set(reader.fieldnames or [])
    if missing:
        return "error", "missing column(s): " + ", ".join(sorted(missing))

    rows, skipped = [], 0
    for r in reader:
        try:
            minutes = int(str(r["minutes"]).strip())
        except (TypeError, ValueError):
            minutes = 0
        if str(r["date"]).strip() != date or minutes <= 0:
            skipped += 1   # a row from the wrong day, or no usable duration
            continue
        rows.append(r)
    if not rows:
        return "error", f"no usable time rows for {date}" + (f" ({skipped} skipped)" if skipped else "")

    # replace-per-day: this file is the day's record now. Manual entries have no source — untouched.
    con.execute("DELETE FROM time_entries WHERE source LIKE ?", (f"{SOURCE_PREFIX}{date}:%",))
    source = f"{SOURCE_PREFIX}{date}:rev{rev}"
    cache = {}
    for r in rows:
        con.execute(
            "INSERT INTO time_entries(date,hours,job_id,category,note,billable,rate_cents,source) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (date, round(int(str(r["minutes"]).strip()) / 60, 4),
             _job_id(con, r.get("job"), r.get("client"), cache),
             str(r.get("work_type") or "").strip().upper(),
             str(r.get("notes") or "").strip(),
             1 if str(r.get("billable") or "").strip().lower() == "y" else 0,
             None, source))

    note = f"{len(rows)} time entr{'y' if len(rows) == 1 else 'ies'} for {date}"
    if have == rev:
        note += " (re-imported)"
    elif have:
        note += f" (rev{rev} replaced rev{have})"
    elif rev > 1:
        note += f" (rev{rev})"
    if skipped:
        note += f", {skipped} row(s) skipped"
    return "imported", note
