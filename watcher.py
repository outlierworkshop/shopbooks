"""Folder watchers: auto-scan configured folders for new bank statements and receipts, so the
owner can drop a file where their bank/phone already saves it instead of clicking Upload.

Deliberately NOT a system daemon (matches the app's local-first, no-background-service ethos) — a
lightweight polling thread that only runs while ShopBooks itself is running, started at app boot and
stopped at shutdown, same lifetime as the existing backup/sync-on-boot behavior. Nothing here ever
posts to the ledger: every processed file lands exactly where a manual upload would (pending in
Review, or an unmatched/matched receipt) — the human-confirmed Review step is unchanged.

Reprocessing is cheap: `watched_files` tracks (key, mtime, size) per file, so an unchanged file is a
fast no-op on every tick; a replaced file (mtime/size changed) is picked up again. The key is
`watch_key()` — machine-independent for a cloud-synced file — so the Mac and the PC share one row
per file instead of re-processing everything after each switch.
"""
import json
import os
import re
import threading
import time
from datetime import date
from pathlib import Path

import db
from logutil import log

DEFAULT_INTERVAL = 60  # seconds between ticks while the server is running

_thread = None
_stop = threading.Event()
_LAST = None  # last run_once() summary, for the Settings page


def _cloud_roots():
    """This machine's synced cloud folders (Dropbox/OneDrive), best-effort. Dropbox's own
    info.json is authoritative when present (both OSes write one); the rest are the conventional
    locations. Only directories that actually exist are returned."""
    home = Path.home()
    roots = []
    for meta in (home / ".dropbox" / "info.json",
                 Path(os.environ.get("APPDATA") or "/nonexistent") / "Dropbox" / "info.json",
                 Path(os.environ.get("LOCALAPPDATA") or "/nonexistent") / "Dropbox" / "info.json"):
        try:
            roots += [Path(acct["path"]) for acct in json.loads(meta.read_text()).values()
                      if isinstance(acct, dict) and acct.get("path")]
        except (OSError, ValueError):
            pass
    roots += [Path(os.environ[v]) for v in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
              if os.environ.get(v)]
    roots += [home / "Dropbox", home / "OneDrive", home / "Library" / "CloudStorage" / "Dropbox"]
    try:
        roots += sorted(d for d in (home / "Library" / "CloudStorage").iterdir()
                        if d.name.lower().startswith("onedrive"))
    except OSError:
        pass
    out = []
    for r in roots:
        if r.is_dir() and r not in out:
            out.append(r)
    return out


CLOUD_NAMES = ("dropbox", "onedrive")
KEY_PREFIX = "cloud:"


def _cloud_split(raw):
    """(canonical cloud name, [components after it]) for a path inside a synced cloud folder, else
    None. Purely textual — the path needn't exist here, which is what lets this read the OTHER
    machine's paths. The folder's own name is canonicalised ('Dropbox (Personal)' -> 'dropbox') so
    the two machines agree even when their cloud folders are named differently."""
    parts = [s for s in re.split(r"[\\/]+", str(raw or "")) if s and not s.endswith(":")]
    for i, s in enumerate(parts):
        name = s.lower().replace("-", " ").split(" ")[0]
        if name in CLOUD_NAMES:
            return name, parts[i + 1:]
    return None


def watch_key(path):
    """The `watched_files` identity for a file: machine-independent when it lives in a cloud folder.

    The books sync between the Mac and the PC, but the same synced file has a different absolute
    path on each — so keying on that path gave one row per machine and re-processed every file once
    per switch. A file under Dropbox/OneDrive is therefore keyed by its path *within* the cloud
    folder (`cloud:dropbox/BP Admin/ShopBooks/Downloads/x.csv`), which both machines compute
    identically. Anything outside a cloud folder keeps its absolute path, which is correct: a
    local-only folder really is per-machine.

    Lower-cased because the components before the filename come from the watch-folder SETTING as
    typed, and the two machines may have typed it with different case. Never raises."""
    raw = str(path or "")
    if raw.startswith(KEY_PREFIX):
        return raw                      # already a key — keep this idempotent (it re-runs on boot)
    split = _cloud_split(raw)
    if not split or not split[1]:
        return raw
    name, rel = split
    return (KEY_PREFIX + "/".join([name] + rel)).lower()


def resolve_path(raw, roots=None):
    """A watch path saved on the OTHER machine, translated to this one (or returned untouched).

    The books move between the Windows machine and the Mac, but a stored path like
    `C:\\Users\\outli\\Dropbox\\Phone\\TravelLog\\triplog.txt` only exists on one of them — the
    same Dropbox folder lives at `~/Library/CloudStorage/Dropbox/...` on the Mac. When the
    configured path is missing here but contains a cloud-folder component (Dropbox/OneDrive), the
    part after that component is re-rooted onto this machine's copy of the cloud folder, and the
    translation is used only if the re-rooted path actually exists. Everything else — existing
    paths, non-cloud paths, no local match — comes back unchanged, so error messages always show
    what's configured. Never raises."""
    raw = str(raw or "").strip()
    if not raw:
        return raw
    try:
        if Path(raw).exists():
            return raw
    except OSError:
        return raw
    split = _cloud_split(raw)
    if not split or not split[1]:
        return raw
    rel = split[1]
    for root in (_cloud_roots() if roots is None else roots):
        cand = Path(root).joinpath(*rel)
        try:
            if cand.exists():
                return str(cand)
        except OSError:
            continue
    return raw


