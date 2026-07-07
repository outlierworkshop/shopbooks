# ShopBooks Architecture

Audience: developers and AI agents extending the app. For day-to-day usage see
`USER_GUIDE.md`; for the quick agent checklist see `../CLAUDE.md` (read that first).

## Design goals (why it is the way it is)

1. **Local and durable.** The owner is replacing QuickBooks Online specifically to own his
   data. Everything is plain files: one SQLite DB + a folder of receipt images. No accounts,
   no telemetry, no cloud storage. The app must keep working in ten years with nothing but
   Python installed. **Data lives outside the code** (`%LOCALAPPDATA%\ShopBooks`, overridable
   via `SHOPBOOKS_DATA_DIR`) so the code can be re-cloned, updated, or tested without ever
   risking the books; `backup.py` snapshots on every launch (local + OneDrive mirror). See
   §Data location & backups.
2. **Correct double-entry under a non-accountant UI.** The ledger is real (balanced journal
   entries) but the user never types "debit". Workflow language: money in / money out,
   categories, transfers.
3. **AI as an accelerator, never a dependency.** Claude reads PDFs/receipts and categorizes;
   if there's no API key (or a call fails), every workflow still completes manually.
4. **One person, simple needs.** 1 bank account, 3 credit cards, Square + ACH income,
   mileage, a tax advisor who wants CSVs. Resist features for hypothetical users
   (multi-currency, payroll, inventory, multi-user) unless the owner asks.
5. **Boring tech.** FastAPI + Jinja server-rendered pages, vanilla CSS, almost no JS
   (one `addRow()` on the invoice form). No ORM, no migrations framework, no build step.
   Any agent can read the whole codebase in minutes.

## Module map

```
app.py          routes only (thin); every route opens/closes its own sqlite connection
├── db.py        connect(), SCHEMA, seed data, settings get/set, DEFAULT_SETTINGS
├── ledger.py    the accounting core (no FastAPI imports — pure functions on a connection)
├── importer.py  statement ingestion: CSV/PDF → staged rows; rules; duplicate detection
├── ai.py        Claude API wrappers (statement extraction, receipt vision, categorization)
├── invoicing.py invoices: totals/queries, fpdf2 PDF, SMTP send
├── migrate.py   QuickBooks Online CSV import (accounts, transactions, customers, mileage, opening balances)
├── backup.py    startup snapshots, retention, OneDrive mirror, full-ZIP download
├── templates/   Jinja2 pages, all extend base.html
└── static/      style.css (CSS variables at top define the palette)
```

Dependency direction: `app.py` → everything; modules don't import `app.py`;
`ledger.py` imports nothing internal; `ai.py`/`invoicing.py` import `db`;
`importer.py` imports `ledger`.

## Data model

```
accounts(id, name UNIQUE, parent_id→accounts, type, kind, active)
  type ∈ asset | liability | equity | income | expense   (accounting identity)
  kind ∈ bank | card | category                          (UI behavior: bank/card are importable)
  parent_id NULL = top-level; else a sub-account (TWO levels max; child.type == parent.type)

entries(id, date, payee, memo, created_at)               (journal entry header)
splits(id, entry_id→entries, account_id→accounts, amount_cents)

batches(id, filename, account_id→accounts, imported_at)  (one statement upload)
staged(id, batch_id→batches, date, description, amount_cents,
       category_id→accounts, status, entry_id→entries)
  status ∈ pending | posted | skipped

rules(id, pattern, account_id→accounts)                  (substring → category; longest wins)

documents(id, filename, path, kind, vendor, doc_date, amount_cents,
          status, entry_id→entries, uploaded_at)         (receipts; status unmatched|matched)

mileage(id, date, miles, purpose, from_loc, to_loc)
settings(key, value)                                     (incl. secrets; see CLAUDE.md)

customers(id, name, email, address, phone, notes)
invoices(id, number UNIQUE, customer_id→customers, date, due_date,
         status, memo, paid_date, paid_entry_id→entries, created_at)
  status ∈ draft | sent | paid | void                    ("overdue" is computed, not stored)
invoice_items(id, invoice_id→invoices CASCADE, description, qty REAL, unit_cents)
```

All money is **integer cents**; all dates are **ISO `YYYY-MM-DD` TEXT** (string comparison
== date comparison, which the SQL relies on).

## Signs — the heart of the system

This section is the difference between correct books and garbage. Internalize it before
changing `ledger.py`, `importer.py`, or any posting code.

### Ledger layer
A split's `amount_cents` is **positive = debit, negative = credit**. Every entry's splits
sum to zero (`post_entry` enforces). Account types have a *normal balance*:

| type | increases with | raw balance sign when healthy | display |
|---|---|---|---|
| asset, expense | debit (+) | positive | raw |
| liability, equity, income | credit (−) | negative | **−raw** (`display_balance`) |

