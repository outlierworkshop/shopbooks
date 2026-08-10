"""Mileage log, time tracking, and jobs routes."""
from datetime import date as date_cls, datetime
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import db
import ledger
import timetracking
import trips as tripsmod
import watcher
from webutil import ctx, get_con, safe_redirect, templates

router = APIRouter()

@router.get("/mileage", response_class=HTMLResponse)
def mileage(request: Request, msg: str = "", err: str = "", con=Depends(get_con)):
    year = date_cls.today().year
    trips = con.execute("SELECT * FROM mileage ORDER BY date DESC, id DESC").fetchall()
    rate = float(db.get_setting(con, "mileage_rate", "0.70"))
    # the deduction follows BUSINESS miles only; the year's total is shown alongside so a big gap
    # between the two is visible rather than silently assumed
    ytd = con.execute("SELECT COALESCE(SUM(miles),0) m FROM mileage WHERE date LIKE ? AND business=1",
                      (f"{year}%",)).fetchone()["m"]
    ytd_all = con.execute("SELECT COALESCE(SUM(miles),0) m FROM mileage WHERE date LIKE ?",
                          (f"{year}%",)).fetchone()["m"]
    candidates = []
    for c in tripsmod.pending_candidates(con):
        mins = (datetime.fromisoformat(c["end_ts"]) - datetime.fromisoformat(c["start_ts"])).total_seconds() / 60
        candidates.append({**dict(c), "minutes": round(mins)})
    return templates.TemplateResponse(request, "mileage.html", ctx(
        request, con, trips=trips, rate=rate, ytd=ytd, ytd_all=ytd_all, year=year,
        deduction_cents=round(ytd * rate * 100), candidates=candidates,
        rules=con.execute("SELECT * FROM mileage_rules ORDER BY active DESC, name").fetchall(),
        routes=con.execute("SELECT * FROM saved_routes ORDER BY name").fetchall(),
        trips_watch_on=bool(db.get_setting(con, "trips_watch_folder", "").strip()),
        msg=msg, err=err))

@router.post("/mileage")
def mileage_add(date: str = Form(...), miles: float = Form(...), purpose: str = Form(""),
                from_loc: str = Form(""), to_loc: str = Form(""), save_route: str = Form(""),
                business: str = Form("1"), con=Depends(get_con)):
    con.execute("INSERT INTO mileage(date,miles,purpose,from_loc,to_loc,business) VALUES(?,?,?,?,?,?)",
                (ledger.normalize_date(date), miles, purpose, from_loc, to_loc,
                 0 if business in ("0", "", "off") else 1))
    if save_route:  # remember this trip as a one-click route
        name = purpose.strip() or f"{from_loc.strip()} → {to_loc.strip()}".strip(" →")
        if name and not con.execute("SELECT 1 FROM saved_routes WHERE name=?", (name,)).fetchone():
            con.execute("INSERT INTO saved_routes(name,from_loc,to_loc,miles) VALUES(?,?,?,?)",
                        (name, from_loc, to_loc, miles))
    con.commit()
    return RedirectResponse("/mileage", status_code=303)

@router.post("/mileage/update")
def mileage_update(trip_id: int = Form(...), date: str = Form(...), miles: float = Form(...),
                   purpose: str = Form(""), from_loc: str = Form(""), to_loc: str = Form(""),
                   business: str = Form("1"), con=Depends(get_con)):
    """Edit a logged trip in place — fix a purpose, correct the miles, or reclassify it as personal
    (which takes it straight out of the deduction). Also how a trip logged before street addresses
    existed gets its from/to tidied up."""
    if miles <= 0:
        return safe_redirect("/mileage", err="Miles must be greater than zero.")
    con.execute("UPDATE mileage SET date=?, miles=?, purpose=?, from_loc=?, to_loc=?, business=? "
                "WHERE id=?",
                (ledger.normalize_date(date), miles, purpose.strip(), from_loc.strip(),
                 to_loc.strip(), 0 if business in ("0", "", "off") else 1, trip_id))
    con.commit()
    return safe_redirect("/mileage", msg="Trip updated.")

@router.post("/mileage/delete")
def mileage_delete(trip_id: int = Form(...), con=Depends(get_con)):
    # trip_candidates.mileage_id references this row, so it has to be cleared first or the DELETE
    # raises a FOREIGN KEY error (PRAGMA foreign_keys=ON) — which is exactly what a trip logged from
    # the phone would do. The candidate stays 'approved' rather than returning to the queue: it's
    # been dealt with, and a trusted rule would otherwise just log it straight back.
    con.execute("UPDATE trip_candidates SET mileage_id=NULL WHERE mileage_id=?", (trip_id,))
    con.execute("DELETE FROM mileage WHERE id=?", (trip_id,))
    con.commit()
    return RedirectResponse("/mileage", status_code=303)

