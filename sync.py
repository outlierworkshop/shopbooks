"""Two-machine sync via a single version-stamped DB copy in the cloud folder.

The danger with using one set of books on two computers is silent data loss: a
live SQLite file in Dropbox/OneDrive can corrupt mid-write, and a naive
"import on boot" can clobber newer work if it fires before the cloud has synced
or after a dirty shutdown. This module avoids both by syncing a *static* copy
guarded by a monotonic version counter, with git-style fast-forward semantics:

  - The synced copy is `_sync.db` in the cloud folder (see backup.cloud_dir()),
    described by `_sync.json`  {version, writer, ts, sha256}.  The leading "_"
    keeps it out of backup.list_restorable().
  - Each machine keeps a sidecar `sync_state.json` in its data dir recording the
    version + content hash it last synced to (`base_version`, `base_sha`), plus a
    stable `machine_id`.  This is NOT stored in the DB, so version bookkeeping
    never perturbs the content hash.
  - "Dirty" = the live DB's content hash differs from `base_sha` (local edits not
    yet pushed).  Content hash is over `iterdump()`, so it reflects data, not
    incidental page layout.

Boot (import_on_boot): compare the cloud version `cv` to our `base_version` bv.
  cv  > bv, not dirty  -> FAST-FORWARD: import the cloud copy.
  cv  > bv, dirty      -> CONFLICT: both sides advanced; leave local untouched,
                          surface to the user (take cloud / keep local).
  cv <= bv             -> nothing to import (we're current, or cloud is stale).
Identical content short-circuits to "up to date" regardless of version.

Close (export_on_close): if the live DB changed, write `_sync.db` + bump the
version.  If the cloud is newer than our base (the other machine pushed while we
worked) we *block* instead of clobbering — that conflict is resolved on next boot.

Nothing here raises into the app: import/export wrap errors and return a status
dict, so a sync hiccup can never stop ShopBooks from opening or closing.
"""
import hashlib
import json
import shutil
import socket
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

import backup
import db

SYNC_DB = "_sync.db"
SYNC_MANIFEST = "_sync.json"
SYNC_DOCS = "_sync_docs"          # cloud subfolder mirroring the receipts (docs/)
STATE_FILE = "sync_state.json"

# Settings that are specific to THIS machine and must never travel between computers:
# backup_dir is a filesystem path (and differs by OS — a Windows path is meaningless on a
# Mac, and writing to it creates junk folders / breaks sync), and each machine controls its
# own sync participation. These are preserved across an import and excluded from the content
# hash, so identical books hash the same on every machine regardless of local config.
LOCAL_SETTINGS = ("backup_dir", "sync_enabled")

# Result of the most recent import_on_boot(), for the UI banner (no recompute per page).
_LAST = None


# --- enable / location -------------------------------------------------------

def enabled(con=None):
    """True if the user has turned cloud sync on (Settings)."""
    own = con is None
    if own:
        con = db.connect()
    try:
        return db.get_setting(con, "sync_enabled", "0") == "1"
    finally:
        if own:
            con.close()


def cloud():
    """The cloud folder the synced copy lives in (configured backup folder or
    auto-detected OneDrive). None in test/isolation mode — callers pass an
    explicit dir there."""
    return backup.cloud_dir()


# --- local sidecar state -----------------------------------------------------

def _state_path():
    return db.DATA / STATE_FILE


def load_state():
    p = _state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def save_state(state):
    db.DATA.mkdir(parents=True, exist_ok=True)
    _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")


def machine_id():
    """Stable per-machine id, generated once and kept in the sidecar."""
    st = load_state()
    mid = st.get("machine_id")
    if not mid:
        mid = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        st["machine_id"] = mid
        save_state(st)
    return mid


# --- content hashing & manifest ---------------------------------------------

