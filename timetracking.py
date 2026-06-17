"""Time tracking & job costing (managerial, NOT part of the double-entry ledger).

Like mileage, tracked time is informational: it never posts journal entries, so
the ledger invariants are untouched. Time is logged manually against optional
**jobs** (which can link to a customer) and free-text work **categories**, with an
optional billable flag + per-entry rate. Billable value = hours x rate (per-entry
`rate_cents`, else the `default_hourly_rate` setting). All money is integer cents.

Phase 1 reports hours and billable value. Full per-job profitability (materials +
income alongside labor) is a later phase that tags ledger transactions with a job.
"""
import db


def default_rate_cents(con):
    """The fallback billing rate (cents/hour) from settings; 0 if unset/invalid."""
    raw = db.get_setting(con, "default_hourly_rate", "0") or "0"
    try:
        return round(float(raw) * 100)
    except ValueError:
        return 0


def _value_cents(hours, billable, rate_cents, default_cents):
    """Billable dollar value of one entry, in cents. Non-billable time is worth 0."""
    if not billable:
        return 0
    rate = rate_cents if rate_cents is not None else default_cents
    return round(hours * rate)


# --- writes (kept here so routes stay thin and the logic is unit-testable) ----

def add_job(con, name, customer_id=None, notes=""):
    cur = con.execute("INSERT INTO jobs(name,customer_id,notes) VALUES(?,?,?)",
                      (name.strip(), customer_id or None, notes.strip()))
    return cur.lastrowid


def set_job_status(con, job_id, status):
    con.execute("UPDATE jobs SET status=? WHERE id=?",
                (status if status in ("active", "done") else "active", job_id))


def add_entry(con, date, hours, job_id=None, category="", note="",
              billable=False, rate_cents=None):
    con.execute(
        "INSERT INTO time_entries(date,hours,job_id,category,note,billable,rate_cents) "
        "VALUES(?,?,?,?,?,?,?)",
        (date, hours, job_id or None, category.strip(), note.strip(),
         1 if billable else 0, rate_cents))


# --- reads / aggregation -----------------------------------------------------

def _rows(con, start=None, end=None, job_id=None):
    q = ("SELECT t.*, j.name job_name, c.name customer_name "
         "FROM time_entries t LEFT JOIN jobs j ON j.id=t.job_id "
         "LEFT JOIN customers c ON c.id=j.customer_id WHERE 1=1")
    args = []
    if start and end:
        q += " AND t.date BETWEEN ? AND ?"
        args += [start, end]
    if job_id is not None:
        q += " AND t.job_id=?"
        args.append(job_id)
    q += " ORDER BY t.date DESC, t.id DESC"
    return con.execute(q, args).fetchall()


def summary(con, start=None, end=None):
    """Totals and breakdowns for a date range (or all time): total hours, billable
    hours, billable value (cents), and per-category and per-job rollups."""
    dft = default_rate_cents(con)
    total_h = bill_h = 0.0
    value = 0
    cat, job = {}, {}
    for r in _rows(con, start, end):
        v = _value_cents(r["hours"], r["billable"], r["rate_cents"], dft)
        total_h += r["hours"]
        value += v
        if r["billable"]:
            bill_h += r["hours"]
        ck = r["category"] or "(uncategorized)"
        c = cat.setdefault(ck, {"category": ck, "hours": 0.0, "billable_hours": 0.0, "billable_value": 0})
        c["hours"] += r["hours"]
        c["billable_value"] += v
        if r["billable"]:
            c["billable_hours"] += r["hours"]
        jname = r["job_name"] or "(no job)"
        j = job.setdefault(r["job_id"], {"job_id": r["job_id"], "job": jname,
                                         "customer": r["customer_name"], "hours": 0.0,
                                         "billable_hours": 0.0, "billable_value": 0})
        j["hours"] += r["hours"]
        j["billable_value"] += v
        if r["billable"]:
            j["billable_hours"] += r["hours"]

    def tidy(d):
        d["hours"] = round(d["hours"], 2)
        d["billable_hours"] = round(d["billable_hours"], 2)
        return d

    return {
        "start": start, "end": end,
        "total_hours": round(total_h, 2),
        "billable_hours": round(bill_h, 2),
        "billable_value": value,
        "by_category": [tidy(c) for c in sorted(cat.values(), key=lambda x: x["hours"], reverse=True)],
        "by_job": [tidy(j) for j in sorted(job.values(), key=lambda x: x["hours"], reverse=True)],
    }


def list_entries(con, start=None, end=None, limit=200):
    """Recent entries (newest first) with job/customer names and computed value."""
    dft = default_rate_cents(con)
    out = []
    for r in _rows(con, start, end)[:limit]:
        out.append({"id": r["id"], "date": r["date"], "hours": r["hours"],
                    "job": r["job_name"], "job_id": r["job_id"], "customer": r["customer_name"],
                    "category": r["category"], "note": r["note"], "billable": bool(r["billable"]),
                    "value": _value_cents(r["hours"], r["billable"], r["rate_cents"], dft)})
    return out


def categories(con):
    """Distinct work categories used so far (for the entry-form autocomplete)."""
    return [r["category"] for r in con.execute(
        "SELECT DISTINCT category FROM time_entries WHERE category!='' ORDER BY category").fetchall()]


def jobs_overview(con):
    """All jobs with their hours + billable value rolled up, for the Jobs page."""
    dft = default_rate_cents(con)
    agg = {}
    for r in con.execute("SELECT job_id, hours, billable, rate_cents FROM time_entries").fetchall():
        a = agg.setdefault(r["job_id"], {"hours": 0.0, "value": 0})
        a["hours"] += r["hours"]
        a["value"] += _value_cents(r["hours"], r["billable"], r["rate_cents"], dft)
    rows = con.execute(
        "SELECT j.*, c.name customer_name FROM jobs j LEFT JOIN customers c ON c.id=j.customer_id "
        "ORDER BY (j.status='done'), j.created_at DESC").fetchall()
    out = []
    for j in rows:
        a = agg.get(j["id"], {"hours": 0.0, "value": 0})
        out.append({"id": j["id"], "name": j["name"], "customer": j["customer_name"],
                    "status": j["status"], "hours": round(a["hours"], 2), "billable_value": a["value"]})
    return out


def job_report(con, job_id):
    """One job's detail: the job row, its entries (newest first), and totals.
    Returns None if the job doesn't exist."""
    job = con.execute(
        "SELECT j.*, c.name customer_name FROM jobs j LEFT JOIN customers c ON c.id=j.customer_id "
        "WHERE j.id=?", (job_id,)).fetchone()
    if not job:
        return None
    dft = default_rate_cents(con)
    total_h = bill_h = 0.0
    value = 0
    entries = []
    for r in _rows(con, job_id=job_id):
        v = _value_cents(r["hours"], r["billable"], r["rate_cents"], dft)
        total_h += r["hours"]
        value += v
        if r["billable"]:
            bill_h += r["hours"]
        entries.append({"id": r["id"], "date": r["date"], "hours": r["hours"],
                        "category": r["category"], "note": r["note"],
                        "billable": bool(r["billable"]), "value": v})
    return {"job": job, "entries": entries, "total_hours": round(total_h, 2),
            "billable_hours": round(bill_h, 2), "billable_value": value}
