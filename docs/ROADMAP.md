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

1. **Test suite** (highest value): pytest + tmp-dir DB fixture (`SHOPBOOKS_DATA_DIR` env var
   or monkeypatched `db.DB_PATH`), porting the happy-path coverage described in
   ARCHITECTURE.md §Testing. Right now tests can clobber real books.
2. **Column migrations**: `db.init()` only auto-creates tables; add a tiny guarded
   `ALTER TABLE` helper before the first schema change to an existing table.
3. **Backup nudge**: one-click "back up data folder to ZIP" + a dashboard reminder when the
   last backup is old.
4. **Entry editing**: today you delete + repost; in-place edit of payee/memo/category would
   be friendlier.
5. **Receipt → new entry**: when a receipt has no statement match (cash purchase), offer
   "create entry from this receipt".

## Ideas parking lot (unvetted)

Email inbox integration (read statements/receipts from a mailbox) · invoice payment links
(Square checkout) · quarterly estimated-tax calculator · multi-year comparison reports ·
attachment of arbitrary documents to entries (contracts, warranties) · read-only phone view.

## Non-goals (owner has not asked; don't build speculatively)

Multi-user/auth, cloud sync, payroll, inventory, accrual accounting, multi-currency,
plugin systems, rewrites in other stacks.
