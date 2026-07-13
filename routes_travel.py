"""Per-diem travel routes: trips, GSA rate lookup, per-diem vs actuals comparison.
Records only — like the mileage log, nothing here posts to the ledger; the per-diem figure is a
tax-time deduction the advisor applies."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import ledger
import perdiem
from webutil import ctx, get_con, safe_redirect, templates

router = APIRouter()


@router.get("/travel", response_class=HTMLResponse)
def travel_page(request: Request, msg: str = "", err: str = "", con=Depends(get_con)):
    trips = []
    for t in perdiem.list_trips(con):
        s = perdiem.trip_summary(con, t)
        trips.append({**dict(t), "perdiem_total": s["perdiem_total_cents"],
                      "meals_total": s["actuals"]["meals_total_cents"],
                      "days": s["breakdown"]["days"], "winner": s["winner"]})
    return templates.TemplateResponse(request, "travel.html", ctx(
        request, con, trips=trips, msg=msg, err=err))


@router.post("/travel")
def travel_add(destination: str = Form(...), city: str = Form(""), state: str = Form(""),
               zip_code: str = Form(""), start_date: str = Form(...), end_date: str = Form(...),
               purpose: str = Form(""), manual_mie: str = Form(""), con=Depends(get_con)):
    try:
        sd = ledger.normalize_date(start_date)
        ed = ledger.normalize_date(end_date)
        perdiem.mie_breakdown(sd, ed, 0)   # validates the date range early
    except ValueError as e:
        return safe_redirect("/travel", err=f"Couldn't read that: {e}")
    if not destination.strip():
        return safe_redirect("/travel", err="Give the trip a destination.")

    city, state, zip_code = city.strip(), state.strip().upper()[:2], zip_code.strip()
    if manual_mie.strip():   # explicit rate wins; no lookup
        try:
            mie = abs(ledger.parse_amount_to_cents(manual_mie))
        except ValueError:
            return safe_redirect("/travel", err="Couldn't read the manual M&IE rate.")
        if mie == 0:
            return safe_redirect("/travel", err="The manual M&IE rate can't be zero.")
        lodging, source, note = None, "manual", "manually entered rate"
    else:
        looked = perdiem.fetch_gsa(con, city, state, zip_code, sd) if (zip_code or (city and state)) else None
        if looked:
            mie, lodging, source, note = looked["mie_cents"], looked["lodging_cents"], "gsa", looked["note"]
        else:
            mie, lodging, source = perdiem.STANDARD_MIE_CENTS, perdiem.STANDARD_LODGING_CENTS, "standard"
            note = ("standard CONUS rate (no locality given)" if not (zip_code or (city and state))
                    else "standard CONUS rate (GSA lookup unavailable — set a locality rate manually if needed)")

    con.execute(
        "INSERT INTO travel_trips(destination,city,state,zip,start_date,end_date,mie_cents,"
        "lodging_cents,rate_source,rate_note,purpose) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (destination.strip(), city, state, zip_code, sd, ed, mie, lodging, source, note, purpose.strip()))
    con.commit()
    return safe_redirect("/travel", msg=(
        f"Trip added — M&IE ${ledger.fmt_cents(mie)}/day ({note})."))


@router.get("/travel/{trip_id}", response_class=HTMLResponse)
def travel_detail(request: Request, trip_id: int, msg: str = "", err: str = "", con=Depends(get_con)):
    trip = perdiem.get_trip(con, trip_id)
    if not trip:
        return RedirectResponse("/travel", status_code=303)
    s = perdiem.trip_summary(con, trip)
    return templates.TemplateResponse(request, "travel_detail.html", ctx(
        request, con, trip=trip, summary=s, breakdown=s["breakdown"], actuals=s["actuals"],
        msg=msg, err=err))


@router.post("/travel/{trip_id}/rate")
def travel_set_rate(trip_id: int, mie: str = Form(...), con=Depends(get_con)):
    """Manual per-trip rate override (e.g. GSA was unreachable at creation, or a locality nuance)."""
    trip = perdiem.get_trip(con, trip_id)
    if not trip:
        return RedirectResponse("/travel", status_code=303)
    try:
        cents = abs(ledger.parse_amount_to_cents(mie))
        if cents == 0:
            raise ValueError("rate is zero")
    except ValueError as e:
        return safe_redirect(f"/travel/{trip_id}", err=f"Couldn't read that rate: {e}")
    con.execute("UPDATE travel_trips SET mie_cents=?, rate_source='manual', "
                "rate_note='manually entered rate' WHERE id=?", (cents, trip_id))
    con.commit()
    return safe_redirect(f"/travel/{trip_id}", msg=f"M&IE rate set to ${ledger.fmt_cents(cents)}/day.")


@router.post("/travel/{trip_id}/refresh-rate")
def travel_refresh_rate(trip_id: int, con=Depends(get_con)):
    """Re-run the GSA lookup for a trip (e.g. after adding an API key or fixing the locality)."""
    trip = perdiem.get_trip(con, trip_id)
    if not trip:
        return RedirectResponse("/travel", status_code=303)
    looked = perdiem.fetch_gsa(con, trip["city"], trip["state"], trip["zip"], trip["start_date"])
    if not looked:
        return safe_redirect(f"/travel/{trip_id}", err=(
            "GSA lookup didn't find a rate for this trip's locality — check the city/state or ZIP, "
            "or set the rate manually below."))
    con.execute("UPDATE travel_trips SET mie_cents=?, lodging_cents=?, rate_source='gsa', rate_note=? WHERE id=?",
                (looked["mie_cents"], looked["lodging_cents"], looked["note"], trip_id))
    con.commit()
    return safe_redirect(f"/travel/{trip_id}", msg=(
        f"GSA rate refreshed — M&IE ${ledger.fmt_cents(looked['mie_cents'])}/day ({looked['note']})."))


@router.post("/travel/{trip_id}/delete")
def travel_delete(trip_id: int, con=Depends(get_con)):
    con.execute("DELETE FROM travel_trips WHERE id=?", (trip_id,))
    con.commit()
    return safe_redirect("/travel", msg="Trip deleted.")
