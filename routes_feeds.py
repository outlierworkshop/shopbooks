"""Bank feed (SimpleFIN) routes."""
from fastapi import APIRouter, Form
from fastapi.responses import RedirectResponse

import ai
import db
import feeds
import importer
from webutil import categories

router = APIRouter()

def _feed_ai_categorize(con, txns):
    """The same categorization recipe the statement-import route uses: only ask the model when rules
    leave something uncategorized, and only when AI is available. Returns names list or None."""
    cats = {a["id"]: a["name"] for a in categories(con, ("expense", "income"))}
    uncategorized = [t for t in txns if importer.apply_rules(con, t["description"]) is None]
    if uncategorized and ai.available(con):
        return ai.categorize(con, [{"description": t["description"], "amount": t["amount_cents"]} for t in txns],
                             list(cats.values()))
    return None

@router.post("/feeds/claim")
def feeds_claim(setup_token: str = Form(...)):
    from urllib.parse import quote
    con = db.connect()
    try:
        try:
            url = feeds.claim_setup_token(setup_token)
        except ValueError as e:
            return RedirectResponse("/settings?err=" + quote(str(e)), status_code=303)
        db.set_setting(con, "simplefin_access_url", url)
        con.commit()
        try:
            n = feeds.refresh_accounts(con)
            con.commit()
            note = (f"Bank feeds connected — the bridge reports {n} account(s). Map each one to its "
                    "ShopBooks account below, then Fetch.")
        except Exception:
            note = "Bank feeds connected. Couldn't list accounts yet — try 'Fetch from bank feeds' in a moment."
        return RedirectResponse("/settings?msg=" + quote(note), status_code=303)
    finally:
        con.close()

@router.post("/feeds/map")
def feeds_map(feed_account_id: str = Form(...), account_id: str = Form(""), enabled: str = Form("")):
    con = db.connect()
    try:
        acct = int(account_id) if account_id.strip() else None
        con.execute("UPDATE feed_accounts SET account_id=?, enabled=? WHERE id=?",
                    (acct, 1 if enabled else 0, feed_account_id))
        con.commit()
        return RedirectResponse("/settings", status_code=303)
    finally:
        con.close()

@router.post("/feeds/fetch")
def feeds_fetch():
    from urllib.parse import quote
    con = db.connect()
    try:
        try:
            r = feeds.fetch(con, categorize=_feed_ai_categorize)
            con.commit()
        except ValueError as e:
            return RedirectResponse("/settings?err=" + quote(str(e)), status_code=303)
        except Exception as e:
            return RedirectResponse("/settings?err=" + quote(
                f"Couldn't reach the bank feed (it may be busy — the bridge refreshes daily): {e}"), status_code=303)
        parts = [f"Fetched {r['staged']} new transaction(s) from the bank feed"]
        if r["accounts"]:
            parts.append(", ".join(f"{a['name']}: {a['new']}" for a in r["accounts"]))
        if r["unmapped"]:
            parts.append(f"unmapped (skipped): {', '.join(r['unmapped'])} — map them in Settings")
        if r["staged"]:
            return RedirectResponse("/review?note=" + quote("; ".join(parts) + "."), status_code=303)
        return RedirectResponse("/settings?msg=" + quote("; ".join(parts) + ". Nothing new to review."), status_code=303)
    finally:
        con.close()

@router.post("/feeds/disconnect")
def feeds_disconnect():
    from urllib.parse import quote
    con = db.connect()
    try:
        db.set_setting(con, "simplefin_access_url", "")
        con.commit()
        return RedirectResponse("/settings?msg=" + quote(
            "Bank feeds disconnected. (Mappings kept; also deactivate the app on bridge.simplefin.org "
            "if you're done with it.)"), status_code=303)
    finally:
        con.close()