### Import layer
`staged.amount_cents`: **positive = money out** of your pocket (purchase, charge, withdrawal,
fee), **negative = money in** (deposit, refund, card payment received). This is chosen to be
intuitive in the Review UI, *not* to match any bank's export convention.

- AI extraction is prompted to emit this convention directly.
- CSV with a single signed Amount column: most banks use negative = money out, so
  `importer.parse_csv` **negates** it. Separate Debit/Credit columns: `abs(debit) − abs(credit)`.
- Banks are inconsistent; the Review screen has per-batch "Flip signs" as the escape hatch.

### Posting formula
Approving a staged row with amount `a` against chosen category C and source account S
(the bank/card the statement belongs to) posts exactly:

```
splits = [(C, +a), (S, −a)]
```

Worked examples (verify any change against all four):
- Card charge $84.37, C=Materials: Materials +8437 (expense up), Card −8437 (liability up). ✓
- Card payment −$500 on the *card* statement, C=Checking: Checking −50000 (asset down),
  Card +50000 (liability down). ✓ A transfer, no income/expense touched.
- Bank deposit −$200 (Square payout) on the *bank* statement, C=Sales-Square:
  Sales −20000 (income up), Checking +20000 (asset up). ✓
- Bank withdrawal $23.10, C=Shipping: Shipping +2310, Checking −2310. ✓

