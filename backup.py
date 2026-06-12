"""Automatic and on-demand backups.

The user's books are irreplaceable, so ShopBooks protects them three ways:
  1. On every app start, a consistent snapshot of books.db is written to
     <datadir>/backups/ (last KEEP kept).
  2. Each snapshot is mirrored to a cloud folder (<OneDrive>/ShopBooks Backups/)
     when OneDrive is present, giving an automatic off-machine copy.
  3. A one-click full ZIP (books.db + all receipt images) is downloadable from
     Settings for the user to stash on an external drive.

Restore is one click from Settings (`restore()` overwrites the live DB via the SQLite backup
API after stashing a `pre-restore-*` copy). A *fresh/seeded* DB is never snapshotted, so an
accidental reset can't evict the good backups, and the app warns when the live DB looks empty
but a data-bearing backup exists (`reset_suspected`).
"""
import io
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

import db

CLOUD_DIRNAME = "ShopBooks Backups"
KEEP = 40


def _configured_backup_dir():
    """User-set backup folder from settings, or '' if unset."""
    con = db.connect()
    try:
        return db.get_setting(con, "backup_dir", "").strip()
    finally:
        con.close()


def cloud_dir():
    """Where the off-machine backup copy goes: user-configured folder if set,
    else an auto-detected OneDrive subfolder. None in test mode or if neither exists."""
    if os.environ.get("SHOPBOOKS_DATA_DIR"):
        return None  # test/isolation mode: never touch a real backup folder
    configured = _configured_backup_dir()
    if configured:
        return Path(configured)
    one = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer") or os.environ.get("OneDriveCommercial")
    if one and Path(one).exists():
        return Path(one) / CLOUD_DIRNAME
    return None


def cloud_source():
    """'configured' | 'onedrive' | 'none' — how cloud_dir() was resolved (for the UI)."""
    if os.environ.get("SHOPBOOKS_DATA_DIR"):
        return "none"
    if _configured_backup_dir():
        return "configured"
    one = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer") or os.environ.get("OneDriveCommercial")
    return "onedrive" if (one and Path(one).exists()) else "none"


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


def looks_fresh(path=None):
    """True if the DB is a brand-new seeded shell (no real data) — not worth backing up,
    and a sign of an accidental reset if a data backup exists."""
    path = Path(path) if path else db.DB_PATH
    if not path.exists():
        return False
    try:
        c = sqlite3.connect(str(path))
        bn = c.execute("SELECT value FROM settings WHERE key='business_name'").fetchone()
        ent = c.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        cust = c.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        docs = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        c.close()
        bn = (bn[0] if bn else "") or ""
        return ent == 0 and cust == 0 and docs == 0 and bn in ("", "My Business")
    except sqlite3.Error:
        return False


def snapshot(keep=KEEP):
    """Write a timestamped DB snapshot locally and mirror to cloud. Returns the path or None.
    A fresh/seeded DB is skipped so an accidental reset can't evict the good backups."""
    if not db.DB_PATH.exists() or looks_fresh():
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
            shutil.copy2(dest, cloud / dest.name)
            _prune(cloud, "books-*.db", keep)
        except OSError:
            pass  # cloud copy is best-effort; local snapshot already succeeded
    return dest


def list_restorable():
    """All restore points (local + cloud), newest first, flagged whether they hold real data."""
    folders = [db.BACKUPS]
    cloud = cloud_dir()
    if cloud and cloud.exists():
        folders.append(cloud)
    seen, out = set(), []
    for folder in folders:
        if not folder.exists():
            continue
        for f in folder.glob("*.db"):
            if f.name in seen or f.name.startswith("_"):
                continue
            seen.add(f.name)
            try:
                st = f.stat()
            except OSError:
                continue
            out.append({"name": f.name, "mtime": st.st_mtime, "size": st.st_size,
                        "when": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                        "has_data": not looks_fresh(f)})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def restore(name):
    """Overwrite the live DB with a chosen backup, after stashing a pre-restore copy.
    Uses the SQLite backup API so it works even with the app running. Raises on a bad name."""
    safe = Path(name).name
    src = db.BACKUPS / safe
    if not src.exists():
        cloud = cloud_dir()
        src = (cloud / safe) if (cloud and (cloud / safe).exists()) else None
    if not src or not src.exists():
        raise FileNotFoundError(name)
    db.BACKUPS.mkdir(parents=True, exist_ok=True)
    if db.DB_PATH.exists():  # undo point (always, even if current looks fresh)
        _consistent_copy(db.BACKUPS / f"pre-restore-{datetime.now():%Y%m%d-%H%M%S}.db")
    source = sqlite3.connect(str(src))
    dest = sqlite3.connect(str(db.DB_PATH))
    try:
        with dest:
            source.backup(dest)  # transactional overwrite of the live DB's contents
    finally:
        source.close()
        dest.close()


def reset_suspected():
    """True when the live DB looks empty but a data-bearing backup exists (likely a reset)."""
    if not looks_fresh():
        return False
    return any(b["has_data"] for b in list_restorable())


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


def check_writable(folder):
    """True if we can create/write in `folder` (creates it if missing)."""
    try:
        p = Path(folder)
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".shopbooks_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def status():
    """Summary for the Settings page."""
    local = sorted(db.BACKUPS.glob("books-*.db")) if db.BACKUPS.exists() else []
    last = None
    if local:
        last = datetime.fromtimestamp(local[-1].stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    cloud = cloud_dir()
    cloud_count = len(list(cloud.glob("books-*.db"))) if cloud and cloud.exists() else 0
    return {
        "data_dir": str(db.DATA),
        "local_count": len(local),
        "last_backup": last,
        "cloud_dir": str(cloud) if cloud else None,
        "cloud_source": cloud_source(),
        "cloud_count": cloud_count,
        "cloud_writable": check_writable(cloud) if cloud else None,
        "configured": _configured_backup_dir(),
    }
