"""Optional AI integration: statement extraction, receipt reading, categorization.

Two interchangeable backends, chosen by the `ai_backend` setting:
  - "claude"  : Anthropic API (most accurate; needs an API key; data leaves the machine)
  - "ollama"  : a local model via Ollama (fully private; needs a GPU + a pulled model)
  - "hybrid"  : Ollama for receipts + categorization, Claude for statement parsing
                (keeps the accuracy-critical statement path on Claude)

Every function degrades gracefully — if the chosen backend isn't usable or a call
fails, it returns None and the app falls back to regex parsing / keyword rules /
manual entry. Receipt reading needs a vision model; Ollama's statement path uses
extracted PDF text (scanned statements fall back to the regex parser).
"""
import base64
import json
import os

import db
import importer

MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


# ---------------------------------------------------------------- backend select

def backend(con):
    return db.get_setting(con, "ai_backend", "claude")


def _task_backend(con, task):
    """task in {'statement','receipt','categorize'}. Resolves 'hybrid' per task."""
    b = backend(con)
    if b == "hybrid":
        return "claude" if task == "statement" else "ollama"
    return b


def api_key(con):
    return db.get_setting(con, "anthropic_api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")


def ollama_url(con):
    return db.get_setting(con, "ollama_url", "http://localhost:11434").rstrip("/")


def ollama_model(con):
    return db.get_setting(con, "ollama_model", "llama3.2-vision")


def _claude_ok(con):
    return bool(api_key(con))


def _ollama_ok(con):
    return bool(ollama_model(con))


def available(con):
    """True if the configured backend has what it needs (controls AI button visibility)."""
    b = backend(con)
    if b == "claude":
        return _claude_ok(con)
    if b == "ollama":
        return _ollama_ok(con)
    return _claude_ok(con) or _ollama_ok(con)  # hybrid


# ---------------------------------------------------------------- prompts/schemas

STATEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "statement_end_date": {"type": "string", "description": "the statement closing / period-end date as YYYY-MM-DD, read from the statement header; empty string if not shown"},
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
    "required": ["statement_end_date", "transactions"],
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
    "properties": {"categories": {"type": "array", "items": {"type": "string"}}},
    "required": ["categories"],
    "additionalProperties": False,
}

RECEIPT_PROMPT = ("Read this receipt. Return the vendor name, the date as YYYY-MM-DD, and the grand "
                  "total actually paid as a number (the final total, not the subtotal or tax line).")


_STATEMENT_RULES = (
    "Extract every individual transaction (skip running balances, summary totals, "
    "interest-rate tables, and payment-due boilerplate). "
    "Sign convention: positive amount = money out (purchase, charge, withdrawal, fee); "
    "negative amount = money in (deposit, payment received, credit, refund). "
    "IMPORTANT ABOUT YEARS: transaction lines usually show only month and day (MM/DD); the full "
    "year appears only in the statement header (the statement/closing/period date). First read "
    "that closing date into statement_end_date (YYYY-MM-DD). For each transaction use the month "
    "and day from its line; do not invent a year that isn't supported by the statement. Output "
    "each transaction date as YYYY-MM-DD."
)


def _statement_prompt(text, account_name):
    return (
        f"This is the text of a bank or credit card statement for the account '{account_name}'. "
        + _STATEMENT_RULES + "\n\n" + text[:150000]
    )


def _categorize_prompt(txns, category_names, examples=None):
    lines = "\n".join(f"{i+1}. {t['description']}  ({'+' if t['amount'] >= 0 else ''}{t['amount']/100:.2f})"
                      for i, t in enumerate(txns))
    ex = ""
    if examples:
        ex = ("\n\nHow THIS business has categorized similar vendors before — match these habits "
              "closely when a transaction looks like one of them:\n"
              + "\n".join(f"- {k} -> {c}" for k, c in examples))
    return (
        "Categorize each transaction below for a one-person workshop/fabrication business. "
        "Positive amounts are money out (expenses or transfers); negative are money in (income/refunds). "
        "Choose exactly one category per transaction from this list (use the exact name):\n"
        + "\n".join(f"- {c}" for c in category_names)
        + "\nIf nothing fits, use 'Uncategorized Expense'."
        + " Money moving between the owner's own accounts is NOT an expense or income: if a line "
        "looks like a credit-card payment (e.g. 'AUTOPAY', 'CRCARDPMT', 'CREDIT CRD', 'EPAYMENT', "
        "'CARD PAYMENT', 'THANK YOU') or an account-to-account transfer (e.g. 'TRANSFER'), return "
        "'Uncategorized Expense' for it - the app links transfers separately, they are not expenses."
        + ex
        + "\n\nReturn the categories array in the same order, one per transaction, "
        f"with exactly {len(txns)} items.\n\nTransactions:\n" + lines
    )


