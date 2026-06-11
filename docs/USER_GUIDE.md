# ShopBooks User Guide

ShopBooks keeps the books for a one-person business, entirely on your own computer.
No subscription, no cloud, no one else's servers. Your complete financial records live in a
stable folder outside the program: `%LOCALAPPDATA%\ShopBooks` (on this machine,
`C:\Users\outli\AppData\Local\ShopBooks`) — the books database, your receipt images, and
automatic backups. The Settings page shows the exact path and a one-click backup download.

## Starting and stopping

- Start: double-click **run.bat**. Your browser opens http://127.0.0.1:8765.
- Stop: close the black console window.
- The app is only reachable from this computer. That's deliberate.

## First-time setup (5 minutes)

1. **Settings** → fill in your business name, address, email, phone (these print on invoices).
2. **Settings** → paste your Claude API key (from console.anthropic.com). This unlocks PDF
   statement reading, receipt photo reading, and smart categorization. Costs are typically
   pennies per statement. Skippable — see "Working without AI" below.
3. **Settings** → SMTP details if you want to email invoices from the app. For Gmail:
   host `smtp.gmail.com`, port `587`, user = your Gmail address, password = an **App Password**
   (myaccount.google.com → Security → 2-Step Verification → App passwords). Your normal
   password will not work.
4. **Accounts** → rename "Credit Card 1/2/3" to your real cards (e.g. "Amex Blue").
5. Check the **mileage rate** in Settings each January (IRS publishes a new one).

## Coming from QuickBooks Online

The **Migrate** page walks you through it. Export four CSVs from QBO and feed them in, in order:

1. **Account List** report → CSV → creates your chart of accounts (your QBO categories carry over).
2. **Transaction Detail by Account** report (date range you want to keep, Cash basis, with the
   Date / Transaction Type / Name / Memo / **Account** / **Split** / Amount columns) → CSV →
   your history lands in Review with QBO's categories already applied. Post it all; Skip the
   duplicate side of transfers (flagged orange).
