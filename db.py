"""Database connection, schema, and seed data for ShopBooks.

Data location (the user's real books) is deliberately OUTSIDE the code folder so
that git operations, a re-clone, or a careless test cleanup can never touch it:

  - default:   %USERPROFILE%\\ShopBooks (Windows) / ~/Library/Application Support/ShopBooks (mac)
               -- stable per-user, not synced, not in repo, and OUTSIDE %AppData% so an
               MSIX-sandboxed host (e.g. the Claude desktop app) can't redirect it (see below)
  - override:  set SHOPBOOKS_DATA_DIR  (TESTS MUST set this to a temp dir)

See backup.py for the automatic snapshot/cloud-backup system.
"""
import os
import shutil
import sqlite3
import sys
from pathlib import Path

APP_NAME = "ShopBooks"
REPO_DIR = Path(__file__).resolve().parent
OLD_DATA = REPO_DIR / "data"   # legacy in-repo location, migrated away from on first run
# ~/AppData/Local/ShopBooks: the old Windows default (pre-2026-06-23, before moving out of
# %AppData% to dodge MSIX redirection) and also a pre-per-OS Mac/Linux fallback. Migrated forward
# automatically by _migrate_old_location. Uses Path.home() so it reads the REAL profile, not a
# sandbox-redirected one.
LEGACY_APPDATA = Path.home() / "AppData" / "Local" / APP_NAME


def _default_data_dir():
    """Per-OS stable location, outside the repo AND outside %AppData%.

    Windows uses %USERPROFILE%\\ShopBooks, deliberately NOT %LOCALAPPDATA%: when the app is
    launched from inside an MSIX-packaged host (e.g. the Claude desktop app), %LOCALAPPDATA% is
    silently redirected into a per-package sandbox, so a %LOCALAPPDATA%-based path resolves to a
    different, empty database and the books look blank. %USERPROFILE% is never redirected. macOS
    uses ~/Library/Application Support; Linux uses $XDG_DATA_HOME or ~/.local/share. Old Windows
    installs under %LOCALAPPDATA%\\ShopBooks are carried forward automatically by
    _migrate_old_location (that path == LEGACY_APPDATA on Windows)."""
    if os.name == "nt":
        base = os.environ.get("USERPROFILE") or str(Path.home())
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def data_dir():
    override = os.environ.get("SHOPBOOKS_DATA_DIR")
    if override:
        return Path(override).resolve()
        
    # Programmatic safety guard to prevent modifying real books during test execution
    import sys
    is_test = False
    if "pytest" in sys.modules or "unittest" in sys.modules:
        is_test = True
    else:
        # Check if the executing script's basename looks like a test runner or test file
        if sys.argv:
            main_script = os.path.basename(sys.argv[0]).lower()
            if (main_script.startswith("test_") or 
                main_script.endswith("_test.py") or 
                main_script == "conftest.py" or 
                "pytest" in main_script or 
                "unittest" in main_script):
                is_test = True
                
    if is_test:
        raise RuntimeError(
            "FATAL: Database imported in a test environment/script (detected via sys.argv/sys.modules) "
            "but SHOPBOOKS_DATA_DIR is not set. To protect the user's real data, you must set "
            "os.environ['SHOPBOOKS_DATA_DIR'] to a temporary directory before importing db or app."
        )
        
    return _default_data_dir()


DATA = data_dir()
DOCS = DATA / "docs"
DB_PATH = DATA / "books.db"
BACKUPS = DATA / "backups"


