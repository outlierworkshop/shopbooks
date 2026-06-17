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
import socket
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import backup
import db

SYNC_DB = "_sync.db"
SYNC_MANIFEST = "_sync.json"
STATE_FILE = "sync_state.json"

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
    """SHA-256 over the DB's logical content (iterdump), so the same data hashes
    the same regardless of file-level page churn. None if the DB is absent."""
    path = Path(path) if path else db.DB_PATH
    if not path.exists():
        return None
    con = sqlite3.connect(str(path))
    try:
        h = hashlib.sha256()
        for line in con.iterdump():
            h.update(line.encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()
    except sqlite3.Error:
        return None
    finally:
        con.close()


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

def _apply_import(src):
    """Overwrite the live DB with `src` via the SQLite backup API, after stashing
    a pre-sync restore point. Transactional, safe with the app running."""
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(src)
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


def _adopt(version, sha):
    st = load_state()
    st["base_version"] = int(version)
    st["base_sha"] = sha
    save_state(st)


def import_on_boot(cdir=None):
    """Run at startup. Fast-forwards from the cloud when it's safe; never clobbers
    local changes (a conflict is left for the user). Returns a plan dict; also
    cached in _LAST for the UI banner. Never raises."""
    global _LAST
    try:
        if not enabled():
            _LAST = {"status": "disabled"}
            return _LAST
        cdir = cdir or cloud()
        p = plan(cdir)
        s = p["status"]
        if s == "fast_forward":
            _apply_import(Path(cdir) / SYNC_DB)
            _adopt(p["cloud_version"], content_hash())
            p["imported"] = True
        elif s == "up_to_date" and p.get("cloud_version", 0) > p["base_version"]:
            # same content, higher version number: adopt it so we stop re-checking.
            _adopt(p["cloud_version"], p.get("local_sha"))
        # local_ahead / local_changes / conflict / no_cloud: leave the DB alone.
        _LAST = p
        return p
    except Exception as e:  # startup must never be blocked by a sync error
        _LAST = {"status": "error", "error": str(e)}
        return _LAST


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