**Splits (multiple categories on one entry).** The formula generalizes: a row of amount `a`
allocated across categories C₁…Cₙ with magnitudes m₁…mₙ (all positive, Σmᵢ = |a|) posts
`[(C₁, sign·m₁), …, (Cₙ, sign·mₙ), (S, −a)]` where `sign = +1` for money-out rows, `−1` for
money-in — so every category leg carries the row's direction and the whole thing still sums to
zero. `app._post_staged(..., splits=[(cat_id, magnitude_cents), …])` books this from Review (the
inline "Split across categories" drawer); the magnitudes must add up to |a| or **nothing posts**
(a mis-typed split can never book a wrong entry). Manual entry (`/entry/new`) uses the same shape:
one source account + a money-in/out direction + N category rows. A split leaves `staged.category_id`
NULL (there's no single category), and skips the single-category-only conveniences — the
transfer post-once check, invoice auto-mark, and "remember as a rule" — none of which are meaningful
for a multi-category row. A register lists a multi-leg entry's counter side as the comma-joined
category names, and each row offers an inline **⇔ Split** editor:
`ledger.rewrite_entry_splits(con, entry_id, anchor_account_id, [(cat_id, magnitude_cents), …],
direction)` deletes the entry's splits and re-inserts the category legs plus a recomputed anchor leg
(`-Σ categories`), so a plain 2-leg entry becomes a split (or a split is re-allocated) in place —
header, receipt, and invoice links untouched. It's anchored to the register account and reuses the
same direction/sign rule as posting. The old single-field inline editor (`update_entry_fields`)
still only touches a 2-leg entry's one category; for `>1` category leg the register shows
"use ⇔ Split to edit" instead of a misleading single dropdown.

### Transfers / credit-card payments (automatic, ±7 days)
A CC payment appears on BOTH statements (bank withdrawal + card payment); posting both
double-counts. Handled automatically:
- `importer.find_pending_partner` pairs the two **pending** sides by shape — money OUT of a bank
  (positive staged amount) with money IN to a card (negative, equal size) within 7 days,
  **direction-enforced** so an unrelated deposit + same-size charge is not mistaken for a transfer.
  `importer.pair_transfers` (run at the end of `stage_transactions`) sets each side's category to
  the other own account, so posting books a transfer (both legs bank/card) rather than an expense.
- `importer.find_posted_transfer` returns the other own-account id when this row's transfer is
  **already booked** from the other statement (matches only entries whose *both* legs are
  bank/card — never a normal expense). The later side is auto-categorized to that account and
  labelled "already recorded" in Review.
- **Post-once guarantee**: `_post_staged` skips a row whose category is an own account when
  `find_posted_transfer` finds the transfer already booked. Because posts within a request share
  one connection (uncommitted rows are visible), `Post all` can have both sides queued and still
  books exactly once, in any import order.
- `importer.possible_duplicate` (±7 days) remains the generic fallback flag for non-transfer
  look-alikes. Auto-merge of arbitrary duplicates was deliberately not built.

## Reports

- **P&L** (`ledger.pnl`): per income/expense account, sum splits joined to entries in the date
  range, display-signed. Cash basis by construction (entries exist only when money moved).
- **Sub-account roll-up** (`ledger._account_tree`): both reports group sub-accounts under their
  top-level parent. Each parent node = `{own (direct postings to the parent), children:[{name,
  amount}], amount (own + Σ children)}`. Because hierarchy is capped at **two levels**, the
  parent total never double-counts: every account is either a top-level or a direct child, so
  the section total = Σ parent totals = Σ all postings. The dropdown helper `app.categories`
  returns the same accounts in tree order with a `label` (`'Parent : Child'`). CSV exports use
  `app._write_account_section` to indent children + a subtotal line.
- **Balance sheet** (`ledger.balance_sheet`): asset/liability/equity balances as of date (same
  roll-up), plus a computed "Retained Earnings" line = −Σ(all income+expense splits ≤ date) so
  the sheet balances without closing entries (the app never closes periods).
- **Mileage** is a tax-return deduction, *not* a ledger entry — reported alongside, never posted.
- **Tax package** (`/taxes/package.zip`): P&L, balance sheet, transaction detail (each line
  cross-referenced to its receipt filename), mileage log, all receipt images for the year.

## Invoicing (phase 2)

Cash basis: creating/sending an invoice does **not** touch the ledger. "Record payment" posts
`[(bank, +total), (income, −total)]` and stores `paid_entry_id` (for a taxed invoice the income leg
is split — see Sales tax). Undo payment (or deleting the entry from a register) reverts the invoice
to `sent` — `ledger.delete_entry` owns that cleanup.
Numbering: `settings.next_invoice_number`, rendered as `INV-{n}`, incremented at creation
(numbers are not reused after deletion — fine for this scale).
PDF: `invoicing.render_pdf` (fpdf2, helvetica, latin-1 — `_latin()` sanitizes).
Email: stdlib `smtplib` STARTTLS + app password; subject/body templates in settings with
`{number} {business} {customer} {total} {due_date} {date}` placeholders.

**Sales tax.** Items and invoice/estimate lines carry a `taxable` flag; a single business-wide
`settings.sales_tax_rate` (percent) applies. `invoice_total` is **tax-inclusive** —
`invoice_subtotal + invoice_tax` (tax = rate × the taxable lines) — so aging, outstanding balances,
and payment reconciliation all account for the tax the customer owes. Collected tax is a **liability,
not income**: `/invoices/{id}/pay` splits the deposit `[(bank, +P), (income, −inc), (Sales Tax
Payable, −tax)]` via `invoicing.tax_allocation` (proportional on partial payments; no-tax invoices
stay a plain 2-leg income posting). Because the payment now has income **and** tax legs,
`invoice_payments_total` / `invoice_payment_entries` count both (income + the Sales Tax Payable leg,
via `_payment_leg_filter`) so tax-inclusive invoices still reconcile. "Sales Tax Payable" is seeded
and ensured on every `db.init` (idempotent, `accounts.name` UNIQUE). **Known limitation:** payments
recorded by *matching an already-booked deposit* (Review/`_match_invoice_to_entry`) don't
retroactively split tax — that deposit was categorized to income at import time.

## AI integration (`ai.py`)

- Key resolution: `settings.anthropic_api_key` first, then `ANTHROPIC_API_KEY` env.
- Model: `settings.ai_model`, default `claude-opus-4-8`.
- All calls use **structured outputs**: `output_config={"format": {"type": "json_schema",
  "schema": ...}}`; every schema object carries `additionalProperties: false`.
- Three capabilities:
  1. `extract_statement(text)` / `extract_statement_pdf(path)` — transactions from statement
     text, or from the PDF itself (base64 `document` block) when text extraction is empty
     (scanned statements).
  2. `extract_receipt(path)` — vendor/date/total from an image (`image` block; jpeg/png/gif/webp)
     or PDF receipt.
  3. `categorize(txns, names)` — batch-categorizes against the account name list; must return
     exactly len(txns) items or it's discarded.
- **Failure contract: return `None`, never raise to a route.** Callers fall back to
  `importer.regex_parse_statement`, keyword rules, or manual fields.
- Categorization precedence on import: rules first (deterministic, free), AI fills the gaps.
- **Statement years are NOT trusted from the model.** Statement lines print only MM/DD; the
  year lives in the header. The schema asks for `statement_end_date`, and `importer.reconcile_years`
  recomputes each transaction's year from month/day + that closing date (most-recent-on-or-before,
  so Dec→Jan rollover is correct), with a hard guardrail that no date is ever in the future. This
  fixed a real bug where the model emitted 2028 for MM/DD-only lines. The regex fallback keeps its
  own year but runs `importer.clamp_future_dates`.

### Pluggable backend (Claude / Ollama / Hybrid)

