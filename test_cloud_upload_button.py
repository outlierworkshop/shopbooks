"""The hover-revealed "Upload to cloud" button by the Save button: it renders only when cloud sync
is on, posts to /sync/now, and /sync/now returns to the page it was launched from. Isolated."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_cloudbtn_")

import db  # noqa: E402
db.init()
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from testutil import ok  # noqa: E402

client = TestClient(appmod.app)
con = db.connect()

# sync off -> no cloud button
db.set_setting(con, "sync_enabled", "0")
con.commit()
page = client.get("/").text
ok('class="cloud-upload"' not in page, "no Upload-to-cloud button when cloud sync is off")

# sync on -> the button appears, posts to /sync/now, carries the current page as back
db.set_setting(con, "sync_enabled", "1")
con.commit()
page = client.get("/").text
ok('class="cloud-upload"' in page and 'action="/sync/now"' in page,
   "Upload-to-cloud button appears (posting to /sync/now) when cloud sync is on")
ok('Upload to cloud' in page, "the button is labeled")

# /sync/now returns to the page it was launched from, not always /settings
r = client.post("/sync/now", data={"back": "/review"}, follow_redirects=False)
ok(r.status_code == 303 and r.headers["location"].startswith("/review"),
   "/sync/now redirects back to where it was launched (/review), not /settings")

con.close()
print("\nCLOUD UPLOAD BUTTON TESTS DONE")
