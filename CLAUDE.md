# ShopBooks — agent guide

Local double-entry accounting app for a one-person business (the owner's QuickBooks Online
replacement). Python 3.14 + FastAPI + SQLite + Jinja2, runs at http://127.0.0.1:8765.
**This is a long-lived project maintained by many agents over time. Read this file fully
before changing anything; read `docs/ARCHITECTURE.md` before touching the ledger, importer,
or AI modules.**

## Run / verify

```powershell
# from this directory
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765   # or run.bat
.venv\Scripts\pip.exe install -r requirements.txt                          # rebuild env
```

There is no committed test suite yet. The established verification pattern is: write a
throwaway script using `fastapi.testclient.TestClient`, exercise the full flow
(import → review → post → balances → receipts → reports → invoices → tax zip), assert the
ledger invariant (below), then **delete the script AND `data/`** so the user's books start clean.

> ⚠️ **Tests write to the real database.** `db.DB_PATH` is fixed at `data/books.db`. If
> `data/books.db` contains real user data, BACK IT UP before running any test, or better:
> monkeypatch `db.DB_PATH`/`db.DATA`/`db.DOCS` to a temp dir first. Improving this (a
> `SHOPBOOKS_DATA_DIR` env var) is a welcome change.

## Invariants — do not break these

1. **Splits sum to zero.** Every journal entry's splits sum to exactly 0 (enforced in
   `ledger.post_entry`). After any change, verify:
   `SELECT entry_id, SUM(amount_cents) FROM splits GROUP BY entry_id HAVING SUM(amount_cents) != 0`
   must return no rows.
2. **All money is integer cents.** Never floats. Parse user/bank input with
   `ledger.parse_amount_to_cents`; format with the `money` Jinja filter / `ledger.fmt_cents`.
3. **Sign conventions** (the most common source of bugs — see ARCHITECTURE.md §Signs):
   - Splits: positive = debit. Asset/expense increase positive; liability/equity/income
     increase negative. Display balances via `ledger.display_balance` (flips credit-normal types).
   - Staged imports: `staged.amount_cents` positive = **money out** (charge/withdrawal),
     negative = money in. Single-amount CSV columns are flipped on import (banks use negative=out).
   - Posting a staged txn: `[(category, +a), (source, -a)]`. This one formula handles
     expenses, income, and transfers uniformly.
4. **Dates are ISO `YYYY-MM-DD` TEXT.** Normalize all inputs through `ledger.normalize_date`.
5. **`ledger.delete_entry` must clear every FK that references entries** — currently
   `staged.entry_id`, `documents.entry_id`, `invoices.paid_entry_id`. If you add a table
   referencing `entries(id)`, update `delete_entry` or deletion will raise IntegrityError.
6. **Cash basis.** Invoices touch the ledger ONLY when payment is recorded (bank debit /
   income credit). Do not post A/R entries on invoice creation.
7. **AI is optional everywhere.** Every `ai.py` function returns `None` on missing key or any
   failure; every caller must have a non-AI fallback path (regex parser, rules, manual entry).
8. **Local-only.** Server binds 127.0.0.1, there is no auth. Never bind 0.0.0.0 without
   adding authentication first. Secrets (API key, SMTP password) live in the `settings` table.

## Schema changes

`db.init()` runs `CREATE TABLE IF NOT EXISTS` at every app start and `INSERT OR IGNORE`s
`DEFAULT_SETTINGS`. **New tables and new settings keys are automatically migrated; new
columns on existing tables are NOT** — add a guarded `ALTER TABLE` in `db.init()`
(check `PRAGMA table_info` first). Existing user data must always survive an upgrade.

## Known footguns (cost previous agents real debugging time)

- **Starlette templates**: must call `templates.TemplateResponse(request, "name.html", ctx)` —
  the old `(name, ctx)` signature was removed and fails with a bizarre
  "cannot use 'tuple' as a dict key" error inside Jinja.
- **fpdf2 is latin-1 only** with built-in fonts: pass all strings through `invoicing._latin()`.
- **Settings secrets**: blank input = keep current value, literal `CLEAR` = remove. Both
  `anthropic_api_key` and `smtp_password` use this convention in `settings_save`.
- **Claude structured outputs**: every JSON schema object needs `additionalProperties: false`;
  no `minLength`/`maximum`-style constraints. Model id default `claude-opus-4-8` (settings key
  `ai_model`). Don't send `temperature` — it 400s on Opus 4.7+.
- **Windows**: the server holds `data/books.db` open — stop it before deleting/moving `data/`.
  Kill by port: `Get-NetTCPConnection -LocalPort 8765 -State Listen` → `Stop-Process`.

## Where things live

| File | Role |
|---|---|
| `app.py` | All FastAPI routes (thin; logic lives in modules) |
| `db.py` | Connection, schema, seeds, settings helpers, `DEFAULT_SETTINGS` |
| `ledger.py` | Double-entry core: post/delete entries, balances, registers, P&L, balance sheet |
| `importer.py` | CSV parsing, PDF text extraction, regex statement fallback, rules engine, duplicate detection, staging |
| `ai.py` | Claude API: statement extraction, receipt vision, categorization (all optional) |
| `invoicing.py` | Invoice queries, fpdf2 PDF rendering, SMTP email |
| `templates/`, `static/style.css` | Server-rendered UI (vanilla; no JS framework) |
| `data/` | **User's real books** — books.db + docs/ (receipt images). Never commit, never wipe without explicit user request |
| `docs/` | ARCHITECTURE.md (design + rationale), USER_GUIDE.md, ROADMAP.md (changelog + planned work) |

## Process expectations for agents

- Update `docs/ROADMAP.md` (changelog section) when you ship a change.
- Keep this file and ARCHITECTURE.md truthful — if you change a convention, update the doc
  in the same change.
- Match existing style: stdlib + the few deps in requirements.txt, server-rendered pages,
  small modules, no ORM, no build step. Don't introduce frameworks without the owner asking.
- The owner is not a professional developer. UI text avoids accounting jargon
  (debit/credit appear only in docs and the manual-entry helper text).
