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

`test_safety.py` is the committed canonical test and shows the **mandatory** pattern: set
`SHOPBOOKS_DATA_DIR` to a temp dir **before importing `db`/`app`**, so a test can never touch
real books. Larger flow tests follow the same pattern with `fastapi.testclient.TestClient`,
exercising import → review → post → balances → receipts → reports → invoices → tax zip and
asserting the ledger invariant (below); they may be throwaway (delete after) but must set the
env var first.

> ⚠️ **NEVER run a test or any DB-mutating script without `SHOPBOOKS_DATA_DIR` set to a temp
> dir.** The user's real books live at `%LOCALAPPDATA%\ShopBooks` (see Data location below).
> A test that imports `db`/`app` without the override will read, write, and—if it cleans up
> after itself—**delete the real database.** This exact mistake destroyed the user's data once.
> The harness env var is the guard; `test_safety.py` proves it works. (Even so, `backup.py`
> snapshots every launch to `<datadir>/backups/` and mirrors to `<OneDrive>/ShopBooks Backups/`.)

## Data location

`db.py` resolves the data dir at import time: `SHOPBOOKS_DATA_DIR` if set, else
`%LOCALAPPDATA%\ShopBooks`. **Deliberately outside the repo** so git ops, re-clones, and test
cleanup can't touch real books. `db.init()` runs a guarded one-time migration of any legacy
in-repo `data/` into the stable location (skipped when the override is set, so tests don't pull
the repo's data in). `backup.snapshot()` runs on app startup (in `app.py`). The cloud mirror is
suppressed when `SHOPBOOKS_DATA_DIR` is set, so tests never write to the user's real OneDrive.

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
`DEFAULT_SETTINGS`. New tables and new settings keys are auto-created; **new columns on
existing tables are NOT created by `CREATE TABLE IF NOT EXISTS`** — add a guarded `ALTER TABLE`
to `db._column_migrations(con)` (it checks `PRAGMA table_info` first; `documents.sha256` is the
existing example). Existing user data must always survive an upgrade.

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
- **Windows**: the server holds the books.db open — stop it before moving the data dir.
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
| `backup.py` | Startup snapshots, retention, cloud mirror, full-ZIP download |
| `migrate.py` | QuickBooks Online CSV import (accounts, transactions, customers, mileage, opening balances) |
| `%LOCALAPPDATA%\ShopBooks\` | **User's real books** — books.db + docs/ (receipts) + backups/. NOT in the repo. Never wipe |
| `docs/` | ARCHITECTURE.md (design + rationale), USER_GUIDE.md, ROADMAP.md (changelog + planned work) |

## Process expectations for agents

- This repo lives at https://github.com/outlierworkshop/shopbooks (private). Commit logical
  units of work with clear messages and push when a change is verified. `data/` is gitignored
  because it holds the user's real books and secrets — **never force-add it**.
- Update `docs/ROADMAP.md` (changelog section) when you ship a change.
- Keep this file and ARCHITECTURE.md truthful — if you change a convention, update the doc
  in the same change.
- Match existing style: stdlib + the few deps in requirements.txt, server-rendered pages,
  small modules, no ORM, no build step. Don't introduce frameworks without the owner asking.
- The owner is not a professional developer. UI text avoids accounting jargon
  (debit/credit appear only in docs and the manual-entry helper text).