def _migrate_from(old_dir):
    """Move a legacy data dir's contents into the current DATA location (books.db, receipts,
    backups, and the sync sidecar), then repoint stored receipt paths. One-time; no-op if the
    new location already has books or the old one has none. Never runs against itself."""
    old_dir = Path(old_dir)
    if old_dir.resolve() == DATA.resolve():
        return
    if DB_PATH.exists() or not (old_dir / "books.db").exists():
        return
    DATA.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_dir / "books.db"), str(DB_PATH))
    old_docs = old_dir / "docs"
    if old_docs.exists():
        for f in old_docs.iterdir():
            if f.is_file():
                shutil.move(str(f), str(DOCS / f.name))
    old_backups = old_dir / "backups"
    if old_backups.exists():
        BACKUPS.mkdir(parents=True, exist_ok=True)
        for f in old_backups.glob("*"):
            if f.is_file():
                shutil.move(str(f), str(BACKUPS / f.name))
    side = old_dir / "sync_state.json"   # keep two-machine sync lineage intact across the move
    if side.exists():
        shutil.move(str(side), str(DATA / "sync_state.json"))
    # fix stored absolute receipt paths to point at the new docs folder
    con = sqlite3.connect(DB_PATH)
    try:
        for row in con.execute("SELECT id, filename FROM documents").fetchall():
            con.execute("UPDATE documents SET path=? WHERE id=?",
                        (str(DOCS / row[1]), row[0]))
        con.commit()
    except sqlite3.OperationalError:
        pass  # documents table absent in a very old schema; nothing to fix
    finally:
        con.close()


