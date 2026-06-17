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
2. **Settings → AI** → choose your AI engine (unlocks PDF statement reading, receipt photo
   reading, and smart categorization):
   - **Claude (cloud):** paste an API key from console.anthropic.com. Most accurate; pennies
     per statement; data is sent to Anthropic.
   - **Ollama (local):** runs entirely on your machine, nothing leaves it. Install Ollama from
     ollama.com, pull a vision model (`ollama pull qwen2.5vl` is best for receipts), put the
     model name in Settings, and click **Test Ollama connection**. Needs a GPU for speed.
   - **Hybrid:** local model for receipts + categorization, Claude for the harder statement
     parsing. Best of both if you have a Claude key and a GPU.
   - Skippable entirely — see "Working without AI" below.
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
- Credit card payments are matched automatically: the two sides (bank withdrawal + card payment,
  within 7 days) are detected, categorized as a transfer, and booked only **once** — so "Post all"
  is safe even with both statements queued. A row marked "transfer already recorded" is the second
  side and is skipped for you. You only step in if a payment's other side hasn't been imported yet
  (then set its category to the other account, e.g. the card or Business Checking).
- Square payouts: category **Sales - Square**.

### 3. Receipts
**Receipts** → upload photos (or PDF receipts). The vendor, date, and total are read
automatically and matched against your posted transactions by amount and date. One clear
match = matched automatically; multiple = you pick. No match yet usually means you haven't
imported that statement yet — it'll match later.

**Better categories from receipts:** a card line like `AMAZON MKTPL` doesn't say *what* you
bought, so it gets a rough category. Once a receipt is **matched** to its transaction, the
Receipts page shows that transaction's category with a dropdown (change it anytime) and a
**🤖 Suggest from receipt** button — Claude reads the receipt's items (Amazon orders carry the
full item list) and picks a better category from your chart of accounts. There's also a page-level
**Recategorize matched transactions from their receipts** to do them all at once. It only ever
changes the category (never the amount), it's an explicit click (never automatic), and you can
re-pick from the dropdown if you disagree.

**Amazon orders:** since Amazon is a big share of purchases, you don't need photos — use
**Import Amazon orders (CSV)** on the Receipts page. Get the file from Amazon → Account →
**Request My Data** → "Your Orders" (emailed as `Retail.OrderHistory.*.csv`). It builds an
itemized receipt per order and auto-matches to your card charges by amount + date. Note: Amazon
bills per *shipment*, so an order total won't always equal one charge — unmatched orders wait on
the Receipts list for you to match by hand.

**Importing a whole folder:** if you keep receipt photos in a folder, use **Import a whole
folder** on the Receipts page — type the folder path (optionally including subfolders) and it
reads every image/PDF, skips ones already imported, and auto-matches each to its expense
transaction. A big folder can take a minute. Receipts whose transaction isn't in the books yet
stay unmatched; after you import more statements, click **Re-check matches** to match them.

### 4. Mileage
Log trips as they happen (date, miles, purpose). The deduction is computed for you and
included in reports — you don't need to do anything else with it.

## Invoicing

**Importing invoices from QuickBooks:** export Reports → **Invoice List** to CSV, then use
**Import from QuickBooks (CSV)** on the Invoices page. It brings in each invoice as a record
(customer, number, date, amount, paid/open) for tracking — it does **not** post income to your
books, so it won't double-count against the deposits you import on bank/Square statements. Re-running
the same file is safe (deduped by invoice number).


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

## Sub-accounts (granular categories)

Want more detail than a single "Vehicle Expenses" line? On the **Accounts** page, use **Add a
sub-account** — give it a name and pick the parent (e.g. "Vehicle Fuel" under "Vehicle
Expenses"). Then categorize transactions to the sub-account. On **Reports**, sub-accounts are
listed under their parent with a rolled-up subtotal, so you see both the detail and the total.

- A sub-account takes its parent's type automatically.
- Two levels (Category → Subcategory). Account names must be unique, so use distinct names like
  "Vehicle Fuel" instead of a second "Fuel".
- Anything you'd categorized directly to the parent still counts — it shows as a "(direct)" line.
- Change a sub-account's parent anytime with the Parent dropdown on the Accounts page.

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

**To restore a backup:** go to **Settings → Restore from a backup**, pick a restore point (rows
marked "(has data)" hold real records), and click **Restore**. Your current books are saved as a
`pre-restore` backup first, so you can undo. No file copying needed.

**The 💾 Save button** in the bottom-left of every screen makes a restore point on demand. Your
work already auto-saves to the database the instant you do anything — Save just stamps a backup you
can roll back to.

**If your books ever look empty**, a red banner appears at the top of every page pointing you to
Restore — that means a backup with your data exists and you can put it back in one click. (Empty
databases are never backed up, so an accidental reset can't overwrite your good backups.)

## Troubleshooting

| Symptom | Fix |
|---|---|
| Browser says can't connect | The app isn't running — double-click run.bat |
| PDF import found nothing | Add the Claude API key in Settings, or export CSV from the bank instead |
| Imported amounts are backwards | Review → "Flip signs" for that batch |
| Email send fails | Gmail needs an App Password (not your normal password); check user/port 587 |
| "possible duplicate" everywhere | You imported the same statement twice — Skip the copies |
| Books look wrong | Check each account's register against the real bank/card balance; delete and repost the bad entry |
