"""AI backend dispatch test (Claude/Ollama/Hybrid). Isolated; no network (calls monkeypatched)."""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_ollama_"))
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)
os.environ.pop("ANTHROPIC_API_KEY", None)  # don't let a real env key affect availability

import ai  # noqa: E402
import db  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
db.init()
con = db.connect()


def set_backend(b, **kw):
    db.set_setting(con, "ai_backend", b)
    for k, v in kw.items():
        db.set_setting(con, k, v)
    con.commit()


# record which backend each call routes to
calls = []
ai._ollama_chat_json = lambda con, prompt, schema, image_bytes=None, timeout=240: (
    calls.append(("ollama", "image" if image_bytes else "text")) or _fake(schema))
ai._claude_json = lambda con, content, schema, max_tokens=16000: (
    calls.append(("claude", "msg")) or _fake(schema))


def _fake(schema):
    props = schema.get("properties", {})
    if "transactions" in props:
        return {"transactions": [{"date": "2026-01-01", "description": "X", "amount": 1.0}]}
    if "vendor" in props:
        return {"vendor": "V", "date": "2026-01-01", "total": 9.0}
    return {"categories": ["Office Supplies"]}


# a real image file so the ollama receipt path doesn't reject on extension
img = TMP / "r.jpg"
img.write_bytes(b"img")

# ---- availability per backend ----
set_backend("claude")
ok(ai.available(con) is False, "claude backend: unavailable with no key")
set_backend("claude", anthropic_api_key="sk-test")
ok(ai.available(con) is True, "claude backend: available with key")
set_backend("ollama", anthropic_api_key="", ollama_model="llama3.2-vision")
ok(ai.available(con) is True, "ollama backend: available with a model set")
set_backend("ollama", ollama_model="")
ok(ai.available(con) is False, "ollama backend: unavailable with no model")

# ---- routing: full ollama ----
set_backend("ollama", ollama_model="llama3.2-vision", anthropic_api_key="")
calls.clear()
ai.extract_receipt(con, str(img)); ai.categorize(con, [{"description": "a", "amount": 100}], ["Office Supplies"])
ai.extract_statement(con, "some statement text", "Card")
ok(all(c[0] == "ollama" for c in calls) and len(calls) == 3, "ollama backend: all three tasks route to ollama")
ok(("ollama", "image") in calls, "ollama receipt sends an image")

# ---- routing: hybrid (receipts+categorize local, statements Claude) ----
set_backend("hybrid", ollama_model="llama3.2-vision", anthropic_api_key="sk-test")
calls.clear()
ai.extract_receipt(con, str(img))
ai.categorize(con, [{"description": "a", "amount": 100}], ["Office Supplies"])
ai.extract_statement(con, "text", "Card")
routes = dict()
for who, _ in calls:
    routes[who] = routes.get(who, 0) + 1
ok(calls[0][0] == "ollama" and calls[1][0] == "ollama", "hybrid: receipt + categorize -> ollama")
ok(calls[2][0] == "claude", "hybrid: statement -> claude")

# ---- routing: claude ----
set_backend("claude", anthropic_api_key="sk-test")
calls.clear()
ai.extract_receipt(con, str(img)); ai.extract_statement(con, "t", "Card")
ok(all(c[0] == "claude" for c in calls), "claude backend: tasks route to claude")

# ---- ollama receipt rejects a PDF (local vision = images only) ----
set_backend("ollama", ollama_model="llama3.2-vision", anthropic_api_key="")
pdf = TMP / "r.pdf"; pdf.write_bytes(b"%PDF-1.4")
calls.clear()
res = ai.extract_receipt(con, str(pdf))
ok(res is None and not calls, "ollama receipt: PDF rejected before any call")

# ---- _task_backend resolution ----
set_backend("hybrid")
ok(ai._task_backend(con, "statement") == "claude" and ai._task_backend(con, "receipt") == "ollama",
   "hybrid task routing is correct")

con.close()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nOLLAMA-BACKEND TESTS DONE")
