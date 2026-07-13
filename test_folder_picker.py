"""Server-side folder browser (GET /settings/browse-folder) backing the Settings folder pickers
(statement/receipt watchers, extra backup folder). Browsers never expose real filesystem paths from
<input type="file">, so the server lists directories instead. Isolated via SHOPBOOKS_DATA_DIR."""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_folderpicker_")).resolve()
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

BROWSE_ROOT = TMP / "browse_me"
(BROWSE_ROOT / "Alpha").mkdir(parents=True)
(BROWSE_ROOT / "beta").mkdir()
(BROWSE_ROOT / ".hidden").mkdir()
(BROWSE_ROOT / "not_a_folder.txt").write_text("just a file")

import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
client = TestClient(appmod.app)

# ---- listing a real folder: only subdirectories, case-insensitive, hidden skipped ----
r = client.get("/settings/browse-folder", params={"path": str(BROWSE_ROOT)})
ok(r.status_code == 200, "browse-folder renders 200")
data = r.json()
ok(data["path"] == str(BROWSE_ROOT), "reports back the resolved path")
ok(data["parent"] == str(TMP), "reports the parent path")
names = [d["name"] for d in data["dirs"]]
ok(names == ["Alpha", "beta"], f"lists only subfolders, sorted case-insensitively (got {names})")
ok(".hidden" not in names, "hidden (dot) folders are skipped")
ok(not any(d["name"] == "not_a_folder.txt" for d in data["dirs"]), "plain files are never listed as folders")
ok(all(Path(d["path"]).is_dir() for d in data["dirs"]), "every returned path really is a directory")

# ---- navigating into a child reports the parent correctly (the "Up" button) ----
r2 = client.get("/settings/browse-folder", params={"path": str(BROWSE_ROOT / "Alpha")})
ok(r2.json()["parent"] == str(BROWSE_ROOT), "child listing's parent points back at BROWSE_ROOT")

# ---- a bogus/nonexistent path never 500s; falls back to home ----
r3 = client.get("/settings/browse-folder", params={"path": "/definitely/not/a/real/path/xyz"})
ok(r3.status_code == 200, "a nonexistent path falls back gracefully (no 500)")
ok(r3.json()["path"] == str(Path.home().resolve()), "falls back to the home directory")

# ---- blank path defaults to home ----
r4 = client.get("/settings/browse-folder", params={"path": ""})
ok(r4.status_code == 200 and r4.json()["path"] == str(Path.home().resolve()),
   "blank path defaults to the home directory")

# ---- settings page renders the picker markup and Browse buttons ----
r5 = client.get("/settings")
ok(r5.status_code == 200, "settings page renders")
ok('id="folderPickerModal"' in r5.text, "settings page includes the folder-picker modal")
ok(r5.text.count("folder-picker-btn") == 4,
   "all four folder fields (statements, receipts, trips, backup) get a Browse button")

# ---- the Receipts "import a whole folder" field uses the same picker (via shared include) ----
r6 = client.get("/receipts")
ok(r6.status_code == 200, "receipts page renders")
ok('id="folderPickerModal"' in r6.text, "receipts page includes the shared folder-picker modal")
ok(r6.text.count("folder-picker-btn") == 1, "the receipts import-folder field gets a Browse button")
ok("openFolderPicker('receipt_import_folder')" in r6.text,
   "the receipts Browse button targets the import-folder input")

print("\nFOLDER PICKER TESTS DONE")