3. **Customers** export → CSV.
4. **Mileage** export → CSV (if you used QBO's tracker).
5. Type in each account's real balance as of the day before your export starts (step 5 on the page).
6. Verify: dashboard balances match your real bank/card balances, and last year's P&L here matches
   QBO's (cash basis). Then cancel QuickBooks.

Re-running any step is safe - existing records are skipped, and nothing posts without Review.

## Core ideas (60 seconds)

- Money lives in **accounts**: your checking account and cards, plus *category* accounts like
  "Materials & Supplies" (where money goes) and "Sales - Square" (where it comes from).
- Every transaction connects exactly two accounts and is always balanced — that's
  double-entry bookkeeping, handled for you.
- **Nothing reaches your books without your approval.** Imports go to a Review queue first.
- In Review, **positive = money out** (charges, withdrawals), **green negative = money in**
  (deposits, refunds, payments).

## The monthly routine (~15 minutes)

### 1. Import statements
Download each statement from your bank/card website — PDF or CSV both work (CSV is the most
reliable; PDF needs the AI key for good results). **Import** → choose the file → choose which
account it belongs to.

### 2. Review
Each transaction shows up with a suggested category (from your rules, then AI). Fix any wrong
ones, then **Post all categorized**. If you imported before setting a Claude API key (or added
new rules since), click **🤖 AI categorize pending** to have Claude take a fresh pass over every
unapproved transaction — it fills in the suggested categories but posts nothing; you still approve. Tips:
- Check **Remember** before posting and that payee becomes a permanent rule — next month it
  categorizes itself. After a few months almost everything will.
- **Possible duplicate** (orange row) usually means a transfer you already recorded — e.g. a
  card payment you posted from the card statement now appearing in the bank statement.
  **Skip** it.
- Amounts look backwards (deposits shown as charges)? **Flip signs** fixes the whole batch.
- Credit card payment on a card statement: set its category to **Business Checking** — it
  posts as a transfer between accounts, not an expense.
- Square payouts: category **Sales - Square**.

### 3. Receipts
**Receipts** → upload photos (or PDF receipts). The vendor, date, and total are read
automatically and matched against your posted transactions by amount and date. One clear
match = matched automatically; multiple = you pick. No match yet usually means you haven't
imported that statement yet — it'll match later.

**Importing a whole folder:** if you keep receipt photos in a folder, use **Import a whole
folder** on the Receipts page — type the folder path (optionally including subfolders) and it
reads every image/PDF, skips ones already imported, and auto-matches each to its expense
transaction. A big folder can take a minute. Receipts whose transaction isn't in the books yet
stay unmatched; after you import more statements, click **Re-check matches** to match them.

### 4. Mileage
Log trips as they happen (date, miles, purpose). The deduction is computed for you and
included in reports — you don't need to do anything else with it.

## Invoicing

1. **Invoices** → add the customer once (name, email, address).
2. **+ New invoice** → pick customer, add line items (description, qty, unit price).
   Numbering is automatic.
3. From the invoice page:
   - **Open PDF** — print it, save it, or attach it manually.
   - **Email** — sends it with the PDF attached (needs SMTP set up). You can tweak the
     subject/message per send; defaults live in Settings.
   - **Record payment** when the money lands — pick the date and account, done. This is what
     puts the income on your books (income counts when paid, which is how cash-basis taxes work).
4. When that deposit later appears in your imported bank statement, **Skip** it in Review
   (it'll be flagged as a possible duplicate).
5. Mistakes: draft invoices can be deleted; sent ones can be voided; payments can be undone.

## Tax time

**Taxes** → pick the year.

1. Run the **pre-flight checklist**: no pending imports, nothing left in "Uncategorized
   Expense", receipts matched. Fix anything flagged.
2. **Download tax package (ZIP)** and email it to your tax advisor. It contains:
   - Profit & Loss (your Schedule C numbers, by category)
   - Balance sheet
   - Every transaction, each cross-referenced to its receipt image filename
   - Mileage log with the computed deduction
   - All the receipt images

## Fixing mistakes

- Wrong category after posting: open the account's **register** (Dashboard → "view register"),
  delete the entry (✕) — an imported line returns to Review for re-categorization.
- Wrong receipt match: **Receipts** → Unmatch.
- Manual one-off transactions: **+ Entry** (the page has examples for the common cases).

## Working without AI

Everything works, just more manually: use CSV exports instead of PDF statements, rely on
keyword rules (Rules page) for categorization, and type vendor/date/total on receipts
yourself (the matching still works once the numbers are in).

## Backups

ShopBooks backs itself up automatically:

- **Every time you start the app**, it saves a snapshot of your books into the `backups` folder
  (keeping the last 20) and mirrors a copy to an off-machine **extra backup folder**.
- **You choose the extra backup folder** in Settings → Backup folder. Point it at any
  OneDrive/Dropbox/Google Drive folder for cloud safety, or an external drive. Leave it blank
  and the app auto-detects OneDrive. Saving creates the folder and writes a test backup so you
  can confirm it works; the status line below shows where backups are going and how many are there.
- **Settings → Backups** also has a **Download full backup (ZIP)** button (database + every
  receipt image) and a **Back up now** button.

**To restore a backup:** stop the app, go to the data folder (Settings shows the path), open the
`backups` folder, and copy the `books-...db` you want over the top of `books.db` (rename it to
`books.db`). Start the app — that snapshot is now your live books.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Browser says can't connect | The app isn't running — double-click run.bat |
| PDF import found nothing | Add the Claude API key in Settings, or export CSV from the bank instead |
| Imported amounts are backwards | Review → "Flip signs" for that batch |
| Email send fails | Gmail needs an App Password (not your normal password); check user/port 587 |
| "possible duplicate" everywhere | You imported the same statement twice — Skip the copies |
| Books look wrong | Check each account's register against the real bank/card balance; delete and repost the bad entry |
