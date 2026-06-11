# ShopBooks Roadmap & Changelog

This file is the project's shared memory across maintainers (human and AI).
**When you ship a change, add a changelog entry.** When you start a roadmap item, note it.
Keep entries short — what changed and why, not how.

## Vision

An "all-around office manager" for a one-person business: bookkeeping (done), invoicing +
email (done), and eventually everything an owner touches at a desk — statements in, clean
books and tax packages out, with AI doing the tedious parts and the human approving.
Guiding constraints live in `ARCHITECTURE.md` §Design goals — local-first, AI-optional,
boring tech, built for exactly one user.

## Changelog

### 2026-06-11 — Sub-accounts (granular chart of accounts)
- `accounts.parent_id` (column migration) enables two-level Category→Subcategory hierarchy;
  sub-accounts inherit the parent's type. Accounts page adds create-sub-account + re-parent
  with validation (same type, parent must be top-level, no 3rd level, unique names).
- Reports roll children under their parent with a subtotal and a "(direct)" line for postings
  made straight to the parent (`ledger._account_tree`); category dropdowns show `Parent : Child`
  labels (`app.categories`); CSV/tax exports indent via `app._write_account_section`.
- Two levels chosen to keep roll-up math un-double-counted; multi-level + per-parent unique
  names left as future work. Covered by `test_subaccounts.py`.

