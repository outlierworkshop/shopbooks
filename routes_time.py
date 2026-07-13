"""Mileage log, time tracking, and jobs routes."""
from datetime import date as date_cls, datetime
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import db
import ledger
import timetracking
import trips as tripsmod
from webutil import ctx, get_con, safe_redirect, templates

router = APIRouter()

@router.get("/mileage", response_class=HTMLResponse)
def mileage(request: Request, msg: str = "", err: str = "", con=Depends(get_con)):
    year = date_cls.today().year
    trips = con.execute("SELECT * FROM mileage ORDER BY date DESC, id DESC").fetchall()
    rate = float(db.get_setting(con, "mileage_rate", "0.70"))
    ytd = con.execute("SELECT COALESCE(SUM(miles),0) m FROM mileage WHERE date LIKE ?",
                      (f"{year}%",)).fetchone()["m"]
    candidates = []
    for c in tripsmod.pending_candidates(con):
        mins = (datetime.fromisoformat(c["end_ts"]) - datetime.fromisoformat(c["start_ts"])).total_seconds() / 60
        candidates.append({**dict(c), "minutes": round(mins)})
    return templates.TemplateResponse(request, "mileage.html", ctx(
        request, con, trips=trips, rate=rate, ytd=ytd, year=year,
        deduction_cents=round(ytd * rate * 100), candidates=candidates,
        routes=con.execute("SELECT * FROM saved_routes ORDER BY name").fetchall(),
        trips_watch_on=bool(db.get_setting(con, "trips_watch_folder", "").strip()),
        msg=msg, err=err))

@router.post("/mileage")
def mileage_add(date: str = Form(...), miles: float = Form(...), purpose: str = Form(""),
                from_loc: str = Form(""), to_loc: str = Form(""), save_route: str = Form(""),
                con=Depends(get_con)):
    con.execute("INSERT INTO mileage(date,miles,purpose,from_loc,to_loc) VALUES(?,?,?,?,?)",
                (ledger.normalize_date(date), miles, purpose, from_loc, to_loc))
    if save_route:  # remember this trip as a one-click route
        name = purpose.strip() or f"{from_loc.strip()} → {to_loc.strip()}".strip(" →")
        if name and not con.execute("SELECT 1 FROM saved_routes WHERE name=?", (name,)).fetchone():
            con.execute("INSERT INTO saved_routes(name,from_loc,to_loc,miles) VALUES(?,?,?,?)",
                        (name, from_loc, to_loc, miles))
    con.commit()
    return RedirectResponse("/mileage", status_code=303)

@router.post("/mileage/delete")
def mileage_delete(trip_id: int = Form(...), con=Depends(get_con)):
    con.execute("DELETE FROM mileage WHERE id=?", (trip_id,))
    con.commit()
    return RedirectResponse("/mileage", status_code=303)

@router.post("/mileage/trip/{cand_id}/approve")
def mileage_trip_approve(cand_id: int, miles: float = Form(...), purpose: str = Form(""),
                         con=Depends(get_con)):
    c = con.execute("SELECT * FROM trip_candidates WHERE id=?", (cand_id,)).fetchone()
    if not c:
        return RedirectResponse("/mileage", status_code=303)
    if miles <= 0:
        return safe_redirect("/mileage", err="Miles must be greater than zero.")
    tripsmod.approve(con, cand_id, miles, purpose.strip(), c["start_place"], c["end_place"])
    con.commit()
    return safe_redirect("/mileage", msg=f"Trip logged: {miles:g} mi on {c['start_ts'][:10]}.")

@router.post("/mileage/trip/{cand_id}/dismiss")
def mileage_trip_dismiss(cand_id: int, con=Depends(get_con)):
    tripsmod.dismiss(con, cand_id)
    con.commit()
    return safe_redirect("/mileage", msg="Trip dismissed (not logged).")

@router.post("/mileage/routes/log")
def mileage_route_log(route_id: int = Form(...), con=Depends(get_con)):
    """One-click: log a saved route as today's trip."""
    r = con.execute("SELECT * FROM saved_routes WHERE id=?", (route_id,)).fetchone()
    if not r:
        return RedirectResponse("/mileage", status_code=303)
    con.execute("INSERT INTO mileage(date,miles,purpose,from_loc,to_loc) VALUES(?,?,?,?,?)",
                (date_cls.today().isoformat(), r["miles"], r["name"], r["from_loc"], r["to_loc"]))
    con.commit()
    return safe_redirect("/mileage", msg=f"Logged '{r['name']}' — {r['miles']:g} mi today.")

@router.post("/mileage/routes/{route_id}/delete")
def mileage_route_delete(route_id: int, con=Depends(get_con)):
    con.execute("DELETE FROM saved_routes WHERE id=?", (route_id,))
    con.commit()
    return RedirectResponse("/mileage", status_code=303)

@router.get("/time", response_class=HTMLResponse)
def time_page(request: Request, start: str = "", end: str = "", con=Depends(get_con)):
    year = date_cls.today().year
    start = start or f"{year}-01-01"
    end = end or f"{year}-12-31"
    return templates.TemplateResponse(request, "time.html", ctx(
        request, con, summary=timetracking.summary(con, start, end),
        entries=timetracking.list_entries(con, start, end), start=start, end=end, year=year,
        jobs=con.execute("SELECT id, name FROM jobs WHERE status='active' ORDER BY created_at DESC").fetchall(),
        cats=timetracking.categories(con),
        default_rate=db.get_setting(con, "default_hourly_rate", "0")))

@router.post("/time")
def time_add(date: str = Form(...), hours: float = Form(...), job_id: str = Form(""),
             category: str = Form(""), note: str = Form(""), billable: str = Form(""),
             rate: str = Form(""), con=Depends(get_con)):
    rate_cents = None
    if str(rate).strip():
        try:
            rate_cents = ledger.parse_amount_to_cents(rate)
        except ValueError:
            rate_cents = None
    timetracking.add_entry(
        con, ledger.normalize_date(date), hours,
        job_id=int(job_id) if job_id.strip() else None,
        category=category, note=note, billable=bool(billable), rate_cents=rate_cents)
    con.commit()
    return RedirectResponse("/time", status_code=303)

@router.post("/time/delete")
def time_delete(entry_id: int = Form(...), con=Depends(get_con)):
    con.execute("DELETE FROM time_entries WHERE id=?", (entry_id,))
    con.commit()
    return RedirectResponse("/time", status_code=303)

@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, con=Depends(get_con)):
    return templates.TemplateResponse(request, "jobs.html", ctx(
        request, con, jobs=timetracking.jobs_overview(con),
        customers=con.execute("SELECT id, name FROM customers ORDER BY name").fetchall()))

@router.post("/jobs")
def jobs_add(name: str = Form(...), customer_id: str = Form(""), notes: str = Form(""), con=Depends(get_con)):
    if name.strip():
        timetracking.add_job(con, name,
                             customer_id=int(customer_id) if customer_id.strip() else None, notes=notes)
        con.commit()
    return RedirectResponse("/jobs", status_code=303)

@router.post("/jobs/status")
def jobs_status(job_id: int = Form(...), status: str = Form(...), con=Depends(get_con)):
    timetracking.set_job_status(con, job_id, status)
    con.commit()
    return RedirectResponse("/jobs", status_code=303)

@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int, con=Depends(get_con)):
    rep = timetracking.job_report(con, job_id)
    if not rep:
        return RedirectResponse("/jobs", status_code=303)
    return templates.TemplateResponse(request, "job_detail.html", ctx(request, con, rep=rep))
