"""Square online payments for invoices (ACH + card) via the Square Invoices API.

Why this shape (it mirrors feeds.py): ShopBooks runs on 127.0.0.1 with no public URL, so it can't
host a payment form and Square can't reach it with webhooks. Instead **Square hosts everything** —
ShopBooks creates + publishes a Square invoice (delivery = SHARE_MANUALLY), emails the customer its
own invoice with the Square `public_url` as a "Pay online" link, and **polls** Square
(`sync_payments`, a button) to learn when it's paid. No new dependency: the REST API is called with
httpx, exactly like feeds/perdiem/ai.

Bookkeeping (cash basis): a paid Square invoice is recorded against the ShopBooks invoice INTO a
"Square" clearing account (income booked gross, sales tax split to Sales Tax Payable), and Square's
processing fee is booked as an expense out of that same clearing account once Square reports it — so
the clearing balance equals the net payout that later lands in the real bank and reconciles there as
a transfer.

Auth: a Square app **access token** (a Personal Access Token works for one's own account) + a
**Location id**, stored as secrets in settings. `square_environment` (sandbox|production) picks the
API base URL. All calls go through `_get/_post/_put`, which tests monkeypatch so nothing hits the
network.
"""
import json
import logging
import uuid
from datetime import date

import db
import invoicing
import ledger

log = logging.getLogger("shopbooks.square")

SQUARE_VERSION = "2025-01-23"        # pinned Square-Version header
CLEARING_ACCOUNT = "Square"          # ShopBooks clearing account online payments land in
SQUARE_FEES_ACCOUNT = "Square Fees"  # expense account the 1% (etc.) processing fee books to


# ---------------------------------------------------------------- configuration
def access_token(con):
    return db.get_setting(con, "square_access_token", "")


def location_id(con):
    return db.get_setting(con, "square_location_id", "")


def environment(con):
    return (db.get_setting(con, "square_environment", "sandbox") or "sandbox").strip().lower()


def configured(con):
    return bool(access_token(con) and location_id(con))


def card_enabled(con):
    return db.get_setting(con, "square_enable_card", "1") == "1"


def _base(con):
    return ("https://connect.squareup.com" if environment(con) == "production"
            else "https://connect.squareupsandbox.com")


# ---------------------------------------------------------------- HTTP layer (mockable in tests)
def _headers(con):
    return {"Authorization": f"Bearer {access_token(con)}",
            "Square-Version": SQUARE_VERSION,
            "Content-Type": "application/json"}


def _api_error(status, text):
    try:
        errs = json.loads(text).get("errors", [])
        msg = "; ".join(e.get("detail") or e.get("code") or "" for e in errs) or text
    except Exception:
        msg = text or f"HTTP {status}"
    return RuntimeError(f"Square API error ({status}): {msg}")


def _request(con, method, path, body=None):
    import httpx
    r = httpx.request(method, _base(con) + path, headers=_headers(con),
                      content=json.dumps(body) if body is not None else None, timeout=30)
    if r.status_code >= 400:
        raise _api_error(r.status_code, r.text)
    return r.json() if r.content else {}


def _get(con, path):
    return _request(con, "GET", path)


def _post(con, path, body):
    return _request(con, "POST", path, body)


def _put(con, path, body):
    return _request(con, "PUT", path, body)


# ---------------------------------------------------------------- accounts (clearing + fee)
def clearing_account_id(con, create=True):
    """The ShopBooks account online payments land in. Uses `square_deposit_account_id` if set;
    otherwise finds/creates a 'Square' clearing account (kind bank / type asset) and remembers it."""
    picked = db.get_setting(con, "square_deposit_account_id", "")
    if picked:
        row = con.execute("SELECT id FROM accounts WHERE id=?", (int(picked),)).fetchone()
        if row:
            return row["id"]
    row = con.execute("SELECT id FROM accounts WHERE name=?", (CLEARING_ACCOUNT,)).fetchone()
    if row:
        db.set_setting(con, "square_deposit_account_id", str(row["id"]))
        return row["id"]
    if not create:
        return None
    aid = con.execute("INSERT INTO accounts(name,kind,type,active) VALUES(?,?,?,1)",
                      (CLEARING_ACCOUNT, "bank", "asset")).lastrowid
    db.set_setting(con, "square_deposit_account_id", str(aid))
    return aid


def _fees_account_id(con):
    row = con.execute("SELECT id FROM accounts WHERE name=?", (SQUARE_FEES_ACCOUNT,)).fetchone()
    if row:
        return row["id"]
    return con.execute("INSERT INTO accounts(name,kind,type,active) VALUES(?,?,?,1)",
                       (SQUARE_FEES_ACCOUNT, "category", "expense")).lastrowid


# ---------------------------------------------------------------- helpers
def _money(m):
    return int(m.get("amount", 0)) if m else 0


