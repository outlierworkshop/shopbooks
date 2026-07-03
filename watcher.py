"""Folder watchers: auto-scan configured folders for new bank statements and receipts, so the
owner can drop a file where their bank/phone already saves it instead of clicking Upload.

Deliberately NOT a system daemon (matches the app's local-first, no-background-service ethos) — a
lightweight polling thread that only runs while ShopBooks itself is running, started at app boot and
stopped at shutdown, same lifetime as the existing backup/sync-on-boot behavior. Nothing here ever
posts to the ledger: every processed file lands exactly where a manual upload would (pending in
Review, or an unmatched/matched receipt) — the human-confirmed Review step is unchanged.

Reprocessing is cheap: `watched_files` tracks (path, mtime, size) per file, so an unchanged file is a
fast no-op on every tick; a replaced file (mtime/size changed) is picked up again.
"""
import threading
import time
from datetime import date
from pathlib import Path

import db

DEFAULT_INTERVAL = 60  # seconds between ticks while the server is running

_thread = None
_stop = threading.Event()
_LAST = None  # last run_once() summary, for the Settings page


def _list_files(folder):
    """Top-level files in `folder` (not recursive — keeps behavior predictable), or [] if the
    folder is missing / not yet accessible (e.g. an undownloaded Dropbox placeholder). Never raises
    — a watcher tick must never take down the background thread over a transient path problem."""
    try:
        p = Path(folder)
        if not p.is_dir():
            return []
        return [f for f in sorted(p.iterdir()) if f.is_file() and not f.name.startswith(".")]
    except OSError:
        return []


def scan_folder(con, folder, kind, exts, process_fn):
    """Scan one folder for files with an extension in `exts`; call process_fn(con, path, data) ->
    (status, note) for each new-or-changed file (per `watched_files`); record the result. Returns
    a summary dict. process_fn exceptions are caught per-file so one bad file doesn't stop the scan."""
    counts = {}
    errors = []
    if not str(folder or "").strip():
        return {"scanned": 0, "counts": counts, "errors": errors, "enabled": False}
    scanned = 0
    for f in _list_files(folder):
        if f.suffix.lower() not in exts:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        mtime, size = st.st_mtime, st.st_size
        row = con.execute("SELECT mtime, size FROM watched_files WHERE path=?", (str(f),)).fetchone()
        if row and row["mtime"] == mtime and row["size"] == size:
            continue  # already processed this exact version of the file
        scanned += 1
        try:
            data = f.read_bytes()
            status, note = process_fn(con, f, data)
        except Exception as e:
            status, note = "error", str(e)[:300]
            errors.append(f"{f.name}: {note}")
        con.execute(
            "INSERT INTO watched_files(path,kind,mtime,size,status,note,processed_at) "
            "VALUES(?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(path) DO UPDATE SET kind=?, mtime=?, size=?, status=?, note=?, processed_at=datetime('now')",
            (str(f), kind, mtime, size, status, note, kind, mtime, size, status, note))
        counts[status] = counts.get(status, 0) + 1
    return {"scanned": scanned, "counts": counts, "errors": errors, "enabled": True}


def run_once(con, statement_fn, receipt_fn):
    """One tick: scan both configured folders (if set). `statement_fn`/`receipt_fn` are
    (con, path, data) -> (status, note) callbacks supplied by the caller (app.py), so this module
    has no dependency on the statement-import or receipt-ingestion pipelines themselves."""
    global _LAST
    statements = scan_folder(con, db.get_setting(con, "statements_watch_folder", ""),
                             "statement", {".pdf", ".csv"}, statement_fn)
    receipts = scan_folder(con, db.get_setting(con, "receipts_watch_folder", ""),
                           "receipt", {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf"}, receipt_fn)
    summary = {"at": date.today().isoformat(), "statements": statements, "receipts": receipts}
    _LAST = summary
    return summary


def status():
    return _LAST


def _loop(statement_fn, receipt_fn, interval):
    while not _stop.is_set():
        con = db.connect()
        try:
            run_once(con, statement_fn, receipt_fn)
            con.commit()
        except Exception:
            pass  # a watcher tick must never crash the background thread
        finally:
            con.close()
        _stop.wait(interval)


def start(statement_fn, receipt_fn, interval=DEFAULT_INTERVAL):
    """Start the background thread (idempotent — a second call while one is running is a no-op)."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(statement_fn, receipt_fn, interval), daemon=True)
    _thread.start()


def stop(timeout=2):
    _stop.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=timeout)
