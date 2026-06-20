"""Two-machine sync tests: version-guarded fast-forward / conflict / no-clobber.

Pattern (mandatory): point SHOPBOOKS_DATA_DIR at a temp dir BEFORE importing db,
so tests can never touch real books. We simulate two machines by swapping the
data dir (each machine has its own DATA + sidecar state) while sharing one cloud
folder, and we drive sync with an explicit cloud dir (cloud_dir() is None in test
mode by design).
"""
import os
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="shopbooks_synctest_")).resolve()
MAC = ROOT / "mac"
PC = ROOT / "pc"
CLOUD = ROOT / "cloud"
os.environ["SHOPBOOKS_DATA_DIR"] = str(MAC)  # some real dir before import

import db          # noqa: E402
import backup      # noqa: E402
import sync        # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)


def use(machine: Path):
    """Switch the 'current computer': repoint every db/backup path at this dir."""
    db.DATA = machine
    db.DOCS = machine / "docs"
    db.DB_PATH = machine / "books.db"
    db.BACKUPS = machine / "backups"
    machine.mkdir(parents=True, exist_ok=True)
    db.init()


def set_name(name):
    c = db.connect(); db.set_setting(c, "business_name", name); c.commit(); c.close()


def boot(cloud=CLOUD):
    """import_on_boot for the current machine against the shared cloud."""
    sync._LAST = None
    return sync.import_on_boot(cdir=cloud)


def close(cloud=CLOUD, force=False):
    return sync.export_on_close(cdir=cloud, force=force)


# Sync is gated on the user opting in; default-off must be a hard no-op. ----------
use(MAC)
ok(close()["status"] == "disabled", "export is a no-op while sync is disabled")
ok(boot()["status"] == "disabled", "import is a no-op while sync is disabled")

# enable on both machines (setting lives in each machine's DB)
c = db.connect(); db.set_setting(c, "sync_enabled", "1"); c.commit(); c.close()

# 1. First export from the Mac creates the cloud copy at version 1. ---------------
set_name("Mac Co")
r = close()
ok(r["status"] == "exported" and r["version"] == 1, "Mac first export -> cloud version 1")
ok((CLOUD / sync.SYNC_DB).exists() and (CLOUD / sync.SYNC_MANIFEST).exists(),
   "cloud copy + manifest written")
ok(close()["status"] == "unchanged", "re-export with no changes is 'unchanged'")

# 2. Fresh PC boots and fast-forwards the Mac's books (no clobber risk). -----------
use(PC)
c = db.connect(); db.set_setting(c, "sync_enabled", "1"); c.commit(); c.close()
p = boot()
ok(p["status"] == "fast_forward" and p.get("imported"), "fresh PC fast-forwards from cloud")
c = db.connect(); got = db.get_setting(c, "business_name"); c.close()
ok(got == "Mac Co", "PC now has the Mac's data after import")
ok(boot()["status"] == "up_to_date", "second PC boot has nothing to import")

# 3. Clean handoff: PC edits, exports v2; Mac pulls it. ---------------------------
set_name("PC Co")
ok(close()["version"] == 2, "PC export bumps to version 2")
use(MAC)
p = boot()
ok(p["status"] == "fast_forward", "Mac fast-forwards PC's v2")
c = db.connect(); ok(db.get_setting(c, "business_name") == "PC Co", "Mac pulled PC's change"); c.close()

# 4. Boot must NEVER import a cloud copy OLDER than local (stale/un-synced peer). --
#    Forge a manifest with a lower version than the Mac's current base.
import json  # noqa: E402
base_v = int(sync.load_state()["base_version"])
(CLOUD / sync.SYNC_MANIFEST).write_text(json.dumps(
    {"version": base_v - 1, "writer": "stale", "ts": "2020-01-01T00:00:00",
     "sha256": "deadbeef"}))