def _history_examples(con, limit=40):
    """The user's vendor->category history, as (vendor_key, category_name) pairs for few-shot."""
    try:
        hist = importer.history_map(con)
        names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM accounts").fetchall()}
        return [(k, names[a]) for k, a in hist.items() if a in names][:limit]
    except Exception:
        return []


# ---------------------------------------------------------------- Claude backend

def _claude_client(con):
    import anthropic
    return anthropic.Anthropic(api_key=api_key(con))


def _claude_model(con, task=None):
    """The model for a task. Categorization can use a cheaper/faster model (e.g. Haiku) via
    the `categorize_model` setting; blank falls back to the main `ai_model`."""
    if task == "categorize":
        m = db.get_setting(con, "categorize_model", "").strip()
        if m:
            return m
    return db.get_setting(con, "ai_model", "claude-opus-4-8")


def _claude_json(con, content, schema, max_tokens=16000, model=None):
    resp = _claude_client(con).messages.create(
        model=model or _claude_model(con),
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def _claude_statement(con, text, account_name):
    try:
        data = _claude_json(con, _statement_prompt(text, account_name), STATEMENT_SCHEMA)
        return importer.reconcile_years(data.get("transactions", []), data.get("statement_end_date", ""))
    except Exception:
        return None


def _claude_statement_pdf(con, pdf_path, account_name):
    data_b64 = base64.standard_b64encode(open(pdf_path, "rb").read()).decode()
    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data_b64}},
        {"type": "text", "text": f"This is a bank or credit card statement for '{account_name}'. " + _STATEMENT_RULES},
    ]
    try:
        data = _claude_json(con, content, STATEMENT_SCHEMA)
        return importer.reconcile_years(data.get("transactions", []), data.get("statement_end_date", ""))
    except Exception:
        return None


def _claude_receipt(con, path):
    ext = os.path.splitext(path)[1].lower()
    mt = MEDIA_TYPES.get(ext)
    if not mt:
        return None
    data_b64 = base64.standard_b64encode(open(path, "rb").read()).decode()
    if mt == "application/pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": mt, "data": data_b64}}
    else:
        block = {"type": "image", "source": {"type": "base64", "media_type": mt, "data": data_b64}}
    try:
        return _claude_json(con, [block, {"type": "text", "text": RECEIPT_PROMPT}], RECEIPT_SCHEMA, max_tokens=1000)
    except Exception:
        return None


def _claude_categorize(con, txns, names, examples=None):
    try:
        cats = _claude_json(con, _categorize_prompt(txns, names, examples), CATEGORIZE_SCHEMA,
                            model=_claude_model(con, "categorize")).get("categories", [])
        return cats if len(cats) == len(txns) else None
    except Exception:
        return None


# ---------------------------------------------------------------- Ollama backend

def _ollama_chat_json(con, prompt, schema, image_bytes=None, timeout=240):
    """One structured-output chat call to a local Ollama server. Raises on failure."""
    import httpx
    msg = {"role": "user", "content": prompt}
    if image_bytes is not None:
        msg["images"] = [base64.b64encode(image_bytes).decode()]
    payload = {"model": ollama_model(con), "stream": False, "format": schema,
               "options": {"temperature": 0}, "messages": [msg]}
    r = httpx.post(ollama_url(con) + "/api/chat", json=payload, timeout=timeout)
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"])


def ollama_status(con):
    """Probe the Ollama server for the Settings 'Test' button."""
    import httpx
    want = ollama_model(con)
    try:
        r = httpx.get(ollama_url(con) + "/api/tags", timeout=5)
        r.raise_for_status()
        models = [m.get("name", "") for m in r.json().get("models", [])]
        base = want.split(":")[0]
        present = any(m == want or m.split(":")[0] == base for m in models)
        return {"reachable": True, "models": models, "model": want, "model_present": present}
    except Exception as e:
        return {"reachable": False, "models": [], "model": want, "model_present": False, "error": str(e)}


def _ollama_statement(con, text, account_name):
    if not text.strip():
        return None
    try:
        data = _ollama_chat_json(con, _statement_prompt(text, account_name), STATEMENT_SCHEMA)
        return importer.reconcile_years(data.get("transactions", []), data.get("statement_end_date", ""))
    except Exception:
        return None