def _fmt_qty(q):
    q = float(q)
    return str(int(q)) if q == int(q) else f"{q:g}"


def _split_name(name):
    parts = (name or "").strip().split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return (name or "Customer"), ""


def test_connection(con):
    """Validate the token + environment by listing locations. Raises ValueError when not configured
    or the Location id is wrong; RuntimeError (Square's message) on an API error. Returns a success
    string for the Settings 'Test connection' button."""
    if not access_token(con):
        raise ValueError("Add your Square access token in Settings first.")
    locs = _get(con, "/v2/locations").get("locations", [])
    names = {l["id"]: l.get("name", l["id"]) for l in locs}
    lid = location_id(con)
    if not lid:
        return f"Token works ({environment(con)}). Now set a Location id — available: " + \
            (", ".join(f"{v} ({k})" for k, v in names.items()) or "none") + "."
    if lid not in names:
        raise ValueError(f"Connected, but Location id '{lid}' isn't in this {environment(con)} "
                         f"account. Available: " + (", ".join(names) or "none") + ".")
    return f"Connected to Square ({environment(con)}) — location: {names[lid]}."


# ---------------------------------------------------------------- customers
def ensure_customer(con, customer_id):
    """Square customer id for a ShopBooks customer, cached in `customers.square_customer_id`. Creates
    the Square customer from the name/email/phone the first time."""
    c = con.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not c:
        raise ValueError("Customer not found.")
    if c["square_customer_id"]:
        return c["square_customer_id"]
    given, family = _split_name(c["name"])
    body = {"idempotency_key": str(uuid.uuid4()), "given_name": given, "note": "ShopBooks customer"}
    if family:
        body["family_name"] = family
    if c["email"]:
        body["email_address"] = c["email"]
    if c["phone"]:
        body["phone_number"] = c["phone"]
    sid = _post(con, "/v2/customers", body)["customer"]["id"]
    con.execute("UPDATE customers SET square_customer_id=? WHERE id=?", (sid, customer_id))
    return sid


# ---------------------------------------------------------------- create + publish an invoice
def _order_line_items(con, invoice_id):
    """Each ShopBooks invoice line becomes one Square line item priced at its line total (qty folded
    into the price so fractional quantities stay exact), plus a 'Sales Tax' line when tax applies —
    so the Square total equals the ShopBooks total to the cent."""
    _, items, _ = invoicing.get_invoice(con, invoice_id)
    lines = []
    for it in items:
        total = round(it["qty"] * it["unit_cents"])
        name = (it["description"] or it["item_name"] or "Item")
        if float(it["qty"]) != 1:
            name = f"{name} (x{_fmt_qty(it['qty'])})"
        lines.append({"name": name[:500], "quantity": "1",
                      "base_price_money": {"amount": total, "currency": "USD"}})
    tax = invoicing.invoice_tax(con, invoice_id)
    if tax:
        lines.append({"name": "Sales Tax", "quantity": "1",
                      "base_price_money": {"amount": tax, "currency": "USD"}})
    return lines


def create_and_publish_invoice(con, invoice_id):
    """Create a Square order + invoice for a ShopBooks invoice, enable ACH (+ card if turned on),
    publish it (delivery SHARE_MANUALLY), and store the mapping. Returns {public_url, status}.
    Raises ValueError if not configured or the invoice has no payable balance."""
    if not configured(con):
        raise ValueError("Square isn't connected — add your access token and location in Settings.")
    inv, _, total = invoicing.get_invoice(con, invoice_id)
    if not inv:
        raise ValueError("Invoice not found.")
    if inv["kind"] != "invoice":
        raise ValueError("Only invoices can be sent for online payment.")
    if total <= 0:
        raise ValueError("This invoice has no balance to collect.")
    sq_customer = ensure_customer(con, inv["customer_id"])

    order = _post(con, "/v2/orders", {"idempotency_key": str(uuid.uuid4()), "order": {
        "location_id": location_id(con), "reference_id": inv["number"],
        "line_items": _order_line_items(con, invoice_id)}})["order"]

    created = _post(con, "/v2/invoices", {"idempotency_key": str(uuid.uuid4()), "invoice": {
        "location_id": location_id(con),
        "order_id": order["id"],
        "primary_recipient": {"customer_id": sq_customer},
        "payment_requests": [{"request_type": "BALANCE", "due_date": inv["due_date"],
                              "automatic_payment_source": "NONE"}],
        "accepted_payment_methods": {"bank_account": True, "card": card_enabled(con)},
        "delivery_method": "SHARE_MANUALLY",
        "invoice_number": inv["number"],
        "title": f"Invoice {inv['number']}",
        "sale_or_service_date": inv["date"],
    }})["invoice"]

    published = _post(con, f"/v2/invoices/{created['id']}/publish",
                      {"version": created["version"], "idempotency_key": str(uuid.uuid4())})["invoice"]
    public_url = published.get("public_url", "")
    con.execute(
        "INSERT INTO square_invoices(invoice_id, square_invoice_id, square_order_id, public_url, "
        "status, version) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(invoice_id) DO UPDATE SET square_invoice_id=excluded.square_invoice_id, "
        "square_order_id=excluded.square_order_id, public_url=excluded.public_url, "
        "status=excluded.status, version=excluded.version, updated_at=datetime('now')",
        (invoice_id, published["id"], order["id"], public_url,
         published.get("status", ""), published.get("version", 0)))
    return {"public_url": public_url, "status": published.get("status", "")}


