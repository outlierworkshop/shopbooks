"""Bank feed (SimpleFIN) routes."""
from fastapi import APIRouter, Depends, Form

import ai
import db
import feeds
import importer
from webutil import categories, get_con, safe_redirect

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
def feeds_claim(setup_token: str = Form(...), con=Depends(get_con)):
    try:
        url = feeds.claim_setup_token(setup_token)
    except ValueError as e:
        return safe_redirect("/settings", err=str(e))
    db.set_setting(con, "simplefin_access_url", url)
    con.commit()
    try:
        n = feeds.refresh_accounts(con)
        con.commit()
        note = (f"Bank feeds connected — the bridge reports {n} account(s). Map each one to its "
                "ShopBooks account below, then Fetch.")
    except Exception:
        note = "Bank feeds connected. Couldn't list accounts yet — try 'Fetch from bank feeds' in a moment."
    return safe_redirect("/settings", msg=note)

@router.post("/feeds/map")
def feeds_map(feed_account_id: str = Form(...), account_id: str = Form(""), enabled: str = Form(""),
             con=Depends(get_con)):
    acct = int(account_id) if account_id.strip() else None
    con.execute("UPDATE feed_accounts SET account_id=?, enabled=? WHERE id=?",
                (acct, 1 if enabled else 0, feed_account_id))
    con.commit()
    return safe_redirect("/settings")

@router.post("/feeds/fetch")
def feeds_fetch(con=Depends(get_con)):
    try:
        r = feeds.fetch(con, categorize=_feed_ai_categorize)
        con.commit()
    except ValueError as e:
        return safe_redirect("/settings", err=str(e))
    except Exception as e:
        return safe_redirect("/settings", err=
            f"Couldn't reach the bank feed (it may be busy — the bridge refreshes daily): {e}")
    parts = [f"Fetched {r['staged']} new transaction(s) from the bank feed"]
    if r["accounts"]:
        parts.append(", ".join(f"{a['name']}: {a['new']}" for a in r["accounts"]))
    if r["unmapped"]:
        parts.append(f"unmapped (skipped): {', '.join(r['unmapped'])} — map them in Settings")
    if r["staged"]:
        return safe_redirect("/review", msg="; ".join(parts) + ".")
    return safe_redirect("/settings", msg="; ".join(parts) + ". Nothing new to review.")

@router.post("/feeds/disconnect")
def feeds_disconnect(con=Depends(get_con)):
    db.set_setting(con, "simplefin_access_url", "")
    con.commit()
    return safe_redirect("/settings", msg=
        "Bank feeds disconnected. (Mappings kept; also deactivate the app on bridge.simplefin.org "
        "if you're done with it.)")