def _ollama_statement_pdf(con, pdf_path, account_name):
    # Local path can't read scanned PDFs (no text); let the caller fall back to the regex parser.
    return None


def _ollama_receipt(con, path):
    if os.path.splitext(path)[1].lower() not in IMAGE_EXTS:
        return None  # local vision path takes images, not PDFs
    try:
        return _ollama_chat_json(con, RECEIPT_PROMPT, RECEIPT_SCHEMA, image_bytes=open(path, "rb").read())
    except Exception:
        return None


def _ollama_categorize(con, txns, names, examples=None):
    try:
        data = _ollama_chat_json(con, _categorize_prompt(txns, names, examples), CATEGORIZE_SCHEMA)
        cats = data.get("categories", [])
        return cats if len(cats) == len(txns) else None
    except Exception:
        return None


# ---------------------------------------------------------------- public dispatch

def extract_statement(con, text, account_name):
    if not available(con):
        return None
    if _task_backend(con, "statement") == "ollama":
        return _ollama_statement(con, text, account_name)
    return _claude_statement(con, text, account_name)


def extract_statement_pdf(con, pdf_path, account_name):
    if not available(con):
        return None
    if _task_backend(con, "statement") == "ollama":
        return _ollama_statement_pdf(con, pdf_path, account_name)
    return _claude_statement_pdf(con, pdf_path, account_name)


def extract_receipt(con, path):
    if not available(con):
        return None
    if _task_backend(con, "receipt") == "ollama":
        return _ollama_receipt(con, path)
    return _claude_receipt(con, path)


def categorize(con, txns, category_names):
    if not txns or not available(con):
        return None
    examples = _history_examples(con)  # few-shot from the user's own categorization history
    if _task_backend(con, "categorize") == "ollama":
        return _ollama_categorize(con, txns, category_names, examples)
    return _claude_categorize(con, txns, category_names, examples)


def analyze(con, facts):
    """Plain-English readout of the deterministic business figures. `facts` is a text block of
    exact numbers (computed by insights.py). Returns prose, or None if AI is unavailable or fails
    — the Insights page always shows the numbers regardless (AI is optional everywhere)."""
    if not _claude_ok(con):
        return None
    prompt = (
        "You are a concise bookkeeping analyst for a one-person workshop/fabrication business. "
        "Using ONLY the exact figures below (do not invent numbers), write a short plain-English "
        "readout for the owner: what's growing or shrinking, the notable expense movers, "
        "profitability, and anything worth watching. 4-7 sentences or tight bullet points. "
        "Be specific with the dollar figures given.\n\n" + facts
    )
    try:
        resp = _claude_client(con).messages.create(
            model=_claude_model(con), max_tokens=900,
            messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in resp.content if b.type == "text").strip() or None
    except Exception:
        return None


RECONCILE_METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "statement_end_date": {"type": "string", "description": "the statement closing / period-end date as YYYY-MM-DD, read from the statement header; empty string if not shown"},
        "ending_balance": {"type": "number", "description": "the statement ending balance, closing balance, or new balance as a number in dollars; 0.0 if not shown or not found"}
    },
    "required": ["statement_end_date", "ending_balance"],
    "additionalProperties": False,
}


def extract_reconcile_metadata(con, text, account_name):
    if not available(con):
        return None
    prompt = (
        f"This is the text of a bank or credit card statement for the account '{account_name}'. "
        "Extract the statement closing / period-end date as YYYY-MM-DD, and the ending balance, closing balance, "
        "or new balance as a number in dollars."
    )
    try:
        if _task_backend(con, "statement") == "ollama":
            return _ollama_chat_json(con, prompt + "\n\n" + text[:150000], RECONCILE_METADATA_SCHEMA)
        return _claude_json(con, prompt + "\n\n" + text[:150000], RECONCILE_METADATA_SCHEMA)
    except Exception:
        return None


def extract_reconcile_metadata_pdf(con, pdf_path, account_name):
    if not available(con):
        return None
    try:
        data_b64 = base64.standard_b64encode(open(pdf_path, "rb").read()).decode()
        content = [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data_b64}},
            {"type": "text", "text": f"This is a bank or credit card statement for '{account_name}'. "
                                    "Extract the statement closing / period-end date as YYYY-MM-DD, and the ending balance, "
                                    "closing balance, or new balance as a number in dollars."},
        ]
        if _task_backend(con, "statement") == "ollama":
            text = importer.pdf_text(pdf_path)
            return extract_reconcile_metadata(con, text, account_name)
        return _claude_json(con, content, RECONCILE_METADATA_SCHEMA)
    except Exception:
        return None
