# ShopBooks

Local double-entry accounting for a one-person business. Replaces QuickBooks Online for
basic bookkeeping: import bank/card statements (PDF or CSV), auto-categorize, match receipt
photos, track mileage, and hand your tax advisor clean reports at year end.

Everything runs on your machine. Your books are one SQLite file (`data/books.db`);
receipt images live in `data/docs/`. Back up the `data` folder and you've backed up the business.

## Run it

- **Windows:** double-click **run.bat** (or `.venv\Scripts\python.exe -m uvicorn app:app --port 8765`).
- **macOS:** double-click **run-mac.command** (or `./run-mac.command`). First run builds the
  virtual environment automatically.

The app opens at http://127.0.0.1:8765 and is only reachable from this computer.

## Monthly workflow

1. **Import** - download each statement (PDF or CSV) from your bank/card sites and upload it,
   picking which account it belongs to.
2. **Review** - confirm/fix the suggested category on each transaction, then "Post all categorized".
   Check "Remember" to turn a payee into a permanent rule.
3. **Receipts** - drop photos of receipts; they're read automatically and matched to posted
   transactions by amount + date.
4. **Mileage** - log trips as they happen; the deduction shows on Reports.

At tax time: **Taxes** → pick the year → run the pre-flight checklist (everything reviewed,
nothing uncategorized, receipts matched) → **Download tax package (ZIP)**. It contains the P&L,
balance sheet, full transaction detail (with receipt filenames), mileage log, and every receipt
image - email the whole thing to your advisor.

## Invoicing

1. Add customers on the **Invoices** page.
2. **+ New invoice** - line items with qty × unit price; numbering is automatic (INV-1001, ...).
3. From the invoice page: open/download the **PDF**, **email it** with the PDF attached
   (set up SMTP in Settings - for Gmail use an App Password), and **record payment** when the
   money arrives - that posts the deposit to your books (cash basis: income counts when paid).
4. When that deposit later shows up in your bank statement import, Skip it (it's flagged as a
   possible duplicate).

## Conventions worth knowing

- **Signs**: in Review, positive = money out (charge/withdrawal), negative (green) = money in.
  If a CSV import looks backwards, hit "Flip signs".
- **Credit card payments** are transfers, not expenses: categorize the card-statement line as
  *Business Checking*. When the same payment shows up in the bank statement import, Skip it
  (it will be flagged as a possible duplicate).
- **Square deposits**: categorize as *Sales - Square*. Square's fees can be split out later, or
  record gross sales manually and book the fee to *Bank & Merchant Fees*.
- The mileage deduction is reported but not posted to the books (it's a tax-return item, not cash).

## AI features (optional)

Add a Claude API key in Settings to enable:
- PDF statement parsing (including scanned statements)
- Receipt photo reading (vendor / date / total)
- Smart categorization of anything the keyword rules don't catch

Without a key, CSV import + keyword rules + manual receipt entry all still work.

## Tech

Python + FastAPI + SQLite + Jinja2. AI calls use the Anthropic API (`claude-opus-4-8` by default,
configurable in Settings). No cloud storage, no subscriptions, no telemetry.
Dependencies: `requirements.txt`.

## Documentation

- **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)** — full manual: setup, monthly routine,
  invoicing, tax time, fixing mistakes, troubleshooting.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — for developers/AI agents: data model,
  sign conventions, posting formula, design decisions and their rationale.
- **[docs/ROADMAP.md](docs/ROADMAP.md)** — changelog, planned work, engineering debt, non-goals.
- **[CLAUDE.md](CLAUDE.md)** — entrypoint for AI agents working on this codebase
  (invariants, footguns, process). Claude Code loads it automatically.