`ai_backend` setting selects the engine; `_task_backend(con, task)` resolves it per task
(`statement` | `receipt` | `categorize`). **Hybrid** = Ollama for receipts + categorization,
Claude for statements (the accuracy-critical path stays on Claude). Each public function
(`extract_statement`, `extract_statement_pdf`, `extract_receipt`, `categorize`) dispatches to a
`_claude_*` or `_ollama_*` impl; both share the prompt/schema builders. Ollama calls go over
httpx to `{ollama_url}/api/chat` with `format=<json schema>` (structured outputs, needs recent
Ollama) and base64 `images` for receipts. Ollama limits: images only for receipts (PDF receipts
return None), and the statement path uses extracted PDF *text* — scanned statements return None
and fall back to the regex parser. `ollama_status()` backs the Settings "Test" button. Local
models are noticeably weaker at reading receipt totals/dates — the review/match gates catch the
errors, so it's viable but verify a good vision model (e.g. `qwen2.5vl`).

## Receipt matching

On upload: optional AI extraction → `receipt_candidates` finds posted entries with a split
exactly equal to the receipt total on an income/expense account, within ±7 days when the
receipt has a date, that don't already have a document. Exactly one candidate → auto-match;
otherwise the user picks from buttons. Matched receipts show 📎 in registers and their
filenames ride along in the tax-package transaction CSV.

## Data location & backups

The user's books are irreplaceable, so location and backups are first-class concerns.

- **Location** (`db.py`): `SHOPBOOKS_DATA_DIR` env var if set, else `%LOCALAPPDATA%\ShopBooks`.
  Chosen over the original in-repo `data/` after a test-cleanup script deleted the live DB:
  putting data outside the repo means git, re-clone, and test teardown physically cannot reach
  it. AppData (not OneDrive) is the *live* location to avoid OneDrive locking/dehydrating an
  open SQLite file; OneDrive is used for *backups* instead.
- **Migration** (`db._migrate_old_location`): one-time move of a legacy in-repo `data/` into the
  stable dir, including rewriting stored absolute receipt paths. Guarded to skip when
  `SHOPBOOKS_DATA_DIR` is set so tests never pull repo data into their temp dir.
- **Backups** (`backup.py`): `snapshot()` runs at app startup — a consistent copy via SQLite's
  backup API (valid even mid-write) into `<datadir>/backups/` (last `KEEP`=40), mirrored to
  `<OneDrive>/ShopBooks Backups/`. **A fresh/seeded DB is NOT snapshotted** (`looks_fresh`): no
  data, plus snapshotting it would evict good backups via retention — this is the guard against
  an accidental reset destroying the backups too. `zip_bytes()` powers the Settings ZIP download.
  Cloud mirroring is suppressed in test mode (`SHOPBOOKS_DATA_DIR` set).
- **Restore** (`backup.restore`): overwrites the live DB's contents from a chosen backup via the
  SQLite backup API (works with the app running), after a `pre-restore-*` undo copy; basename-only
  (path-traversal guarded). `pre-restore-*` / `SAFE-*` files are never pruned (`_prune` globs
  `books-*.db` only). `reset_suspected()` (live `looks_fresh` AND a data backup exists) drives a
  warning banner in `base.html`; the 💾 Save button (`/backup/now` with a `back` field) snapshots
  from any page. Cheap on normal loads: `looks_fresh(live)` short-circuits before scanning backups.
- **Test isolation is mandatory**: every test sets `SHOPBOOKS_DATA_DIR` to a temp dir before
  importing `db`/`app`. `test_safety.py` is the committed proof and template.

## Decisions log (don't re-litigate without new information)

| Decision | Why |
|---|---|
| SQLite, not plain-text (Beancount) | Owner is not a developer; relational queries power the UI; one-file backup retained |
| Cash basis, no A/R account | Matches how a one-man Schedule C business actually files |
| Custom engine, not GnuCash/Beancount under the hood | The differentiators (AI import, receipt matching, review queue) needed full control; the double-entry core is ~200 lines |
| Review queue between import and ledger | Imports are guesses (parsers + AI); nothing reaches the books without human approval |
| Rules before AI | Deterministic, free, instant; AI only handles the long tail |
| Mileage not posted to ledger | It's a tax deduction, not a cash event; posting it would corrupt the P&L |
| Per-request sqlite connections | Dead simple, correct enough at single-user scale |
| No auth, bind 127.0.0.1 | Single user, single machine. Auth is a prerequisite for ever changing the bind address |
| Live data in AppData, backups in OneDrive | Keeps an open SQLite file off a syncing folder (no lock/dehydrate), while still getting automatic off-machine backup |

## Testing approach

`test_safety.py` is committed and proves data isolation + the backup system. The mandatory
pattern for every test: set `SHOPBOOKS_DATA_DIR` to a temp dir **before importing `db`/`app`**,
so tests run against a throwaway database and can never read, write, or delete real books.
Flow tests use `fastapi.testclient.TestClient` over the full happy path and assert the ledger
zero-sum invariant; they may be throwaway but must set the env var first. Future work: fold the
throwaway flow scripts into a committed pytest suite sharing a tmp-dir fixture.
