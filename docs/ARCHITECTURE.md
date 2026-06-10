# ShopBooks Architecture

Audience: developers and AI agents extending the app. For day-to-day usage see
`USER_GUIDE.md`; for the quick agent checklist see `../CLAUDE.md` (read that first).

## Design goals (why it is the way it is)

1. **Local and durable.** The owner is replacing QuickBooks Online specifically to own his
   data. Everything is plain files: one SQLite DB + a folder of receipt images. No accounts,
   no telemetry, no cloud storage. The app must keep working in ten years with nothing but
   Python installed.
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
├── templates/   Jinja2 pages, all extend base.html
└── static/      style.css (CSS variables at top define the palette)
```

Dependency direction: `app.py` → everything; modules don't import `app.py`;
`ledger.py` imports nothing internal; `ai.py`/`invoicing.py` import `db`;
`importer.py` imports `ledger`.

## Data model

```
accounts(id, name UNIQUE, type, kind, active)
  type ∈ asset | liability | equity | income | expense   (accounting identity)
  kind ∈ bank | card | category                          (UI behavior: bank/card are importable)

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

### Duplicate detection
A transfer appears on BOTH statements (card payment: card stmt shows credit, bank stmt shows
withdrawal). Posting both double-counts. `importer.possible_duplicate` flags a staged row when
a posted split already exists on the same source account for `−amount` within ±4 days.
The user Skips the second copy. (Auto-merge was deliberately not built: too risky.)

## Reports

- **P&L** (`ledger.pnl`): per income/expense account, sum splits joined to entries in the date
  range, display-signed. Cash basis by construction (entries exist only when money moved).
- **Balance sheet** (`ledger.balance_sheet`): asset/liability/equity balances as of date, plus
  a computed "Retained Earnings" line = −Σ(all income+expense splits ≤ date) so the sheet
  balances without closing entries (the app never closes periods).
- **Mileage** is a tax-return deduction, *not* a ledger entry — reported alongside, never posted.
- **Tax package** (`/taxes/package.zip`): P&L, balance sheet, transaction detail (each line
  cross-referenced to its receipt filename), mileage log, all receipt images for the year.

## Invoicing (phase 2)

Cash basis: creating/sending an invoice does **not** touch the ledger. "Record payment" posts
`[(bank, +total), (income, −total)]` and stores `paid_entry_id`. Undo payment (or deleting the
entry from a register) reverts the invoice to `sent` — `ledger.delete_entry` owns that cleanup.
Numbering: `settings.next_invoice_number`, rendered as `INV-{n}`, incremented at creation
(numbers are not reused after deletion — fine for this scale).
PDF: `invoicing.render_pdf` (fpdf2, helvetica, latin-1 — `_latin()` sanitizes).
Email: stdlib `smtplib` STARTTLS + app password; subject/body templates in settings with
`{number} {business} {customer} {total} {due_date} {date}` placeholders.

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

## Receipt matching

On upload: optional AI extraction → `receipt_candidates` finds posted entries with a split
exactly equal to the receipt total on an income/expense account, within ±7 days when the
receipt has a date, that don't already have a document. Exactly one candidate → auto-match;
otherwise the user picks from buttons. Matched receipts show 📎 in registers and their
filenames ride along in the tax-package transaction CSV.

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

## Testing approach

No committed suite yet (see ROADMAP). Pattern used so far: throwaway `TestClient` script
covering the full happy path + the ledger zero-sum invariant, run against a **fresh** `data/`,
then delete the script and `data/`. If real books exist, protect them first
(see CLAUDE.md warning). Highest-value future work: pytest suite with a tmp-dir DB fixture.
