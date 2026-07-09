"""Backup skip-when-fresh, restore, and reset detection. Isolated via SHOPBOOKS_DATA_DIR."""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_restore_"))
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)
import db  # noqa: E402
import backup  # noqa: E402
import ledger  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
client = TestClient(appmod.app)  # triggers db.init + a startup snapshot attempt

# fresh DB: looks_fresh True, and the startup snapshot must have been SKIPPED
ok(backup.looks_fresh(db.DB_PATH), "brand-new seeded DB looks fresh")
ok(len(list((TMP / "backups").glob("books-*.db"))) == 0, "no snapshot taken for a fresh DB (protects good backups)")

# add real data -> no longer fresh -> snapshot now works
con = db.connect()
db.set_setting(con, "business_name", "Outlier Workshop")
db.set_setting(con, "anthropic_api_key", "sk-secret")
con.commit(); con.close()
ok(not backup.looks_fresh(db.DB_PATH), "DB with a real business name is not fresh")
snap = backup.snapshot()
ok(snap is not None and snap.exists(), "snapshot taken once the DB has data")
data_backup = snap.name

# simulate an accidental reset: wipe settings/data back to fresh
con = db.connect()
db.set_setting(con, "business_name", "My Business")
db.set_setting(con, "anthropic_api_key", "")
con.commit(); con.close()
ok(backup.looks_fresh(db.DB_PATH), "after reset the live DB looks fresh again")
ok(backup.reset_suspected(), "reset_suspected True: fresh live + a data backup exists")
# the reset banner shows on pages
ok("look empty" in client.get("/").text, "dashboard shows the reset warning banner")

# restore brings the data back, and stashes a pre-restore copy
r = client.post("/backup/restore", data={"name": data_backup}, follow_redirects=False)
ok(r.status_code == 303, "restore route returns a redirect")
con = db.connect()
bn = db.get_setting(con, "business_name")
key = db.get_setting(con, "anthropic_api_key")
con.close()
ok(bn == "Outlier Workshop" and key == "sk-secret", "restore brought back business name + API key")
ok(len(list((TMP / "backups").glob("pre-restore-*.db"))) >= 1, "a pre-restore undo copy was made")
ok(not backup.reset_suspected(), "after restore, no reset warning")

# the Save button route backs up and returns to the page it was clicked from
r = client.post("/backup/now", data={"back": "/reports"}, follow_redirects=False)
ok(r.status_code == 303 and r.headers["location"].startswith("/reports") and "saved=1" in r.headers["location"],
   "Save button backs up and returns to the originating page")

# path-traversal guard on restore
r = client.post("/backup/restore", data={"name": "../../evil.db"}, follow_redirects=False)
from urllib.parse import unquote  # noqa: E402
ok("could not be found" in unquote(r.headers.get("location", "")), "restore rejects a path-traversal name")

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nRESTORE TESTS DONE")