def _migrate_old_location():
    """One-time move of a legacy data location into the current stable per-OS location.

    Only runs for the DEFAULT location (never when SHOPBOOKS_DATA_DIR is set, so tests never
    pull real data into their temp dir). Handles both the old in-repo data/ folder and the
    ~/AppData/Local/ShopBooks location (the old Windows default, and a pre-per-OS Mac/Linux
    fallback) -> carried forward to the current %USERPROFILE%\\ShopBooks. No-op once migrated.
    """
    if os.environ.get("SHOPBOOKS_DATA_DIR"):
        return
    _migrate_from(OLD_DATA)        # legacy in-repo data/
    _migrate_from(LEGACY_APPDATA)  # legacy macOS/Linux fallback (~/AppData/Local/ShopBooks)

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  parent_id INTEGER REFERENCES accounts(id),  -- NULL = top-level; else a sub-account (2 levels max)
  type TEXT NOT NULL CHECK(type IN ('asset','liability','equity','income','expense')),
  kind TEXT NOT NULL DEFAULT 'category',   -- 'bank','card','category'
  active INTEGER NOT NULL DEFAULT 1,
  schedule_c_line TEXT
);
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  payee TEXT NOT NULL DEFAULT '',
  memo TEXT NOT NULL DEFAULT '',
  job_id INTEGER REFERENCES jobs(id),  -- optional: tags this transaction to a job (for job costing)
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS splits(
  id INTEGER PRIMARY KEY,
  entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  amount_cents INTEGER NOT NULL,
  reconciled_id INTEGER REFERENCES reconciliations(id)  -- set when this account-leg is cleared in a reconciliation (Phase 2)
);
CREATE INDEX IF NOT EXISTS idx_splits_account ON splits(account_id);
CREATE INDEX IF NOT EXISTS idx_splits_entry ON splits(entry_id);
CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY,
  pattern TEXT NOT NULL,                   -- case-insensitive substring match
  account_id INTEGER NOT NULL REFERENCES accounts(id)
);
CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY,
  filename TEXT,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  imported_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS staged(
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
  date TEXT NOT NULL,
  description TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,           -- positive = money out (charge), negative = money in
  category_id INTEGER REFERENCES accounts(id),
  status TEXT NOT NULL DEFAULT 'pending',  -- pending/posted/skipped
  entry_id INTEGER REFERENCES entries(id),
  memo TEXT NOT NULL DEFAULT ''            -- optional note, carried to the posted entry's memo
);
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY,
  filename TEXT NOT NULL,
  path TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'receipt',
  vendor TEXT DEFAULT '',
  doc_date TEXT DEFAULT '',
  amount_cents INTEGER,
  status TEXT NOT NULL DEFAULT 'unmatched',  -- unmatched/matched
  entry_id INTEGER REFERENCES entries(id),
  uploaded_at TEXT DEFAULT (datetime('now')),
  staged_id INTEGER REFERENCES staged(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS document_staged_links(
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  staged_id INTEGER NOT NULL REFERENCES staged(id) ON DELETE CASCADE,
  PRIMARY KEY (document_id, staged_id)
);
CREATE TABLE IF NOT EXISTS document_entry_links(
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  PRIMARY KEY (document_id, entry_id)
);
CREATE TABLE IF NOT EXISTS mileage(
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  miles REAL NOT NULL,
  purpose TEXT NOT NULL DEFAULT '',
  from_loc TEXT DEFAULT '',
  to_loc TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS customers(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT DEFAULT '',
  address TEXT DEFAULT '',
  phone TEXT DEFAULT '',
  notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS invoices(
  id INTEGER PRIMARY KEY,
  number TEXT UNIQUE NOT NULL,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  date TEXT NOT NULL,
  due_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',   -- draft/sent/paid/void
  memo TEXT DEFAULT '',
  paid_date TEXT,
  paid_entry_id INTEGER REFERENCES entries(id),    -- entry WE posted via Record Payment (we own it)
  matched_entry_id INTEGER REFERENCES entries(id), -- existing deposit this invoice is linked to (we do NOT own it)
  kind TEXT NOT NULL DEFAULT 'invoice',            -- 'invoice' | 'estimate' (estimates never post/match)
  converted_invoice_id INTEGER REFERENCES invoices(id),  -- for an estimate: the invoice it became
  last_reminder_date TEXT,                         -- when the last overdue reminder email was sent (AR follow-ups)
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS invoice_items(
  id INTEGER PRIMARY KEY,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  description TEXT NOT NULL,
  qty REAL NOT NULL DEFAULT 1,
  unit_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invoice_entry_links(
  invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  PRIMARY KEY (invoice_id, entry_id)
);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  customer_id INTEGER REFERENCES customers(id),  -- optional link to a customer
  status TEXT NOT NULL DEFAULT 'active',          -- active/done
  notes TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS time_entries(
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,                   -- ISO YYYY-MM-DD
  hours REAL NOT NULL,
  job_id INTEGER REFERENCES jobs(id),   -- optional
  category TEXT NOT NULL DEFAULT '',    -- free-text work type: carving, finishing, admin...
  note TEXT NOT NULL DEFAULT '',
  billable INTEGER NOT NULL DEFAULT 0,  -- 0/1
  rate_cents INTEGER,                   -- per-hour billing rate; NULL = use default_hourly_rate
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_time_job ON time_entries(job_id);
CREATE INDEX IF NOT EXISTS idx_time_date ON time_entries(date);
CREATE TABLE IF NOT EXISTS reconciliations(
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  statement_date TEXT NOT NULL,             -- ISO YYYY-MM-DD (statement closing date)
  statement_balance_cents INTEGER NOT NULL, -- ending balance off the statement (natural/display sign)
  book_balance_cents INTEGER NOT NULL,      -- book balance as-of statement_date, snapshotted at reconcile time
  difference_cents INTEGER NOT NULL,        -- statement - book (0 = reconciled)
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_recon_account ON reconciliations(account_id);

CREATE TABLE IF NOT EXISTS recurring(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,                        -- payee / what it's for (e.g. 'Shop rent')
  amount_cents INTEGER NOT NULL,             -- always positive; `flow` sets the direction
  flow TEXT NOT NULL DEFAULT 'expense',      -- 'expense' (money out) | 'income' (money in)
  account_id INTEGER NOT NULL REFERENCES accounts(id),   -- bank/card it's paid from / deposited to
  category_id INTEGER NOT NULL REFERENCES accounts(id),  -- the expense/income category
  frequency TEXT NOT NULL DEFAULT 'monthly', -- 'weekly' | 'monthly' | 'yearly'
  next_date TEXT NOT NULL,                   -- next date this is due to post (YYYY-MM-DD)
  memo TEXT DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  last_posted_date TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
"""

SEED_ACCOUNTS = [
    # (name, type, kind)
    ("Business Checking", "asset", "bank"),
    ("Credit Card 1", "liability", "card"),
    ("Credit Card 2", "liability", "card"),
    ("Credit Card 3", "liability", "card"),
    ("Owner's Equity", "equity", "category"),
    ("Owner's Draw", "equity", "category"),
    ("Sales - Square", "income", "category"),
    ("Sales - ACH / Invoices", "income", "category"),
    ("Other Income", "income", "category"),
    ("Advertising & Marketing", "expense", "category"),
    ("Bank & Merchant Fees", "expense", "category"),
    ("Contract Labor", "expense", "category"),
    ("Insurance", "expense", "category"),
    ("Internet & Phone", "expense", "category"),
    ("Materials & Supplies", "expense", "category"),
    ("Meals (Business)", "expense", "category"),
    ("Office Supplies", "expense", "category"),
    ("Professional Services", "expense", "category"),
    ("Rent / Lease", "expense", "category"),
    ("Repairs & Maintenance", "expense", "category"),
    ("Shipping & Postage", "expense", "category"),
    ("Software & Subscriptions", "expense", "category"),
    ("Taxes & Licenses", "expense", "category"),
    ("Tools & Small Equipment", "expense", "category"),
    ("Travel", "expense", "category"),
    ("Utilities", "expense", "category"),
    ("Vehicle Expenses", "expense", "category"),
    ("Uncategorized Expense", "expense", "category"),
]

SEED_RULES = [
    ("SQUARE", "Sales - Square"),
    ("SQ *", "Sales - Square"),
    ("USPS", "Shipping & Postage"),
    ("UPS", "Shipping & Postage"),
    ("FEDEX", "Shipping & Postage"),
    ("HOME DEPOT", "Materials & Supplies"),
    ("LOWES", "Materials & Supplies"),
    ("MCMASTER", "Materials & Supplies"),
    ("GRAINGER", "Materials & Supplies"),
    ("ADOBE", "Software & Subscriptions"),
    ("GITHUB", "Software & Subscriptions"),
    ("GOOGLE", "Software & Subscriptions"),
    ("MICROSOFT", "Software & Subscriptions"),
    ("COMCAST", "Internet & Phone"),
    ("VERIZON", "Internet & Phone"),
    ("AT&T", "Internet & Phone"),
    ("SERVICE FEE", "Bank & Merchant Fees"),
    ("MONTHLY FEE", "Bank & Merchant Fees"),
    ("AMAZON", "Office Supplies"),
]

DEFAULT_SETTINGS = {
    "mileage_rate": "0.70",      # $/mile - verify the current IRS rate each January
    "default_hourly_rate": "0",  # $/hour for billable time; per-entry rate overrides this
    "ai_backend": "claude",      # claude | ollama | hybrid
    "ai_model": "claude-opus-4-8",
    "categorize_model": "",      # optional cheaper/faster model for categorization; blank = use ai_model
    "anthropic_api_key": "",
    "ollama_url": "http://localhost:11434",
    "ollama_model": "llama3.2-vision",
    "business_name": "My Business",
    "backup_dir": "",            # extra/off-machine backup folder; blank = auto-detect OneDrive
    "sync_enabled": "0",         # two-machine cloud sync (see sync.py); off by default
    "books_locked_through": "",  # year-end close: entries on/before this date are frozen; blank = nothing locked
    "business_address": "",
    "business_email": "",
    "business_phone": "",
    "invoice_terms": "Payment due within 30 days. Thank you for your business!",
    "next_invoice_number": "1001",
    "next_estimate_number": "1001",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "email_subject": "Invoice {number} from {business}",
    "email_body": ("Hi {customer},\n\nAttached is invoice {number} for ${total}, "
                   "due {due_date}.\n\nThank you!\n{business}"),
    "reminder_subject": "Reminder: invoice {number} from {business} is past due",
    "reminder_body": ("Hi {customer},\n\nA friendly reminder that invoice {number} for ${total} "
                      "was due {due_date} and is still open. The invoice is attached again for your "
                      "convenience.\n\nThank you!\n{business}"),
    "estimated_income_tax_rate": "15",
}


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _column_migrations(con):
    """Add columns to existing tables. `CREATE TABLE IF NOT EXISTS` never alters an existing
    table, so every new column on a shipped table needs a guarded ALTER here. Existing user
    data must always survive an upgrade."""
    con.execute("""
    CREATE TABLE IF NOT EXISTS document_staged_links(
      document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      staged_id INTEGER NOT NULL REFERENCES staged(id) ON DELETE CASCADE,
      PRIMARY KEY (document_id, staged_id)
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS document_entry_links(
      document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
      PRIMARY KEY (document_id, entry_id)
    )""")

    have = {r["name"] for r in con.execute("PRAGMA table_info(documents)").fetchall()}
    if "sha256" not in have:
        con.execute("ALTER TABLE documents ADD COLUMN sha256 TEXT")
    if "staged_id" not in have:
        con.execute("ALTER TABLE documents ADD COLUMN staged_id INTEGER REFERENCES staged(id) ON DELETE SET NULL")

    if "staged_id" in have:
        if not con.execute("SELECT 1 FROM document_staged_links LIMIT 1").fetchone():
            con.execute(
                "INSERT INTO document_staged_links(document_id, staged_id) "
                "SELECT id, staged_id FROM documents WHERE staged_id IS NOT NULL"
            )
    if "entry_id" in have:
        if not con.execute("SELECT 1 FROM document_entry_links LIMIT 1").fetchone():
            con.execute(
                "INSERT INTO document_entry_links(document_id, entry_id) "
                "SELECT id, entry_id FROM documents WHERE entry_id IS NOT NULL"
            )

    acct = {r["name"] for r in con.execute("PRAGMA table_info(accounts)").fetchall()}
    if "parent_id" not in acct:
        con.execute("ALTER TABLE accounts ADD COLUMN parent_id INTEGER REFERENCES accounts(id)")
    if "schedule_c_line" not in acct:
        con.execute("ALTER TABLE accounts ADD COLUMN schedule_c_line TEXT")
    ent = {r["name"] for r in con.execute("PRAGMA table_info(entries)").fetchall()}
    if "job_id" not in ent:
        con.execute("ALTER TABLE entries ADD COLUMN job_id INTEGER REFERENCES jobs(id)")
    invc = {r["name"] for r in con.execute("PRAGMA table_info(invoices)").fetchall()}
    if "matched_entry_id" not in invc:
        con.execute("ALTER TABLE invoices ADD COLUMN matched_entry_id INTEGER REFERENCES entries(id)")
    stg = {r["name"] for r in con.execute("PRAGMA table_info(staged)").fetchall()}
    if "memo" not in stg:
        con.execute("ALTER TABLE staged ADD COLUMN memo TEXT NOT NULL DEFAULT ''")
    spl = {r["name"] for r in con.execute("PRAGMA table_info(splits)").fetchall()}
    if "reconciled_id" not in spl:
        con.execute("ALTER TABLE splits ADD COLUMN reconciled_id INTEGER REFERENCES reconciliations(id)")
    invc2 = {r["name"] for r in con.execute("PRAGMA table_info(invoices)").fetchall()}
    if "kind" not in invc2:
        con.execute("ALTER TABLE invoices ADD COLUMN kind TEXT NOT NULL DEFAULT 'invoice'")
    if "converted_invoice_id" not in invc2:
        con.execute("ALTER TABLE invoices ADD COLUMN converted_invoice_id INTEGER REFERENCES invoices(id)")
    if "last_reminder_date" not in invc2:
        con.execute("ALTER TABLE invoices ADD COLUMN last_reminder_date TEXT")

    con.execute("""
    CREATE TABLE IF NOT EXISTS invoice_entry_links(
      invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
      entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
      PRIMARY KEY (invoice_id, entry_id)
    )""")
    if con.execute("SELECT COUNT(*) FROM invoice_entry_links").fetchone()[0] == 0:
        con.execute("""
        INSERT INTO invoice_entry_links (invoice_id, entry_id)
        SELECT id, matched_entry_id FROM invoices WHERE matched_entry_id IS NOT NULL
        """)


def init():
    _migrate_old_location()
    con = connect()
    con.executescript(SCHEMA)
    _column_migrations(con)
    if not con.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
        for name, typ, kind in SEED_ACCOUNTS:
            con.execute("INSERT INTO accounts(name,type,kind) VALUES(?,?,?)", (name, typ, kind))
        for pattern, acct in SEED_RULES:
            row = con.execute("SELECT id FROM accounts WHERE name=?", (acct,)).fetchone()
            if row:
                con.execute("INSERT INTO rules(pattern,account_id) VALUES(?,?)", (pattern, row["id"]))
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    con.commit()
    con.close()


def get_setting(con, key, default=""):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(con, key, value):
    con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
