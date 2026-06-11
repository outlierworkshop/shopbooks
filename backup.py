"""Automatic and on-demand backups.

The user's books are irreplaceable, so ShopBooks protects them three ways:
  1. On every app start, a consistent snapshot of books.db is written to
     <datadir>/backups/ (last KEEP kept).
  2. Each snapshot is mirrored to a cloud folder (<OneDrive>/ShopBooks Backups/)
     when OneDrive is present, giving an automatic off-machine copy.
  3. A one-click full ZIP (books.db + all receipt images) is downloadable from
     Settings for the user to stash on an external drive.

Restore is intentionally manual (stop the app, copy a backups/books-*.db over
books.db) — documented in docs/USER_GUIDE.md — so nothing auto-overwrites live data.
"""
import io
import os
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

import db

CLOUD_DIRNAME = "ShopBooks Backups"
KEEP = 20


def cloud_dir():
    # In test/isolation mode (SHOPBOOKS_DATA_DIR set) never touch the real cloud folder.
    if os.environ.get("SHOPBOOKS_DATA_DIR"):
        return None
    one = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer") or os.environ.get("OneDriveCommercial")
    if one and Path(one).exists():
        return Path(one) / CLOUD_DIRNAME
    return None


def _prune(folder, pattern, keep):
    files = sorted(folder.glob(pattern))
    for old in files[:-keep] if len(files) > keep else []:
        try:
            old.unlink()
        except OSError:
            pass


def _consistent_copy(dest):
    """Copy the live DB via sqlite's backup API so it's valid even mid-write."""
    src = sqlite3.connect(db.DB_PATH)
    out = sqlite3.connect(dest)
    try:
        with out:
            src.backup(out)
    finally:
        out.close()
        src.close()


def snapshot(keep=KEEP):
    """Write a timestamped DB snapshot locally and mirror to cloud. Returns the path or None."""
    if not db.DB_PATH.exists():
        return None
    db.BACKUPS.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = db.BACKUPS / f"books-{ts}.db"
    if dest.exists():  # same-second restart
        return dest
    _consistent_copy(dest)
    _prune(db.BACKUPS, "books-*.db", keep)
    cloud = cloud_dir()
    if cloud:
        try:
            cloud.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(dest, cloud / dest.name)
            _prune(cloud, "books-*.db", keep)
        except OSError:
            pass  # cloud copy is best-effort; local snapshot already succeeded
    return dest


def zip_bytes():
    """Full backup: books.db + every receipt image, as ZIP bytes (for download)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if db.DB_PATH.exists():
            tmp = db.BACKUPS / "_ziptmp.db"
            db.BACKUPS.mkdir(parents=True, exist_ok=True)
            _consistent_copy(tmp)
            z.write(tmp, "books.db")
            tmp.unlink(missing_ok=True)
        if db.DOCS.exists():
            for f in db.DOCS.iterdir():
                if f.is_file():
                    z.write(f, f"docs/{f.name}")
    buf.seek(0)
    return buf.getvalue()


def status():
    """Summary for the Settings page."""
    local = sorted(db.BACKUPS.glob("books-*.db")) if db.BACKUPS.exists() else []
    last = None
    if local:
        last = datetime.fromtimestamp(local[-1].stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    cloud = cloud_dir()
    return {
        "data_dir": str(db.DATA),
        "local_count": len(local),
        "last_backup": last,
        "cloud_dir": str(cloud) if cloud else None,
    }