### 2026-06-11 — Automatic credit-card-payment (transfer) matching
- The two sides of a CC payment (bank withdrawal + card payment, equal amount, within 7 days)
  are auto-detected by shape (money-out-of-bank ↔ money-in-to-card, direction-enforced so an
  unrelated deposit + same-size charge isn't mis-paired) and auto-categorized as a transfer.
- `_post_staged` is now transfer-aware (`importer.find_posted_transfer`): a transfer books
  exactly once regardless of import order or "Post all" — the second side auto-skips. Review
  labels rows "transfer to …" / "transfer already recorded". `possible_duplicate` window 4→7.
- New `importer.find_pending_partner` / `find_posted_transfer` / `pair_transfers`; covered by
  `test_transfers.py` (both-pending, cross-import, no false-pairing, zero-sum).

### 2026-06-11 — Fix wrong statement years (deterministic year reconciliation)
- Bug: statement lines show only MM/DD, so the model guessed years and emitted e.g. 2028.
- Schema now extracts `statement_end_date`; `importer.reconcile_years` recomputes each year from
  month/day + the closing date (handles Dec→Jan rollover), ignoring the model's year, and never
  allows a future date. Regex fallback runs `importer.clamp_future_dates`.
- Import → Review note now shows the imported date range to sanity-check at a glance.
- Added a "Discard batch" button in Review (deletes a batch's unposted rows, keeps posted ones)
  so a bad import can be thrown away and redone. Covered by `test_years.py`, `test_discard.py`.

### 2026-06-11 — Local AI via Ollama (pluggable backend)
- `ai_backend` setting: **claude** (default) | **ollama** (fully local) | **hybrid** (local
  receipts + categorize, Claude statements). `ai.py` refactored into `_claude_*`/`_ollama_*`
  impls behind per-task dispatch (`_task_backend`); shared prompts/schemas
- Ollama via httpx `/api/chat` with structured outputs + base64 images; Settings has engine
  dropdown, server URL, model, and a "Test Ollama connection" probe (`ollama_status`)
- httpx is now a runtime dependency. Verified live against a real Ollama (llava:13b): wire
  format + structured output work; weaker models misread totals (caught by review/match gates)
  — recommend `qwen2.5vl` for receipts. Covered by `test_ollama.py` (dispatch, no network)

### 2026-06-11 — Receipt folder import + re-check matches
- "Import a whole folder" on Receipts: scans a folder (optional subfolders) for image/PDF
  receipts, reads each with AI, auto-matches to expense transactions; dedupes on content
  (`documents.sha256`, added via the new `db._column_migrations` guarded-ALTER helper)
- "Re-check matches" button rematches unmatched receipts after more statements are imported
- Refactored single/batch upload through shared `_ingest_receipt()`; covered by
  `test_receiptfolder.py`. Clears engineering-debt item #2 (column migrations)

### 2026-06-11 — AI categorize pending (Review)
- "🤖 AI categorize pending" button on Review re-runs categorization (rules first, Claude for
  the rest) over all pending staged rows; suggestions only, nothing posts. Shows when a key is
  set. `_ai_review_pending()` in app.py; covered by `test_aireview.py` (AI monkeypatched, no network)

### 2026-06-11 — Configurable backup folder
- New `backup_dir` setting: users pick the off-machine backup folder in Settings (any
  OneDrive/Dropbox/external path); blank = auto-detect OneDrive (prior behavior)
- `backup.cloud_dir()` honors the setting; `cloud_source()`/`check_writable()` added;
  `status()` enriched (source, count, writable). Saving validates the folder and writes a
  test backup. Still suppressed in test mode. Covered by `test_backupdir.py`

### 2026-06-11 — Data safety overhaul (after a data-loss incident)
- **Incident:** a test-cleanup script (`Remove-Item data/`) deleted the live database; the user
  lost settings + API key (no transactions had been entered). Root cause: tests ran against the
  real DB and data lived inside the repo folder.
- Moved the live data dir out of the repo to `%LOCALAPPDATA%\ShopBooks` (overridable via
  `SHOPBOOKS_DATA_DIR`); `db.init()` auto-migrates a legacy in-repo `data/` over, fixing receipt paths
- New `backup.py`: consistent startup snapshots (SQLite backup API) to `<datadir>/backups/`
  (last 20) + automatic mirror to `<OneDrive>/ShopBooks Backups/`; one-click full ZIP (DB +
  receipts) and "Back up now" in Settings; Settings shows data path + backup status
- Mandatory test isolation via `SHOPBOOKS_DATA_DIR`; cloud mirror suppressed in test mode;
  `test_safety.py` committed as the canonical proof (clears engineering-debt item #1)

### 2026-06-10 — QuickBooks Online migration
- `/migrate` page + `migrate.py`: imports QBO report CSVs — Account List (chart of accounts,
  QBO type → ShopBooks type/kind), Transaction Detail by Account (history staged into Review
  with categories from the Split column; bank/card sign normalization; other-side rows skipped),
  Customers, Mileage (deduped), plus opening-balance posting against Owner's Equity
- Parser handles QBO grouped-report noise (title rows, totals, sub-account names);
  header rows require ≥2 non-empty cells (bugfix: one-cell title rows false-matched)
- Also this date: green dollar favicon (`make_icon.py`), desktop shortcut, repo published
  to github.com/outlierworkshop/shopbooks (private)

### 2026-06-10 — Phase 2: invoicing, email, tax package
- Customers + invoices (auto-numbered INV-####, line items, draft/sent/paid/void, overdue computed)
- Invoice PDFs (fpdf2) and SMTP emailing with PDF attached (Gmail app-password flow)
- Cash-basis payment recording: posts bank debit / income credit, undoable;
  `ledger.delete_entry` now clears `invoices.paid_entry_id` (bugfix found in testing)
- Taxes page: pre-flight checklist + one-ZIP tax package (P&L, balance sheet, transaction
  detail with receipt filenames, mileage, receipt images)
- Settings expanded: business identity, invoice terms, email templates, SMTP

### 2026-06-10 — Phase 1: core accounting
- Double-entry ledger (entries/splits, zero-sum enforced), seeded chart of accounts for
  1 bank + 3 cards + Square/ACH income + Schedule C expense categories
- Statement import: CSV parser (header sniffing, debit/credit or signed-amount columns),
  PDF via pdfplumber text + Claude extraction, regex fallback; review queue; per-batch sign flip
- Rules engine (substring → category, longest wins, learn-from-approval) + AI categorization
- Receipt upload → Claude vision (vendor/date/total) → amount+date matching, auto-match when unique
- Mileage log with configurable rate; P&L, balance sheet, registers, CSV exports
- Duplicate (transfer) detection; dashboard; settings with local secret storage

## Next up (owner-approved direction, not yet scheduled)

- **Recurring invoices** — monthly/weekly templates that auto-create drafts.
- **Square fee splitting** — a payout deposit is net; optionally split gross sales vs
  "Bank & Merchant Fees" at review time.
- **Inbox folder auto-import** — watch `data/inbox/`; statements dropped there import
  themselves and land in Review.
- **AI monthly close summary** — one-paragraph "here's what happened in your business last
  month" + anomalies (new vendors, unusual amounts), shown on the dashboard or emailed.

## Engineering debt (do these opportunistically)

1. **Test suite**: partially done — `test_safety.py` is committed and the `SHOPBOOKS_DATA_DIR`
   isolation pattern is established. Remaining: fold the throwaway flow scripts
   (import/review/post, invoicing, QBO migrate) into a committed pytest suite with a shared
   tmp-dir fixture.
2. **Entry editing**: today you delete + repost; in-place edit of payee/memo/category would
   be friendlier.
3. **Receipt → new entry**: when a receipt has no statement match (cash purchase), offer
   "create entry from this receipt".
4. **Backup health on dashboard**: surface "last cloud backup N days ago" if it goes stale.
5. **Large receipt folders**: AI reads run synchronously; a big folder blocks the request.
   Consider background processing + progress if it becomes painful.

## Ideas parking lot (unvetted)

Email inbox integration (read statements/receipts from a mailbox) · invoice payment links
(Square checkout) · quarterly estimated-tax calculator · multi-year comparison reports ·
attachment of arbitrary documents to entries (contracts, warranties) · read-only phone view.

## Non-goals (owner has not asked; don't build speculatively)

Multi-user/auth, cloud sync, payroll, inventory, accrual accounting, multi-currency,
plugin systems, rewrites in other stacks.
