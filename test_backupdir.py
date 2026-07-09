"""Configurable backup folder test. Fully isolated via SHOPBOOKS_DATA_DIR."""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_bdir_"))
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db  # noqa: E402
import backup  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed

db.init()
con = db.connect()

# In isolation mode cloud_dir is suppressed regardless of setting (protects real folders in tests)
db.set_setting(con, "backup_dir", str(TMP / "chosen"))
con.commit()
ok(backup.cloud_dir() is None, "cloud_dir suppressed in test/isolation mode")

# Exercise the resolution logic directly by temporarily clearing the env flag.
saved = os.environ.pop("SHOPBOOKS_DATA_DIR")
try:
    chosen = TMP / "chosen"
    ok(backup.cloud_dir() == chosen, "configured backup_dir is used when set")
    ok(backup.cloud_source() == "configured", "source reports 'configured'")
    ok(backup.check_writable(chosen) and chosen.exists(), "check_writable creates+writes the folder")

    db.set_setting(con, "backup_dir", "")
    con.commit()
    # with no setting, falls back to OneDrive auto-detect (or none); just assert it doesn't crash
    src = backup.cloud_source()
    ok(src in ("onedrive", "none"), f"blank setting falls back to auto-detect ({src})")
finally:
    os.environ["SHOPBOOKS_DATA_DIR"] = saved

# status() shape
db.set_setting(con, "backup_dir", str(TMP / "chosen"))
con.commit()
con.close()
st = backup.status()
for k in ("data_dir", "local_count", "cloud_dir", "cloud_source", "cloud_count", "cloud_writable", "configured"):
    ok(k in st, f"status() includes '{k}'")

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nBACKUP-DIR TESTS DONE")
