"""Folder receipt-import + rematch test. Isolated via SHOPBOOKS_DATA_DIR; AI monkeypatched."""
import os
import tempfile
from pathlib import Path
from urllib.parse import unquote

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_rcpt_"))
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import ai  # noqa: E402
import db  # noqa: E402
import ledger  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ok = lambda c, w: print(("PASS" if c else "FAIL"), w)
loc = lambda r: unquote(r.headers["location"])
client = TestClient(appmod.app)

# AI on: vendor/date/total inferred from the saved file's name
ai.available = lambda con: True
def fake_receipt(con, path):
    p = path.lower()
    if "nomatch" in p:
        return {"vendor": "Mystery", "date": "2026-03-12", "total": 99.99}
    if "sub" in p:
        return {"vendor": "SubShop", "date": "2026-03-12", "total": 12.00}
    return {"vendor": "Depot", "date": "2026-03-12", "total": 50.00}
ai.extract_receipt = lambda con, path: fake_receipt(con, path)

# post an expense entry the $50 receipt should match (Materials & Supplies, within 7 days)
con = db.connect()
mats = con.execute("SELECT id FROM accounts WHERE name='Materials & Supplies'").fetchone()["id"]
card = con.execute("SELECT id FROM accounts WHERE name='Credit Card 1'").fetchone()["id"]
ledger.post_entry(con, "2026-03-10", "Home Depot", [(mats, 5000), (card, -5000)])
con.commit(); con.close()

# build a receipt folder: 2 top-level images, 1 ignored txt, 1 in a subfolder
folder = TMP / "receipts_src"
(folder / "sub").mkdir(parents=True)
(folder / "depot_match.jpg").write_bytes(b"img-a")
(folder / "store_nomatch.png").write_bytes(b"img-b")
(folder / "notes.txt").write_bytes(b"not a receipt")
(folder / "sub" / "lunch_sub.jpg").write_bytes(b"img-c")

# bad folder path
r = client.post("/receipts/import-folder", data={"folder": str(TMP / "nope")}, follow_redirects=False)
ok(r.status_code == 303 and "Folder not found" in loc(r), "missing folder -> clear error")

# top-level scan (no subfolders)
r = client.post("/receipts/import-folder", data={"folder": str(folder)}, follow_redirects=False)
ok("1 matched" in loc(r), "scan: the $50 receipt auto-matched the expense")
ok("1 imported" in loc(r), "scan: the $99.99 receipt imported but unmatched")
con = db.connect()
names = {Path(d["filename"]).name: d for d in con.execute("SELECT * FROM documents")}
ok("notes.txt" not in str(list(names)), "non-image (notes.txt) ignored")
ok("lunch_sub.jpg" not in names, "subfolder file skipped when recursive off")
depot = con.execute("SELECT * FROM documents WHERE filename='depot_match.jpg'").fetchone()
ok(depot["status"] == "matched" and depot["entry_id"], "matched receipt is linked to the entry")
con.close()

# re-run same folder -> all duplicates (sha256 dedupe), nothing new
r = client.post("/receipts/import-folder", data={"folder": str(folder)}, follow_redirects=False)
ok("2 already imported" in loc(r), "re-running the folder dedupes on content")

# recursive scan -> subfolder file now imported (others are dupes)
r = client.post("/receipts/import-folder", data={"folder": str(folder), "recursive": "1"}, follow_redirects=False)
con = db.connect()
ok(con.execute("SELECT 1 FROM documents WHERE filename='lunch_sub.jpg'").fetchone() is not None,
   "recursive scan picks up subfolder receipts")

# rematch: add the matching $99.99 expense, then re-check
nomatch = con.execute("SELECT * FROM documents WHERE filename='store_nomatch.png'").fetchone()
ok(nomatch["status"] == "unmatched", "store_nomatch still unmatched before its transaction exists")
ledger.post_entry(con, "2026-03-13", "Mystery Store", [(mats, 9999), (card, -9999)])
con.commit(); con.close()
r = client.post("/receipts/rematch", follow_redirects=False)
ok("1 newly matched" in loc(r), "rematch matches the receipt once its transaction exists")
con = db.connect()
n = con.execute("SELECT status FROM documents WHERE filename='store_nomatch.png'").fetchone()["status"]
con.close()
ok(n == "matched", "store_nomatch is now matched")

# nothing posted by receipt handling - entry count unchanged at 2
con = db.connect()
ne = con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
con.close()
ok(ne == 2, "receipt import/match never creates ledger entries (only the 2 we posted)")

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nRECEIPT-FOLDER TESTS DONE")