def get_mapping(con, invoice_id):
    return con.execute("SELECT * FROM square_invoices WHERE invoice_id=?", (invoice_id,)).fetchone()


# ---------------------------------------------------------------- poll for payment
def _processing_fee(con, order_id):
    """Sum the processing fee across the order's payments (may be 0 until Square finalizes it — ACH
    fees can lag a day or two, so a later sync books it). Never raises: a lookup failure returns 0."""
    if not order_id:
        return 0
    total = 0
    try:
        order = _get(con, f"/v2/orders/{order_id}").get("order", {})
        for tender in order.get("tenders", []):
            pid = tender.get("payment_id")
            if not pid:
                continue
            pay = _get(con, f"/v2/payments/{pid}").get("payment", {})
            for f in pay.get("processing_fee", []) or []:
                total += _money(f.get("amount_money"))
    except Exception as e:
        log.warning("Square fee lookup failed for order %s: %s", order_id, e)
        return 0
    return total


def _paid_cents(con, sq, invoice_id, status):
    """How much Square has collected on this invoice. Square reports the completed amount inside
    `payment_requests[].total_completed_amount_money` (there's no reliable top-level field), so sum
    those; if the invoice is fully PAID but the amount is absent, fall back to the ShopBooks invoice
    total (a BALANCE request paid in full == the invoice total)."""
    v = _money(sq.get("total_completed_amount_money"))
    if not v:
        v = sum(_money(pr.get("total_completed_amount_money"))
                for pr in (sq.get("payment_requests") or []))
    if not v and status == "PAID":
        _, _, v = invoicing.get_invoice(con, invoice_id)
    return v


def _book_fee(con, invoice_id, number, fee_cents):
    ledger.post_entry(con, date.today().isoformat(), f"Square fee - Invoice {number}",
                      [(_fees_account_id(con), fee_cents), (clearing_account_id(con), -fee_cents)],
                      memo="Square processing fee")


def sync_payments(con):
    """Poll every mapped Square invoice. When one is PAID and not yet booked, record the payment into
    the Square clearing account (income gross, tax split) against the ShopBooks invoice, and book the
    processing fee once Square reports it. Idempotent (tracked by payment_recorded_cents / fee_recorded).
    Returns a summary dict. Raises ValueError if not configured; network errors bubble to the route."""
    if not configured(con):
        raise ValueError("Square isn't connected — add your access token and location in Settings.")
    rows = con.execute("SELECT s.*, i.number FROM square_invoices s "
                       "JOIN invoices i ON i.id=s.invoice_id").fetchall()
    recorded = fees = 0
    seen = []
    for r in rows:
        sq = _get(con, f"/v2/invoices/{r['square_invoice_id']}").get("invoice", {})
        status = sq.get("status", "")
        paid_cents = _paid_cents(con, sq, r["invoice_id"], status)
        con.execute("UPDATE square_invoices SET status=?, version=?, updated_at=datetime('now') "
                    "WHERE invoice_id=?", (status, sq.get("version", r["version"]), r["invoice_id"]))
        # Record the payment only once the Square invoice is fully PAID (partial payments: phase 2).
        if status == "PAID" and paid_cents > r["payment_recorded_cents"]:
            income_id = invoicing.invoice_default_income_id(con, r["invoice_id"])
            invoicing.record_invoice_payment(
                con, r["invoice_id"], into_account_id=clearing_account_id(con), income_id=income_id,
                amount_cents=paid_cents, date=date.today().isoformat(),
                label=f"Square payment - Invoice {r['number']}",
                memo=f"Square invoice {r['square_invoice_id']}")
            con.execute("UPDATE square_invoices SET payment_recorded_cents=? WHERE invoice_id=?",
                        (paid_cents, r["invoice_id"]))
            recorded += 1
        if status == "PAID" and not r["fee_recorded"]:
            fee = _processing_fee(con, r["square_order_id"])
            if fee > 0:
                _book_fee(con, r["invoice_id"], r["number"], fee)
                con.execute("UPDATE square_invoices SET fee_recorded=1 WHERE invoice_id=?",
                            (r["invoice_id"],))
                fees += 1
        seen.append({"number": r["number"], "status": status})
    con.commit()
    return {"recorded": recorded, "fees": fees, "invoices": seen}
