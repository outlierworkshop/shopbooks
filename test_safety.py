"""Data-safety tests: isolation via SHOPBOOKS_DATA_DIR + backup system.

This file demonstrates the MANDATORY test pattern: point SHOPBOOKS_DATA_DIR at a
temp dir BEFORE importing db/app, so tests can never touch real books.
"""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_test_"))
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db  # noqa: E402  (import after env var is set)
import backup  # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)

# 1. isolation: every path must be under the temp dir, not the repo or AppData
ok(db.DATA == TMP, f"DATA points at temp dir ({db.DATA})")
ok(str(db.DB_PATH).startswith(str(TMP)), "DB_PATH under temp dir")
ok(db.DATA != db._default_data_dir() and db.REPO_DIR not in db.DATA.parents,
   "live data dir is neither the default location nor inside the repo")
repo_data = (db.REPO_DIR / "data" / "books.db")
repo_existed = repo_data.exists()

db.init()
ok(db.DB_PATH.exists(), "init created DB in temp dir")
ok(repo_data.exists() == repo_existed, "init did NOT create/migrate repo data/ (override set)")

# 2. backup snapshot lands in temp dir
import app  # noqa: E402  (triggers db.init + backup.snapshot at import)
snaps = list((TMP / "backups").glob("books-*.db"))
ok(len(snaps) >= 1, f"startup snapshot created ({len(snaps)} found)")

# snapshot is a valid sqlite db with the schema
import sqlite3  # noqa: E402
c = sqlite3.connect(snaps[0])
n = c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
c.close()
ok(n > 0, "snapshot is a valid DB with seeded accounts")

# 3. full ZIP contains books.db
import io, zipfile  # noqa: E402
z = zipfile.ZipFile(io.BytesIO(backup.zip_bytes()))
ok("books.db" in z.namelist(), "ZIP backup contains books.db")

# 4. retention prunes to KEEP
for i in range(backup.KEEP + 5):
    # force distinct filenames
    import time
    p = TMP / "backups" / f"books-2020010{i % 9}-00000{i % 9}.db"
    p.write_bytes(b"x")
backup._prune(TMP / "backups", "books-*.db", backup.KEEP)
remaining = list((TMP / "backups").glob("books-*.db"))
ok(len(remaining) <= backup.KEEP, f"retention prunes to <= {backup.KEEP} ({len(remaining)})")

# 5. migration logic: simulate an old in-repo DB moving to a fresh location (direct call)
import shutil, sqlite3 as s3  # noqa: E402
old = TMP / "fake_repo" / "data"
old.mkdir(parents=True)
oc = s3.connect(old / "books.db"); oc.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, filename TEXT, path TEXT)")
oc.execute("INSERT INTO documents(filename,path) VALUES('r.jpg','C:/old/r.jpg')"); oc.commit(); oc.close()
(old / "docs").mkdir(); (old / "docs" / "r.jpg").write_bytes(b"img")
newloc = TMP / "fake_new"
# monkeypatch module paths and run migration
orig = (db.DATA, db.DOCS, db.DB_PATH, db.OLD_DATA)
db.DATA, db.DOCS, db.DB_PATH, db.OLD_DATA = newloc, newloc / "docs", newloc / "books.db", old
os.environ.pop("SHOPBOOKS_DATA_DIR")  # migration only runs for default location
db._migrate_old_location()
moved_db = (newloc / "books.db").exists()
moved_img = (newloc / "docs" / "r.jpg").exists()
mc = s3.connect(newloc / "books.db")
fixed = mc.execute("SELECT path FROM documents").fetchone()[0]
mc.close()
db.DATA, db.DOCS, db.DB_PATH, db.OLD_DATA = orig
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)
ok(moved_db and moved_img, "migration moved DB + receipts to new location")
ok(fixed == str(newloc / "docs" / "r.jpg"), "migration rewrote receipt path")

shutil.rmtree(TMP, ignore_errors=True)
print("\nDATA-SAFETY TESTS DONE")
