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

### 2026-06-23 — Real fix for "shortcut launches blank": pin data dir outside AppData
- Root cause finally found. When the desktop shortcut is opened from inside the Claude desktop
  app, the server runs in that app's **MSIX sandbox**, which silently redirects `%LOCALAPPDATA%`
  to a per-package cache (`...\Packages\Claude_*\LocalCache\Local\`). So the default data dir
  (`%LOCALAPPDATA%\ShopBooks`) resolved to a *different, empty* database — the books looked blank
  even though the real data was safe. (Earlier "stale duplicate server" theory was wrong; this is
  AppData virtualization.) Confirmed: a redirected copy existed under the Claude package cache.
- Fix: `run.bat` now sets `SHOPBOOKS_DATA_DIR=%USERPROFILE%\ShopBooks` — a location MSIX never
  redirects — so every launch (sandboxed or not) reads the same database. Existing books were
  consolidated from `%LOCALAPPDATA%\ShopBooks` to `%USERPROFILE%\ShopBooks` (books.db + docs +
  41 backups + sync_state.json). The old AppData copies are left in place as extra backups.
- Caveat: launch via the shortcut/`run.bat` (which sets the pin). Running the raw uvicorn command
  without the env var would fall back to the per-OS default. Consider promoting `%USERPROFILE%\
  ShopBooks` to the Windows default in `db._default_data_dir()` later so all launch paths agree.

### 2026-06-19 — Sync receipt files between machines (docs-sync)
- Cloud sync now mirrors the **`docs/` folder** alongside `_sync.db`, via a `_sync_docs/` subfolder
  in the cloud folder. `export`/`Sync now` pushes local receipts up (additive, even if the DB is
  unchanged); `import`/`Pull` brings the other machine's receipts down — including a **backfill** when
  the DB is already up to date (so previously-imported phantom receipts get their files). Receipts are
  immutable + uniquely named, so it's a safe additive union by filename.
- `_apply_import` now **repoints document paths** to this machine's docs folder (keeping each file's
  basename), fixing imported rows that carried the other machine's absolute paths.
- Pairs with the earlier `/doc` robustness (no 500 on a missing file). `test_sync.py` covers push,
  pull, path-repoint, and backfill. NOTE: deletes aren't propagated (additive only) — an orphan file
  may linger in `_sync_docs`, harmless since the DB row is gone.

### 2026-06-19 — Receipt hover preview + inline viewing
- Clicking a receipt now opens it **inline in a new tab** (image / PDF / Amazon order text) instead
  of downloading — `/doc` sends the right media type with `Content-Disposition: inline`.
- New `static/receipt-preview.js` (loaded globally): hovering any `data-doc="/doc/<id>"` element shows
  a small floating popup — the image, the Amazon receipt text, or a "PDF — click to open" note. Kind
  is sniffed from the response Content-Type, so templates only add the attribute. Applied to the 📎 on
  account registers and the Receipts page thumbnails. Pure progressive enhancement (click still works).

### 2026-06-19 — macOS launcher (`run-mac.command`)
- Repo-committed, double-clickable macOS launcher (the Mac equivalent of `run.bat`). Resolves its own
  folder so it works wherever cloned; builds the venv on first run; frees port 8765; serves on the real
  default data location (no `SHOPBOOKS_DATA_DIR`, so cloud sync/backups are active); opens the browser.
  Forces `arch -arm64` on Apple Silicon (via `hw.optional.arm64`) so native wheels load even if a
  Rosetta terminal would otherwise run x86_64. README + CLAUDE.md run instructions updated.
- (The earlier `~/Applications/ShopBooks.app` Dock launcher is machine-specific and not in the repo.)

### 2026-06-19 — Per-OS data location + auto-migration
- `db._default_data_dir` is now OS-aware: Windows `%LOCALAPPDATA%\ShopBooks` (unchanged — existing
  PC installs untouched), macOS `~/Library/Application Support/ShopBooks`, Linux `$XDG_DATA_HOME`/
  `~/.local/share/ShopBooks`. Replaces the old Windows-style `~/AppData/Local/ShopBooks` fallback
  that Mac/Linux were using.
- `_migrate_from` (generalizes `_migrate_old_location`): on first launch at the new location, moves a
  legacy dir's **books.db + docs + backups + sync_state.json** forward and repoints stored receipt
  paths. Runs for both the in-repo `data/` and the old `~/AppData/Local/ShopBooks`. No-op once moved;
  never runs under `SHOPBOOKS_DATA_DIR`. Verified live: the Mac's 21-entry books moved cleanly with
  sync lineage intact. `test_datamigrate.py` added; `test_safety.py` migration cases still pass.

### 2026-06-19 — Harden two-machine cloud sync
- **Machine-local settings no longer sync.** `backup_dir` (and `sync_enabled`) are preserved across
  an import, so pulling another computer's books can't overwrite this machine's cloud-folder path.
  (Real bug it caused: a Mac that pulled the PC's books inherited a Windows `backup_dir`, then wrote
  backups to a literal `C:\Users\...` folder under the repo and broke its own sync. Fixed + cleaned up.)
- **Stable cross-machine content hash.** `content_hash` neutralizes those machine-local settings, so
  identical books hash the same on every machine — no more spurious version bumps / cloud writes when
  only `backup_dir` differs.
- **"Pull from cloud now" button** (Settings → Sync) + `sync.pull()`: import on demand, no app restart
  needed (closes the gap where enabling sync mid-session never pulled).
- **Cloud-file download awareness.** Imports validate the cloud copy is a real, downloaded SQLite DB
  (`_readable_db`) and wait/retry for it (`_wait_readable`) — Dropbox/iCloud online-only placeholders
  no longer cause a silent no-op. New `cloud_unavailable` status surfaces a clear banner instead.
- Never clobbers local data on a bad/placeholder source. `test_sync.py` extended (pull, local-setting
  preservation, stable hash / no spurious export, cloud_unavailable + no-clobber, `_readable_db`).

### 2026-06-18 — Fix: cross-import transfers no longer go uncategorized (regression)
- Root cause: the "retroactive transfer matching" rework replaced `pair_transfers` (which had two
  jobs) with `rescan_transfers`, but only kept the pending↔pending pass — it dropped the
  **already-posted** pass. So when the card payment was posted first and the bank statement imported
  later (or vice-versa), the later side never matched the booked transfer: it sat uncategorized and,
  if posted, would **double-count the payment** (overstating expenses + wrong bank balance).
- Fix: `rescan_transfers` now runs a second pass calling `find_posted_transfer` for any pending
  bank/card row not paired in pass 1, pointing it at the other own account (so it auto-skips on post).
  Idempotent (won't re-count an already-pointed row). `test_transfers.py` scenario B passes again;
  full suite green.

### 2026-06-18 — Smart categorization: learn from the user's own history (issue #3)
- New deterministic **history layer** in `importer.py`: `payee_key` (normalize a bank descriptor to
  a stable vendor key — strip store #s/ids/dates), `history_map`/`history_category` (vendor → the
  category this business has used most, from posted income/expense legs; excludes Uncategorized and
  transfers). Works offline, no AI needed.
- Auto-categorize order is now **rules → your history → AI** in both the import (`stage_transactions`)
  and the Review "AI review" flows. History only fills if the learned category is still active.
- AI gets the history as **few-shot**: `ai._categorize_prompt` embeds "how THIS business categorized
  similar vendors before," so Claude matches the owner's habits/chart. New `categorize_model` setting
  (blank = use `ai_model`) to run categorization on a cheaper/faster model (e.g. Haiku).
- Stays suggestions only — nothing posts without confirmation in Review. `test_categorize.py`
  (15 checks: normalization, history map, rules>history>AI precedence, prompt few-shot, model setting).
- NOTE: `test_transfers.py` has one pre-existing failure (cross-import transfer auto-match) unrelated
  to this change — present on main before it; flagged for separate follow-up.

### 2026-06-18 — Click-to-sort columns everywhere (`static/sort.js`)
- New dependency-free `static/sort.js` (loaded globally in `base.html`). Two mechanisms:
  - **Tables**: add `class="sortable"` to a `<table>` and every text column header becomes
    clickable (click toggles asc/desc, ▲/▼ indicator). Type is auto-detected per column —
    money (`$1,234.56`, `(45.00)`), plain numbers, ISO dates (`YYYY-MM-DD`), text — and it reads
    `<input>`/`<select>` values so editable rows (accounts, customers) sort by their field. Skips
    `<tfoot>`, `tr.no-sort`, and empty/action headers. Blanks/em-dashes sort last.
  - **Card lists**: a `[data-sortbar="#listId"]` toolbar with `[data-field]` buttons sorts the
    `[data-sortitem]` children of that list by their `data-<field>` attribute. Used for the
    receipts page (Date / Vendor / Amount / Status), which is cards, not a table.
- Applied to: Review (transactions), registers, invoices + customers, accounts, mileage, rules,
  jobs, time (+ by-category/by-job), job detail, reconcile overview + per-account (dups/period/
  history), dashboard recent activity, settings restore list, and the receipts card list.
- Deliberately NOT applied to hierarchical/total tables (Reports P&L/balance-sheet rollups, the
  reconcile key/value summary, invoice line-items with a totals row, entry forms) where reordering
  rows would break meaning. Pure client-side; degrades to server order if JS is off.
- **Persistent**: the active sort is saved in `localStorage` keyed by page path + table/list index
  and re-applied on load, so it survives the full page reload after posting/skipping/saving a
  transaction (which is a POST→redirect→GET). Each page/register remembers its own column + direction.

### 2026-06-18 — Retroactive transfer matching (bank↔bank too) + "Find transfers" button
- `importer.rescan_transfers(con)` pairs internal transfers across ALL pending rows, not just at
  import time. Matches equal-and-opposite amounts between two of the user's own bank/card accounts
  within 7 days, greedy by nearest date (each row used once), and points each side's category at
  the other account so posting books one transfer (second side auto-skips via the post-once guard).
  Now handles **bank↔bank** (and card↔card), not only bank↔card credit-card payments. Idempotent.
- Wired into: a new **↔ Find transfers** button on /review (retroactively scans the queue),
  `importer.stage_transactions` (replaces the old import-time `pair_transfers`), and the QBO
  `/migrate/transactions` import (migrated rows previously never got paired at all).
- Note on one-sided transfers: a payment only matches when BOTH sides are in the queue. If only
  the bank statement is imported (not the card's), categorize the bank payment directly to the
  card account (or add a rule on the payee, e.g. "CAPITAL ONE CRCARDPMT" → that card).
- Verified with an isolated test (bank↔card, bank↔bank, real-expense-untouched, no self-pairing,
  idempotent re-run, post-once skip) and a dry-run on a copy of the real books (2 genuine
  two-sided transfers matched: $11,111.11 bank transfer + $51.00 Chase payment).

### 2026-06-18 — Fix "blank books on launch" (hardened launcher)
- Root cause was NOT data loss — the live `books.db` stayed full the whole time. A stale/leftover
  server bound to port 8765 (e.g. a dev instance, or one started with `SHOPBOOKS_DATA_DIR` pointing
  at a temp dir) was answering with a fresh 28-account seed and an empty dashboard.
- `run.bat` now kills whatever holds port 8765 (`netstat | findstr ":8765 " → taskkill`) BEFORE
  starting, guaranteeing one clean server on the real books every launch. Removed the fragile
  nested-quote "delayed browser open" line that could abort the script.
- Note for future debugging: one `run.bat` launch shows TWO `python.exe` processes — the `.venv`
  python is a launcher stub that re-execs the real interpreter. That's one server, not a duplicate.

### 2026-06-18 — Reconciliation, Phase 1: balance check (issue #4)
- New `reconciliations` table + `reconcile.py`. Per bank/card account, enter a statement's closing
  date + ending balance; compares to the book balance as-of that date (`ledger.raw_balance`,
  display-signed so it reads like the statement) and reports the difference (0 = reconciled).
- Saves a checkpoint per statement; `reconcile.status` powers a /reconcile overview (book balance,
  last reconciled, in-balance/off flag, activity since). When off, the account page lists that
  period's transactions and flags likely duplicates (same amount within a few days) to find the gap.
- All deterministic; nothing posts to the ledger. "Reconcile" nav link; `test_reconcile.py` (13
  checks: balance compare, as-of, card sign, checkpoint status, duplicate detection).
- Phase 2 (later): per-transaction "cleared" checkboxes (QuickBooks-style); optional AI explanation
  of a discrepancy.

### 2026-06-17 — Match invoices to existing deposits (no ledger entries)
- New `invoices.matched_entry_id` (column migration): links an invoice to a deposit already on the
  books **without owning it**. `invoice_deposit_candidates` finds posted income legs == invoice
  total near the invoice date, unlinked. Match sets status=paid + paid_date from the deposit and
  links it — **posts nothing** (distinct from Record Payment, which owns its `paid_entry_id`).
- Routes: `POST /invoices/{id}/match`, `/invoices/{id}/unmatch` (only unlinks, never deletes the
  deposit), `/invoices/match-all` (auto-links unique matches). `ledger.delete_entry` clears the
  link if the deposit is ever deleted. Invoice view shows candidates / matched state; Invoices
  page has a "Match to deposits" button. Covered by `test_invoice_match.py`.

### 2026-06-17 — Import invoices from QuickBooks (records only)
- `migrate.parse_invoices` reads a QBO Invoice List / Transaction List CSV (tolerant headers:
  Date, No./Num, Customer/Name, Due Date, Amount/Total, Open Balance, Status; skips non-invoice
  rows in a mixed list). `migrate.import_invoices` creates customers (reused) + invoice records
  with a single summary line item, status paid (open balance 0 / "Paid") else sent, deduped on
  invoice number. **Records only — never posts to the ledger** (cash basis; income comes from
  deposit imports, so no double-counting). Route `POST /invoices/import-qbo` + Invoices-page upload.
- Covered by `test_invoice_import.py`. Column mapping to be verified against the owner's real export.

### 2026-06-17 — Hide/reactivate accounts; loaded owner's real 2025 chart of accounts
- Imported the owner's full 2025 P&L chart of accounts (14 income, ~67 expense incl. parents+subs)
  with the 2-level hierarchy; flattened the one 3-level COGS branch; disambiguated duplicate names
  ("Rent Utilities", "Materials - Consumables & Fixturing"); reused existing Office Supplies/Utilities.
- New active/inactive toggle: `POST /accounts/active` (refuses to hide accounts with posted splits
  or active sub-accounts, so reports stay correct), `ledger.accounts_with_balances(include_inactive=)`
  + `active`/`has_history` flags, Accounts-page Hide/Reactivate + show-hidden. Pickers already
  filter `active=1`. Hid 16 unused seed categories (3 with history kept). Covered by `test_deactivate.py`.

### 2026-06-17 — Recategorize a transaction from its matched receipt (relates to #3)
- `ledger.entry_category` / `ledger.set_entry_category`: read and re-point the single income/
  expense leg of a simple 2-sided entry to another account **of the same type** (amounts
  unchanged → stays balanced). Refuses transfers/multi-split and cross-type moves.
- Receipts page: matched receipts show their current category with a **manual dropdown**
  (reversible) and a **🤖 Suggest from receipt** button; a page-level **Recategorize matched
  transactions from their receipts** batches it. AI reads the receipt vendor/items (Amazon `.txt`
  has the itemized list) and picks from the **expense chart of accounts** via `ai.categorize`.
- Why: card lines like `AMAZON MKTPL` categorize weakly; the order receipt's items (e.g. an RTX
  5070 Ti → Tools & Small Equipment) give a far better category. Matching still never auto-edits
  a category — recategorize is an explicit click and fully reversible. Covered by `test_recategorize.py`.

### 2026-06-17 — Auto-attach Amazon orders as receipts (issue #12)
- `importer.parse_amazon_orders` parses the Amazon order-history CSV (newer
  `Retail.OrderHistory.*.csv` and older Order Reports), tolerant header detection, groups item
  rows by Order ID and sums to an order total. Deterministic — no AI.
- `app._ingest_amazon_order` writes an itemized `.txt` receipt to `docs/`, inserts a `documents`
  row (vendor=Amazon), dedupes on order id (sha256), and auto-matches via `receipt_candidates`
  (amount+date). New route `POST /receipts/import-amazon` + a Receipts-page upload.
- Caveat documented in UI/guide: Amazon bills per shipment, so order totals are approximate
  matches — user confirms unmatched ones in the Receipts page. Covered by `test_amazon.py`.
- Verified against the owner's real Business order report (75 orders): order-level total
  (`Order Net Total`) is taken ONCE per order — item-subtotal summing would be wrong when an
  order-level promo applies (e.g. $148.62 charge vs $161.52 item sum). cp1252 decode fallback
  for ™/® in titles.

### 2026-06-16 — Job costing, Phase 2: tag transactions to jobs (issue #9)
- New nullable `entries.job_id` (in SCHEMA + a guarded `_column_migrations` ALTER) tags a whole
  transaction to a job. `ledger.post_entry` takes an optional `job_id`; `ledger.set_entry_job`
  tags retroactively; `ledger.register` rows now carry their job.
- Assign a job: on the +Entry page, or inline per-row on any account register (auto-submits).
- `timetracking.job_financials` / `job_transactions` compute income − expenses on tagged
  transactions = **net cash profit per job**; `job_report` adds financials, the tagged-transaction
  list, and **effective $/hour** (net cash ÷ hours logged). Jobs page gains a Net-profit column.
- Owner's own labor is NOT subtracted (cash-basis, one person) — shown alongside as $/hour.
- `test_timetracking.py` extended: job financials, retroactive (un)tagging, untagged txns
  excluded, effective hourly, and the splits-sum-to-zero invariant after posting.

### 2026-06-16 — Time tracking & job costing, Phase 1 (issue #9)
- New `jobs` and `time_entries` tables + `default_hourly_rate` setting. Manual time entry only
  (no timer), logged against optional **jobs** (which can link to a customer) and free-text work
  **categories**, with a billable flag + optional per-entry rate.
- `timetracking.py`: hours/billable-value rollups by job and category, per-job report, jobs
  overview (mirrors `insights.py` style). All money in integer cents.
- Pages: `/time` (period totals, entry log, by-category/by-job breakdowns), `/jobs` and
  `/jobs/{id}` (create jobs, mark done, per-job hours + billable value). "Time" nav link.
- **Not posted to the ledger** (managerial, like mileage) and **not wired into invoices** yet —
  billable hours show a dollar value in reports only. `test_timetracking.py` asserts the math and
  that no ledger entries/splits are ever created.
- Phase 2 (separate issues): tag ledger transactions with a `job_id` for full profit-per-job;
  optional invoice-from-billable-time; optional live timer.

### 2026-06-12 — In-app restore, Save button, reset protection (after another data loss)
- The live DB had reset to fresh again (root cause still unconfirmed; suspected an accidental
  recreate). Backups had the data — recovered. Hardening so it can't bite again:
- `backup.snapshot()` now **skips a fresh/seeded DB** (`looks_fresh`), so an accidental reset
  can never evict the good backups via retention (bumped KEEP 20→40).
- `backup.reset_suspected()` → a red banner on every page when the live DB looks empty but a
  data backup exists, linking to Restore.
- One-click **Restore** in Settings (`backup.restore` overwrites via the SQLite backup API,
  stashing a `pre-restore-*` undo copy first; path-traversal guarded).
- **💾 Save button** fixed bottom-left of every screen → snapshots and returns to the page with
  a "Saved ✓" toast.
- Covered by `test_restore.py`; `test_safety.py` updated for skip-when-fresh.

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