def content_hash(path=None):
    """SHA-256 over the DB's logical content, with machine-local settings neutralized so the
    same books hash the same on every machine (regardless of backup_dir / sync_enabled or
    file-level page churn). None if the DB is absent or unreadable."""
    path = Path(path) if path else db.DB_PATH
    if not path.exists():
        return None
    src = mem = None
    try:
        src = sqlite3.connect(str(path))
        mem = sqlite3.connect(":memory:")           # copy so we never modify the real file
        src.backup(mem)
        mem.execute("UPDATE settings SET value='' WHERE key IN (%s)"
                    % ",".join("?" * len(LOCAL_SETTINGS)), LOCAL_SETTINGS)
        h = hashlib.sha256()
        for line in mem.iterdump():
            h.update(line.encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()
    except sqlite3.Error:
        return None
    finally:
        if src:
            src.close()
        if mem:
            mem.close()


def read_manifest(cdir=None):
    cdir = cdir or cloud()
    if not cdir:
        return None
    p = Path(cdir) / SYNC_MANIFEST
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- the decision ------------------------------------------------------------

def plan(cdir=None):
    """Decide what boot should do. Returns a dict with a `status`:
      no_cloud | up_to_date | local_changes | local_ahead | fast_forward | conflict
    plus version/dirty details for the UI. Pure (no side effects)."""
    cdir = cdir or cloud()
    state = load_state()
    base_v = int(state.get("base_version", 0))
    base_sha = state.get("base_sha")
    local_sha = content_hash()
    if base_sha is None:
        # never synced: a fresh/seeded DB is safe to replace; real data is not.
        dirty = not backup.looks_fresh()
    else:
        dirty = local_sha != base_sha

    info = {"base_version": base_v, "dirty": dirty, "local_sha": local_sha}
    man = read_manifest(cdir)
    if not cdir or man is None:
        info["status"] = "no_cloud"
        return info

    cv = int(man.get("version", 0))
    info.update(cloud_version=cv, cloud_sha=man.get("sha256"),
                writer=man.get("writer"), when=man.get("ts"))

    if local_sha is not None and man.get("sha256") == local_sha:
        info["status"] = "up_to_date"          # identical content, version aside
    elif cv < base_v:
        info["status"] = "local_ahead"         # cloud is behind us; don't import
    elif cv == base_v:
        info["status"] = "local_changes" if dirty else "up_to_date"
    elif dirty:
        info["status"] = "conflict"            # both advanced -> manual resolve
    else:
        info["status"] = "fast_forward"        # cloud advanced, we didn't
    return info


# --- applying changes --------------------------------------------------------

def _readable_db(path):
    """True if `path` is a materialized, valid SQLite DB with our schema — not a cloud
    placeholder (Dropbox/iCloud online-only file whose bytes aren't on disk yet)."""
    path = Path(path)
    if not path.exists():
        return False
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            c.execute("SELECT COUNT(*) FROM accounts").fetchone()
            return True
        finally:
            c.close()
    except sqlite3.Error:
        return False


def _wait_readable(path, attempts=1, delay=1.5):
    """Wait for a cloud file to actually download. Opening it nudges the cloud provider to
    materialize it; retry a few times before giving up. Returns True once it's a valid DB."""
    path = Path(path)
    for i in range(max(1, attempts)):
        if _readable_db(path):
            return True
        try:  # touch the bytes so Dropbox/iCloud begin fetching the file
            with open(path, "rb") as f:
                f.read(1 << 16)
        except OSError:
            pass
        if i < attempts - 1:
            time.sleep(delay)
    return _readable_db(path)


def _mirror_files(src_dir, dst_dir):
    """Copy files in src_dir that are missing from dst_dir. Receipts are immutable and uniquely
    named, so this is a safe additive merge by filename (each machine ends up with the union).
    Best-effort: skips dotfiles and anything unreadable (e.g. a not-yet-downloaded cloud file).
    Returns the number copied."""
    src_dir, dst_dir = Path(src_dir), Path(dst_dir)
    if not src_dir.exists():
        return 0
    dst_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in src_dir.iterdir():
        if not f.is_file() or f.name.startswith("."):
            continue
        dest = dst_dir / f.name
        if dest.exists():
            continue
        try:
            shutil.copy2(str(f), str(dest))
            n += 1
        except OSError:
            pass  # e.g. cloud placeholder not downloaded yet; it'll copy next time
    return n


def _push_docs(cdir):
    """Upload this machine's receipt files to the cloud (additive)."""
    return _mirror_files(db.DOCS, Path(cdir) / SYNC_DOCS)


def _pull_docs(cdir):
    """Download receipt files from the cloud into this machine's docs (additive)."""
    return _mirror_files(Path(cdir) / SYNC_DOCS, db.DOCS)


def _repoint_doc_paths(con):
    """After importing another machine's DB, the document rows carry that machine's file paths.
    Keep each file's basename but point it at THIS machine's docs folder, so /doc resolves and
    matches the files mirrored by _pull_docs."""
    for r in con.execute("SELECT id, path FROM documents").fetchall():
        con.execute("UPDATE documents SET path=? WHERE id=?", (str(db.DOCS / Path(r["path"]).name), r["id"]))


def _apply_import(src):
    """Overwrite the live DB with `src` via the SQLite backup API, after stashing a pre-sync
    restore point. Preserves this machine's local settings (backup_dir, sync_enabled) so a
    pull never imports another computer's paths/config. Transactional; safe with the app running.
    Raises if `src` isn't a readable DB (e.g. a not-yet-downloaded cloud placeholder)."""
    src = Path(src)
    if not _readable_db(src):
        raise OSError(f"source is not a readable database (still downloading?): {src}")
    _pull_docs(src.parent)            # bring the other machine's receipt files down first
    con = db.connect()
    try:  # remember machine-local settings to put back after the overwrite
        keep = {k: db.get_setting(con, k, "") for k in LOCAL_SETTINGS}
    finally:
        con.close()
    db.BACKUPS.mkdir(parents=True, exist_ok=True)
    if db.DB_PATH.exists():
        backup._consistent_copy(db.BACKUPS / f"pre-sync-{datetime.now():%Y%m%d-%H%M%S}.db")
    source = sqlite3.connect(str(src))
    dest = sqlite3.connect(str(db.DB_PATH))
    try:
        with dest:
            source.backup(dest)
    finally:
        source.close()
        dest.close()
    con = db.connect()
    try:  # restore this machine's settings, and repoint receipt paths at this machine's docs
        for k, v in keep.items():
            db.set_setting(con, k, v)
        _repoint_doc_paths(con)
        con.commit()
    finally:
        con.close()


def _adopt(version, sha):
    st = load_state()
    st["base_version"] = int(version)
    st["base_sha"] = sha
    save_state(st)


def _import(cdir, attempts, delay):
    """Shared fast-forward import. Pulls the cloud copy only when it's strictly newer and we
    have no local changes; never clobbers local edits (a conflict is left for the user). If the
    cloud file hasn't downloaded yet, returns status 'cloud_unavailable' instead of failing
    silently. Caches the result in _LAST for the banner. Never raises."""
    global _LAST
    try:
        if not enabled():
            _LAST = {"status": "disabled"}
            return _LAST
        cdir = cdir or cloud()
        p = plan(cdir)
        s = p["status"]
        if s == "fast_forward":
            src = Path(cdir) / SYNC_DB
            if not _wait_readable(src, attempts, delay):
                p["status"] = "cloud_unavailable"   # placeholder not downloaded yet
                _LAST = p
                return p
            _apply_import(src)
            _adopt(p["cloud_version"], content_hash())
            p["imported"] = True
        elif s == "up_to_date" and p.get("cloud_version", 0) > p["base_version"]:
            # same content, higher version number: adopt it so we stop re-checking.
            _adopt(p["cloud_version"], p.get("local_sha"))
        # local_ahead / local_changes / conflict / no_cloud: leave the DB alone.
        if cdir:  # backfill receipt files even when we didn't import the DB (additive, idempotent)
            _pull_docs(cdir)
        _LAST = p
        return p
    except Exception as e:
        _LAST = {"status": "error", "error": str(e)}
        return _LAST


def import_on_boot(cdir=None):
    """Run at startup. Brief wait for the cloud file (it may still be downloading), so launch
    isn't blocked but a freshly-synced copy is usually caught."""
    return _import(cdir, attempts=3, delay=1.5)


def pull(cdir=None, attempts=8, delay=1.5):
    """Manual 'Pull from cloud now' — same as boot import but waits longer for the cloud file
    to download, so the user can retry without restarting the app."""
    return _import(cdir, attempts=attempts, delay=delay)


def export_on_close(cdir=None, force=False):
    """Run at shutdown (or 'Sync now'). Pushes the live DB to the cloud copy and
    bumps the version, unless the cloud is newer than our base — then it blocks
    rather than overwrite the other machine's work (resolved on next boot, or with
    force=True via 'keep local'). Never raises."""
    try:
        if not enabled():
            return {"status": "disabled"}
        cdir = cdir or cloud()
        if not cdir:
            return {"status": "no_cloud"}
        if backup.looks_fresh():
            return {"status": "skipped_fresh"}
        cdir = Path(cdir)
        cdir.mkdir(parents=True, exist_ok=True)
        _push_docs(cdir)               # upload receipt files (additive) even if the DB is unchanged
        local_sha = content_hash()
        man = read_manifest(cdir)
        base_v = int(load_state().get("base_version", 0))
        cloud_v = int(man.get("version", 0)) if man else 0

        if man and man.get("sha256") == local_sha:
            _adopt(max(base_v, cloud_v), local_sha)    # cloud already has our content
            return {"status": "unchanged", "version": max(base_v, cloud_v)}
        if man and cloud_v > base_v and not force:
            return {"status": "blocked_cloud_newer",
                    "cloud_version": cloud_v, "base_version": base_v}

        version = max(base_v, cloud_v) + 1
        backup._consistent_copy(cdir / SYNC_DB)
        manifest = {"version": version, "writer": machine_id(),
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "sha256": local_sha}
        (cdir / SYNC_MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _adopt(version, local_sha)
        return {"status": "exported", "version": version}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# --- explicit conflict resolution -------------------------------------------

def take_cloud(cdir=None):
    """Resolve a conflict by discarding local changes and taking the cloud copy."""
    cdir = cdir or cloud()
    man = read_manifest(cdir)
    if not (cdir and man):
        return {"status": "no_cloud"}
    _apply_import(Path(cdir) / SYNC_DB)
    _adopt(int(man["version"]), content_hash())
    return {"status": "took_cloud", "version": int(man["version"])}


def keep_local(cdir=None):
    """Resolve a conflict by overwriting the cloud copy with local changes."""
    return export_on_close(cdir=cdir, force=True)


# --- UI helpers --------------------------------------------------------------

_ALERTS = {
    "conflict": ("error", "Sync conflict: this computer and the cloud copy both have "
                 "changes. Go to Settings → Sync to choose which to keep."),
    "local_ahead": ("error", "The cloud sync copy is OLDER than your books here — the "
                    "other computer may not have synced. Not importing. See Settings → Sync."),
    "cloud_unavailable": ("error", "Newer books are waiting in the cloud, but the file hasn't "
                          "finished downloading yet. Open your sync folder in Finder to force the "
                          "download, then use Settings → Sync → Pull from cloud now."),
    "error": ("error", "Cloud sync hit a problem reading the cloud copy. Try Settings → Sync → "
              "Pull from cloud now."),
}


def last_alert():
    """A {level, message} banner for the last boot result, or None."""
    if not _LAST:
        return None
    a = _ALERTS.get(_LAST.get("status"))
    return {"level": a[0], "message": a[1]} if a else None


def status(cdir=None):
    """Full picture for the Settings → Sync panel."""
    cdir = cdir or cloud()
    p = plan(cdir)
    p["enabled"] = enabled()
    p["machine_id"] = machine_id()
    p["cloud_dir"] = str(cdir) if cdir else None
    return p