p = boot()
ok(p["status"] == "local_ahead", "older cloud copy is flagged local_ahead, not imported")
c = db.connect(); ok(db.get_setting(c, "business_name") == "PC Co", "stale cloud did NOT overwrite Mac"); c.close()
# restore a correct manifest for the conflict test
close(force=True)

# 5. CONFLICT: both machines edit from the same base; the later boot must NOT
#    silently lose the local edits. ----------------------------------------------
use(MAC); set_name("Mac edits A")        # Mac edits locally, does NOT export yet
use(PC)
p = boot(); ok(p["status"] in ("up_to_date", "fast_forward"), "PC starts from current cloud")
set_name("PC edits B"); close()          # PC exports its version, advancing the cloud
use(MAC)                                  # Mac still holds unexported "Mac edits A"
p = boot()
ok(p["status"] == "conflict", "divergent edits on both sides -> conflict")
c = db.connect(); ok(db.get_setting(c, "business_name") == "Mac edits A",
                     "conflict left the Mac's local edits intact (no silent clobber)"); c.close()

# 5a. Resolve by keeping local -> overwrites cloud, Mac's data wins.
sync.keep_local(cdir=CLOUD)
use(PC); p = boot()
ok(p["status"] == "fast_forward", "after 'keep local', PC pulls the resolved copy")
c = db.connect(); ok(db.get_setting(c, "business_name") == "Mac edits A",
                     "PC now has the kept (Mac) data"); c.close()

# 5b. Set up another conflict and resolve by taking cloud instead.
use(PC); set_name("PC local change")     # PC edits, unexported
use(MAC); set_name("Mac pushed"); close()  # Mac pushes, cloud advances
use(PC); ok(boot()["status"] == "conflict", "second conflict detected on PC")
sync.take_cloud(cdir=CLOUD)
c = db.connect(); ok(db.get_setting(c, "business_name") == "Mac pushed",
                     "'take cloud' discarded PC's local edit for the cloud copy"); c.close()
ok(boot()["status"] == "up_to_date", "after 'take cloud', PC is up to date")

# 6. A pre-sync restore point is stashed before any import overwrites data. -------
pre = list((PC / "backups").glob("pre-sync-*.db"))
ok(len(pre) >= 1, "imports leave a pre-sync backup to undo from")

# 7. Errors in sync never propagate (must not block app open/close). --------------
ok(sync.import_on_boot(cdir=Path("/nonexistent/\0bad"))["status"] in ("error", "no_cloud", "up_to_date", "fast_forward", "conflict", "local_ahead", "local_changes"),
   "import_on_boot returns a status dict instead of raising")

# ---- Hardening (sync-hardening branch) -------------------------------------------
import shutil  # noqa: E402
import sqlite3  # noqa: E402

def pull(cloud=CLOUD):
    sync._LAST = None
    return sync.pull(cdir=cloud, attempts=1, delay=0)

# 8. Manual "Pull from cloud now" imports on demand (no restart). ------------------
# Reset to a clean two-machine state: Mac is source of truth.
for d in (MAC, PC, CLOUD):
    shutil.rmtree(d, ignore_errors=True)
use(MAC)
c = db.connect(); db.set_setting(c, "sync_enabled", "1"); c.commit(); c.close()
set_name("Mac Co"); close()                       # Mac exports v1
use(PC)
c = db.connect(); db.set_setting(c, "sync_enabled", "1"); c.commit(); c.close()
r = pull()
ok(r["status"] == "fast_forward" and r.get("imported"), "pull() imports the cloud copy on demand")
c = db.connect(); ok(db.get_setting(c, "business_name") == "Mac Co", "pull() brought the Mac's data to the PC"); c.close()
ok(pull()["status"] == "up_to_date", "pull() when already current is a no-op")

