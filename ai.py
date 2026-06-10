"""Optional Claude API integration: statement extraction, receipt reading, categorization.

Every function degrades gracefully — if no API key is configured the app falls
back to regex parsing and keyword rules, and receipts get entered by hand.
"""
import base64
import json
import os

import db

MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf",
}


def api_key(con):
    return db.get_setting(con, "anthropic_api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")


def available(con):
    return bool(api_key(con))


def _client(con):
    if not available(con):
        return None
    import anthropic
    return anthropic.Anthropic(api_key=api_key(con))


def _model(con):
    return db.get_setting(con, "ai_model", "claude-opus-4-8")


def _json_response(client, model, content, schema, max_tokens=16000):
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


STATEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "description": {"type": "string"},
                    "amount": {"type": "number", "description": "dollars; positive = money out (charge/withdrawal), negative = money in (payment/deposit/refund)"},
                },
                "required": ["date", "description", "amount"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["transactions"],
    "additionalProperties": False,
}

RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor": {"type": "string"},
        "date": {"type": "string", "description": "YYYY-MM-DD, or empty string if unreadable"},
        "total": {"type": "number", "description": "grand total in dollars; 0 if unreadable"},
    },
    "required": ["vendor", "date", "total"],
    "additionalProperties": False,
}

CATEGORIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "categories": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["categories"],
    "additionalProperties": False,
}


def extract_statement(con, text, account_name):
    """Extract transactions from raw statement text. Returns list of dicts or None on failure."""
    client = _client(con)
    if client is None:
        return None
    prompt = (
        f"This is the text of a bank or credit card statement for the account '{account_name}'. "
        "Extract every individual transaction (skip running balances, summary totals, "
        "interest-rate tables, and payment-due boilerplate). "
        "Sign convention: positive amount = money out (purchase, charge, withdrawal, fee); "
        "negative amount = money in (deposit, payment received, credit, refund). "
        "Dates as YYYY-MM-DD; infer the year from the statement period.\n\n" + text[:150000]
    )
    try:
        data = _json_response(client, _model(con), prompt, STATEMENT_SCHEMA)
        return data.get("transactions", [])
    except Exception:
        return None


def extract_statement_pdf(con, pdf_path, account_name):
    """Send the PDF itself to Claude (handles scanned/image statements)."""
    client = _client(con)
    if client is None:
        return None
    data_b64 = base64.standard_b64encode(open(pdf_path, "rb").read()).decode()
    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data_b64}},
        {"type": "text", "text": (
            f"This is a bank or credit card statement for '{account_name}'. Extract every individual "
            "transaction (skip balances, totals, boilerplate). Positive amount = money out; "
            "negative = money in. Dates as YYYY-MM-DD, inferring the year from the statement period."
        )},
    ]
    try:
        data = _json_response(client, _model(con), content, STATEMENT_SCHEMA)
        return data.get("transactions", [])
    except Exception:
        return None


def extract_receipt(con, path):
    """Read vendor/date/total off a receipt photo or PDF. Returns dict or None."""
    client = _client(con)
    if client is None:
        return None
    ext = os.path.splitext(path)[1].lower()
    mt = MEDIA_TYPES.get(ext)
    if not mt:
        return None
    data_b64 = base64.standard_b64encode(open(path, "rb").read()).decode()
    if mt == "application/pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": mt, "data": data_b64}}
    else:
        block = {"type": "image", "source": {"type": "base64", "media_type": mt, "data": data_b64}}
    content = [block, {"type": "text", "text": "Read this receipt. Give the vendor name, the date, and the grand total paid."}]
    try:
        return _json_response(client, _model(con), content, RECEIPT_SCHEMA, max_tokens=1000)
    except Exception:
        return None


def categorize(con, txns, category_names):
    """txns: list of {'description','amount'} dicts. Returns list of category names (or None)."""
    client = _client(con)
    if client is None or not txns:
        return None
    lines = "\n".join(f"{i+1}. {t['description']}  ({'+' if t['amount'] >= 0 else ''}{t['amount']/100:.2f})"
                      for i, t in enumerate(txns))
    prompt = (
        "Categorize each transaction below for a one-person workshop/fabrication business. "
        "Positive amounts are money out (expenses or transfers); negative are money in (income/refunds). "
        "Choose exactly one category per transaction from this list (use the exact name):\n"
        + "\n".join(f"- {c}" for c in category_names)
        + "\nIf nothing fits, use 'Uncategorized Expense'. "
        "Return the categories array in the same order, one per transaction, "
        f"with exactly {len(txns)} items.\n\nTransactions:\n" + lines
    )
    try:
        data = _json_response(client, _model(con), prompt, CATEGORIZE_SCHEMA)
        cats = data.get("categories", [])
        return cats if len(cats) == len(txns) else None
    except Exception:
        return None