def _list_files(folder):
    """Top-level files in `folder` (not recursive — keeps behavior predictable), or [] if the
    folder is missing / not yet accessible (e.g. an undownloaded Dropbox placeholder). A path that
    IS a file watches just that file — the trips setting pointed at `...\\TravelLog\\triplog.txt`
    itself and silently read nothing, so a file target must work, not be a config mistake. Never
    raises — a watcher tick must never take down the background thread over a transient path
    problem."""
    try:
        p = Path(resolve_path(folder))
        if p.is_file():
            return [p]
        if not p.is_dir():
            return []
        return [f for f in sorted(p.iterdir()) if f.is_file() and not f.name.startswith(".")]
    except OSError:
        return []


def scan_folder(con, folder, kind, exts, process_fn):
    """Scan one folder for files with an extension in `exts`; call process_fn(con, path, data) ->
    (status, note) for each new-or-changed file (per `watched_files`); record the result. Returns
    a summary dict. process_fn exceptions are caught per-file so one bad file doesn't stop the scan.

    The status a callback returns decides whether the file is ever looked at again. **`error` alone
    is retried** on every tick (see the NOTE below); every other status is terminal for that version
    of the file. So a callback must return `error` only when the failure might be OURS and a later
    fix could succeed ("couldn't tell which account this is for"), and a terminal status —
    `skipped`, `empty`, `duplicate` — when the verdict is a settled fact about the file's content
    ("this CSV has no date/description/amount columns, so it isn't a statement"). Getting that
    backwards means either a file that's never retried after a parser fix, or a permanent one-a-
    minute failure in the log."""
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
        key = watch_key(f)
        row = con.execute("SELECT mtime, size, status FROM watched_files WHERE path=?",
                          (key,)).fetchone()
        # Whole seconds, not exact float equality: the row may have been written by the OTHER
        # machine, whose filesystem stores mtime at a different resolution than this one's. A real
        # edit moves the mtime by far more than a second (and usually changes the size too).
        if (row and int(row["mtime"]) == int(mtime) and row["size"] == size
                and row["status"] != "error"):
            continue  # already processed this exact version of the file
        # NOTE the `status != "error"` above: a file that FAILED is retried on every tick even though
        # it hasn't changed, because the failure is often ours, not the file's — the trip log sat
        # unchanged with status='error' ("not a trip event") and so was skipped forever, even after
        # the parser was taught its format. Also covers transient failures (a file read mid-write).
        # Retrying a genuinely bad file each tick is cheap; silently never retrying is a trap.
        scanned += 1
        try:
            data = f.read_bytes()
            status, note = process_fn(con, f, data)
        except Exception as e:
            log.warning("watcher: processing %s failed: %s", f.name, e)
            status, note = "error", str(e)[:300]
            errors.append(f"{f.name}: {note}")
        con.execute(
            "INSERT INTO watched_files(path,kind,mtime,size,status,note,processed_at) "
            "VALUES(?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(path) DO UPDATE SET kind=?, mtime=?, size=?, status=?, note=?, processed_at=datetime('now')",
            (key, kind, mtime, size, status, note, kind, mtime, size, status, note))
        counts[status] = counts.get(status, 0) + 1
    return {"scanned": scanned, "counts": counts, "errors": errors, "enabled": True}


def run_once(con, statement_fn, receipt_fn, trip_fn=None, time_fn=None):
    """One tick: scan the configured folders (if set). The callbacks are
    (con, path, data) -> (status, note), supplied by the caller (app.py), so this module has no
    dependency on the import/ingestion pipelines themselves. `trip_fn` (Bluetooth mileage events —
    see trips.py) and `time_fn` (shop-log time CSVs — see shoplog.py) are optional so older
    callers/tests keep working unchanged."""
    global _LAST
    statements = scan_folder(con, db.get_setting(con, "statements_watch_folder", ""),
                             "statement", {".pdf", ".csv"}, statement_fn)
    receipts = scan_folder(con, db.get_setting(con, "receipts_watch_folder", ""),
                           "receipt", {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf"}, receipt_fn)
    summary = {"at": date.today().isoformat(), "statements": statements, "receipts": receipts}
    if trip_fn is not None:
        summary["trips"] = scan_folder(con, db.get_setting(con, "trips_watch_folder", ""),
                                       "trip", {".txt", ".csv"}, trip_fn)
    if time_fn is not None:
        summary["time"] = scan_folder(con, db.get_setting(con, "time_watch_folder", ""),
                                      "time", {".csv"}, time_fn)
    _LAST = summary
    return summary


def status():
    return _LAST


def _loop(statement_fn, receipt_fn, trip_fn, time_fn, interval):
    while not _stop.is_set():
        con = db.connect()
        try:
            run_once(con, statement_fn, receipt_fn, trip_fn, time_fn)
            con.commit()
        except Exception as e:
            log.warning("watcher tick failed: %s", e)  # a tick must never crash the background thread
        finally:
            con.close()
        _stop.wait(interval)


def start(statement_fn, receipt_fn, trip_fn=None, time_fn=None, interval=DEFAULT_INTERVAL):
    """Start the background thread (idempotent — a second call while one is running is a no-op)."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(statement_fn, receipt_fn, trip_fn, time_fn, interval),
                               daemon=True)
    _thread.start()


def stop(timeout=2):
    _stop.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=timeout)