# 9. Machine-local settings (backup_dir) are NOT synced between machines. ----------
use(MAC)
c = db.connect(); db.set_setting(c, "backup_dir", "/Users/mac/Dropbox/SB"); c.commit(); c.close()
set_name("Mac Co v2"); close()                    # Mac exports v2 with its own backup_dir
use(PC)
c = db.connect(); db.set_setting(c, "backup_dir", "C:/Users/pc/Dropbox/SB"); c.commit(); c.close()
pull()                                            # PC pulls v2
c = db.connect()
ok(db.get_setting(c, "business_name") == "Mac Co v2", "books data did sync across")
ok(db.get_setting(c, "backup_dir") == "C:/Users/pc/Dropbox/SB",
   "PC kept its OWN backup_dir after the import (machine-local setting preserved)")
c.close()

# 10. backup_dir differences don't cause spurious version bumps. -------------------
#     PC made no book changes, only differs by backup_dir -> closing must NOT push.
ok(close()["status"] == "unchanged",
   "identical books with a different backup_dir hash the same -> no spurious export")

# 11. A not-yet-downloaded cloud copy (placeholder) -> cloud_unavailable, no clobber.
use(MAC); set_name("Mac Co v3"); close()          # cloud now has v3 (real)
(CLOUD / sync.SYNC_DB).write_bytes(b"not a sqlite database yet")  # simulate Dropbox placeholder
use(PC)
before = (db.connect().execute("SELECT value FROM settings WHERE key='business_name'").fetchone()[0])
r = pull()
ok(r["status"] == "cloud_unavailable", "unreadable/placeholder cloud copy -> cloud_unavailable")
after = (db.connect().execute("SELECT value FROM settings WHERE key='business_name'").fetchone()[0])
ok(before == after, "local books NOT clobbered when the cloud copy isn't a valid DB")
ok(sync.last_alert() and "downloading" in sync.last_alert()["message"],
   "cloud_unavailable surfaces a helpful banner (not a silent failure)")

# 12. _readable_db distinguishes a real DB from a placeholder. ----------------------
ok(sync._readable_db(MAC / "books.db") is True, "_readable_db True for a real DB")
ok(sync._readable_db(CLOUD / sync.SYNC_DB) is False, "_readable_db False for a placeholder file")

# 13. Receipt FILES sync between machines (docs-sync). -----------------------------
for d in (MAC, PC, CLOUD):
    shutil.rmtree(d, ignore_errors=True)
use(MAC)
c = db.connect(); db.set_setting(c, "sync_enabled", "1"); c.commit(); c.close()
set_name("Docs Co")
# add a receipt: a file in docs/ + a document row pointing at it (Mac-local path)
(MAC / "docs").mkdir(parents=True, exist_ok=True)
(MAC / "docs" / "rcpt_99.jpg").write_bytes(b"JPEGDATA")
c = db.connect()
c.execute("INSERT INTO documents(filename, path, vendor) VALUES('rcpt_99.jpg', ?, 'Test')",
          (str(MAC / "docs" / "rcpt_99.jpg"),))
c.commit(); c.close()
close()                                            # exports DB + pushes receipt files
ok((CLOUD / sync.SYNC_DOCS / "rcpt_99.jpg").exists(), "export pushes receipt files to the cloud")

use(PC)
c = db.connect(); db.set_setting(c, "sync_enabled", "1"); c.commit(); c.close()
pull()                                             # imports DB + pulls receipt files
ok((PC / "docs" / "rcpt_99.jpg").exists(), "pull brings the receipt file to the other machine")
c = db.connect()
p = c.execute("SELECT path FROM documents WHERE filename='rcpt_99.jpg'").fetchone()["path"]
c.close()
ok(p == str(PC / "docs" / "rcpt_99.jpg"), "imported receipt path repointed to THIS machine's docs")

# 14. Backfill: a missing receipt is restored even when the DB is already up to date.
os.remove(PC / "docs" / "rcpt_99.jpg")
pull()
ok((PC / "docs" / "rcpt_99.jpg").exists(), "pull backfills a missing receipt file when DB is up to date")

import shutil  # noqa: E402
shutil.rmtree(ROOT, ignore_errors=True)
print("\nSYNC TESTS DONE")
