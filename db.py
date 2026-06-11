"""Database connection, schema, and seed data for ShopBooks.

Data location (the user's real books) is deliberately OUTSIDE the code folder so
that git operations, a re-clone, or a careless test cleanup can never touch it:

  - default:   %LOCALAPPDATA%\\ShopBooks  (stable per-user; not synced, not in repo)
  - override:  set SHOPBOOKS_DATA_DIR  (TESTS MUST set this to a temp dir)

See backup.py for the automatic snapshot/cloud-backup system.
"""
import os
import shutil
import sqlite3
from pathlib import Path

APP_NAME = "ShopBooks"
REPO_DIR = Path(__file__).resolve().parent
OLD_DATA = REPO_DIR / "data"   # legacy in-repo location, migrated away from on first run


def _default_data_dir():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME


def data_dir():
    override = os.environ.get("SHOPBOOKS_DATA_DIR")
    return Path(override).resolve() if override else _default_data_dir()


DATA = data_dir()
DOCS = DATA / "docs"
DB_PATH = DATA / "books.db"
BACKUPS = DATA / "backups"


def _migrate_old_location():
    """One-time move of a legacy in-repo data/ folder into the stable location.

    Only runs for the DEFAULT location (never when SHOPBOOKS_DATA_DIR is set, so
    tests never pull the repo's data/ into their temp dir). No-op once migrated.
    """
    if os.environ.get("SHOPBOOKS_DATA_DIR"):
        return
    if DB_PATH.exists() or not (OLD_DATA / "books.db").exists():
        return
    DATA.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    shutil.move(str(OLD_DATA / "books.db"), str(DB_PATH))
    old_docs = OLD_DATA / "docs"
    if old_docs.exists():
        for f in old_docs.iterdir():
            if f.is_file():
                shutil.move(str(f), str(DOCS / f.name))
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  type TEXT NOT NULL CHECK(type IN ('asset','liability','equity','income','expense')),
  kind TEXT NOT NULL DEFAULT 'category',   -- 'bank','card','category'
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  payee TEXT NOT NULL DEFAULT '',
  memo TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS splits(
  id INTEGER PRIMARY KEY,
  entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  amount_cents INTEGER NOT NULL
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
  entry_id INTEGER REFERENCES entries(id)
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
  uploaded_at TEXT DEFAULT (datetime('now'))
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
  paid_entry_id INTEGER REFERENCES entries(id),
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS invoice_items(
  id INTEGER PRIMARY KEY,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  description TEXT NOT NULL,
  qty REAL NOT NULL DEFAULT 1,
  unit_cents INTEGER NOT NULL
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
    "ai_model": "claude-opus-4-8",
    "anthropic_api_key": "",
    "business_name": "My Business",
    "backup_dir": "",            # extra/off-machine backup folder; blank = auto-detect OneDrive
    "business_address": "",
    "business_email": "",
    "business_phone": "",
    "invoice_terms": "Payment due within 30 days. Thank you for your business!",
    "next_invoice_number": "1001",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "email_subject": "Invoice {number} from {business}",
    "email_body": ("Hi {customer},\n\nAttached is invoice {number} for ${total}, "
                   "due {due_date}.\n\nThank you!\n{business}"),
}


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init():
    _migrate_old_location()
    con = connect()
    con.executescript(SCHEMA)
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
