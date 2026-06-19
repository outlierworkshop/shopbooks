"""Data-location migration: per-OS default dir + carrying a legacy location forward.

Isolation: SHOPBOOKS_DATA_DIR -> temp before importing db. We then monkeypatch the module
paths (as test_safety does) to exercise _migrate_from against a fake legacy dir, so the real
data dir is never touched.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_migtest_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db  # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)

# --- per-OS default location -------------------------------------------------
d = str(db._default_data_dir())
if sys.platform == "darwin":
    ok("Library/Application Support/ShopBooks" in d, f"macOS default is Application Support ({d})")
elif os.name == "nt":
    ok(d.endswith("ShopBooks"), f"Windows default under LOCALAPPDATA ({d})")
else:
    ok(d.endswith("ShopBooks"), f"Linux default under share dir ({d})")

# --- build a fake LEGACY data dir (old ~/AppData/Local/ShopBooks style) ------
old = TMP / "legacy"
(old / "docs").mkdir(parents=True)
(old / "backups").mkdir(parents=True)
oc = sqlite3.connect(old / "books.db")
oc.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, filename TEXT, path TEXT)")
oc.execute("INSERT INTO documents(filename, path) VALUES('r.jpg', 'C:/old/r.jpg')")
oc.commit(); oc.close()
(old / "docs" / "r.jpg").write_bytes(b"img")
(old / "backups" / "books-20260101-000000.db").write_bytes(b"bak")
(old / "sync_state.json").write_text('{"machine_id":"X","base_version":5}')

# point the module at a fresh NEW location and run the migration
new = TMP / "new"
orig = (db.DATA, db.DOCS, db.DB_PATH, db.BACKUPS)
db.DATA, db.DOCS, db.DB_PATH, db.BACKUPS = new, new / "docs", new / "books.db", new / "backups"
os.environ.pop("SHOPBOOKS_DATA_DIR")  # migration is guarded off when the override is set
db._migrate_from(old)

ok((new / "books.db").exists() and not (old / "books.db").exists(), "books.db moved to the new location")
ok((new / "docs" / "r.jpg").exists(), "receipt image moved")
ok((new / "backups" / "books-20260101-000000.db").exists(), "backups carried over")
ok((new / "sync_state.json").read_text().find('"base_version":5') > 0, "sync lineage (sync_state.json) preserved")
mc = sqlite3.connect(new / "books.db")
fixed = mc.execute("SELECT path FROM documents").fetchone()[0]
mc.close()
ok(fixed == str(new / "docs" / "r.jpg"), "stored receipt path repointed to the new docs folder")

# idempotent / safe: with the new location populated, a second migrate is a no-op
db._migrate_from(old)
ok((new / "books.db").exists(), "second migrate is a harmless no-op (new location already has books)")

db.DATA, db.DOCS, db.DB_PATH, db.BACKUPS = orig
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nDATA MIGRATION TESTS DONE")