@router.post("/mileage/trip/{cand_id}/approve")
def mileage_trip_approve(cand_id: int, miles: float = Form(...), purpose: str = Form(""),
                         business: str = Form("1"), con=Depends(get_con)):
    c = con.execute("SELECT * FROM trip_candidates WHERE id=?", (cand_id,)).fetchone()
    if not c:
        return RedirectResponse("/mileage", status_code=303)
    if miles <= 0:
        return safe_redirect("/mileage", err="Miles must be greater than zero.")
    is_biz = 0 if business in ("0", "", "off") else 1
    tripsmod.approve(con, cand_id, miles, purpose.strip(), c["start_place"], c["end_place"],
                     business=is_biz)
    con.commit()
    kind = "business" if is_biz else "personal (not deducted)"
    return safe_redirect("/mileage", msg=f"Trip logged: {miles:g} mi on {c['start_ts'][:10]} — {kind}.")

@router.post("/mileage/scan")
def mileage_scan(con=Depends(get_con)):
    """Check the trips folder for new drives right now, instead of waiting for the ~60s watcher tick.

    Does the whole pipeline, not just the file read: pairing runs even when no new file arrived (a
    dangling start may have aged past its window), and standing rules are re-applied so trips that
    were captured before a rule existed get classified too."""
    folder = db.get_setting(con, "trips_watch_folder", "")
    if not str(folder).strip():
        return safe_redirect("/mileage", err="No trips folder is set yet — add one in "
                                             "Settings - Folder watchers.")
    r = watcher.scan_folder(con, folder, "trip", {".txt", ".csv"}, tripsmod._watch_trip_event)
    tripsmod.pair_events(con)
    matched, auto = tripsmod.apply_rules_to_pending(con)
    con.commit()
    waiting = len(tripsmod.pending_candidates(con))
    if r["errors"]:
        return safe_redirect("/mileage", err="Trip log couldn't be read: " + "; ".join(r["errors"][:2]))
    bits = []
    if r["scanned"]:
        bits.append(", ".join(f"{v} {k}" for k, v in r["counts"].items()))
    if auto:
        bits.append(f"{auto} auto-logged by a rule")
    bits.append(f"{waiting} trip(s) waiting" if waiting else "nothing waiting for approval")
    return safe_redirect("/mileage", msg="Checked for new trips: " + " · ".join(bits))

@router.post("/mileage/rules/from-trip/{cand_id}")
def mileage_rule_from_trip(cand_id: int, name: str = Form(""), purpose: str = Form(""),
                           match_kind: str = Form("destination"), radius_m: int = Form(150),
                           business: str = Form("1"), auto_log: str = Form(""),
                           con=Depends(get_con)):
    """Create a standing rule from a detected trip — the trip supplies the coordinates, which is the
    only practical way to get them (nobody types latitude by hand)."""
    c = con.execute("SELECT * FROM trip_candidates WHERE id=?", (cand_id,)).fetchone()
    if not c:
        return RedirectResponse("/mileage", status_code=303)
    label = name.strip() or purpose.strip() or (c["end_place"] or "Trip rule")
    kind = "route" if match_kind == "route" else "destination"
    con.execute(
        "INSERT INTO mileage_rules(name,match_kind,dest_lat,dest_lon,start_lat,start_lon,radius_m,"
        "purpose,business,auto_log) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (label, kind, c["end_lat"], c["end_lon"],
         c["start_lat"] if kind == "route" else None, c["start_lon"] if kind == "route" else None,
         max(25, min(int(radius_m or 150), 20000)), purpose.strip(),
         0 if business in ("0", "", "off") else 1, 1 if auto_log else 0))
    # classify everything already waiting, so the trip you made the rule from is sorted out too
    matched, logged = tripsmod.apply_rules_to_pending(con)
    con.commit()
    extra = f" {matched} waiting trip(s) matched" + (f", {logged} auto-logged" if logged else "") if matched else ""
    return safe_redirect("/mileage", msg=f"Rule '{label}' saved.{extra}")

@router.post("/mileage/rules/{rule_id}/delete")
def mileage_rule_delete(rule_id: int, con=Depends(get_con)):
    con.execute("UPDATE trip_candidates SET rule_id=NULL WHERE rule_id=?", (rule_id,))
    con.execute("DELETE FROM mileage_rules WHERE id=?", (rule_id,))
    con.commit()
    return safe_redirect("/mileage", msg="Rule deleted (logged trips are unchanged).")

@router.post("/mileage/rules/{rule_id}/toggle")
def mileage_rule_toggle(rule_id: int, con=Depends(get_con)):
    con.execute("UPDATE mileage_rules SET active = CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",
                (rule_id,))
    con.commit()
    return safe_redirect("/mileage", msg="Rule updated.")

@router.post("/mileage/trips/refresh-places")
def mileage_refresh_places(con=Depends(get_con)):
    """Re-label pending trips from their stored coordinates — for trips captured before the labels
    became street addresses, so they don't have to be re-driven to get a proper address."""
    try:
        changed = tripsmod.refresh_places(con)
    except Exception as e:                       # geocoding is optional everywhere; never 500 here
        return safe_redirect("/mileage", err=f"Could not look up addresses right now ({e}).")
    con.commit()
    if not changed:
        return safe_redirect("/mileage", msg="Locations are already up to date.")
    return safe_redirect("/mileage", msg=f"Updated the locations on {changed} trip(s).")

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
