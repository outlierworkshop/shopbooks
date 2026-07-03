"""Tests for folder watchers (watcher.py + the _watch_statement/_watch_receipt callbacks in app.py).

Isolated via SHOPBOOKS_DATA_DIR. No real background thread is exercised for the processing-logic
tests (watcher.run_once is called directly, synchronously) — only a short lifecycle smoke test
starts/stops the real thread. AI is unavailable (no key), matching the deterministic-fallback paths.
"""
import os
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shopbooks_watchtest_")).resolve()
WATCH = TMP / "watch"
STMT_DIR, RCPT_DIR = WATCH / "statements", WATCH / "receipts"
STMT_DIR.mkdir(parents=True)
RCPT_DIR.mkdir(parents=True)
os.environ["SHOPBOOKS_DATA_DIR"] = str(TMP)

import db        # noqa: E402
import watcher   # noqa: E402

ok = lambda cond, what: print(("PASS" if cond else "FAIL"), what)

db.init()
con = db.connect()

# ---------------------------------------------------------------- scan_folder (generic engine)

log = []
def stub_process(con, path, data):
    log.append(path.name)
    return "imported", f"{len(data)} bytes"

# blank folder setting -> disabled, no-op
r = watcher.scan_folder(con, "", "x", {".txt"}, stub_process)
ok(r == {"scanned": 0, "counts": {}, "errors": [], "enabled": False}, "blank folder is reported disabled, not an error")

scratch = TMP / "scratch"
scratch.mkdir()
(scratch / "a.txt").write_bytes(b"hello")
(scratch / "b.csv").write_bytes(b"nope")          # wrong extension for this scan
(scratch / ".hidden.txt").write_bytes(b"skip me")  # dotfile

r1 = watcher.scan_folder(con, str(scratch), "x", {".txt"}, stub_process)
con.commit()
ok(r1["enabled"] and r1["scanned"] == 1 and log == ["a.txt"],
   "only the matching, non-hidden file is processed")
ok(con.execute("SELECT COUNT(*) c FROM watched_files").fetchone()["c"] == 1, "one watched_files row recorded")

log.clear()
r2 = watcher.scan_folder(con, str(scratch), "x", {".txt"}, stub_process)
con.commit()
ok(r2["scanned"] == 0 and log == [], "an unchanged file is NOT reprocessed on the next scan")

time.sleep(0.05)
(scratch / "a.txt").write_bytes(b"hello world, now longer")  # size (and mtime) change
r3 = watcher.scan_folder(con, str(scratch), "x", {".txt"}, stub_process)
con.commit()
ok(r3["scanned"] == 1 and log == ["a.txt"], "a changed file (different size) IS reprocessed")

r4 = watcher.scan_folder(con, str(TMP / "does-not-exist"), "x", {".txt"}, stub_process)
ok(r4 == {"scanned": 0, "counts": {}, "errors": [], "enabled": True},
   "a missing folder scans cleanly (0 found), never raises")

def boom(con, path, data):
    raise RuntimeError("nope")
(TMP / "err").mkdir()
(TMP / "err" / "bad.txt").write_bytes(b"x")
r5 = watcher.scan_folder(con, str(TMP / "err"), "x", {".txt"}, boom)
con.commit()
ok(r5["counts"].get("error") == 1 and len(r5["errors"]) == 1,
   "a process_fn exception is caught and recorded as an 'error', not raised")

# ---------------------------------------------------------------- run_once (reads settings)

r = watcher.run_once(con, stub_process, stub_process)
con.commit()
ok(r["statements"]["enabled"] is False and r["receipts"]["enabled"] is False,
   "run_once: both watchers report disabled when the settings are blank")
ok(watcher.status() == r, "status() returns the last run_once summary")

db.set_setting(con, "statements_watch_folder", str(STMT_DIR))
db.set_setting(con, "receipts_watch_folder", str(RCPT_DIR))
con.commit()

# ---------------------------------------------------------------- _watch_statement (real pipeline)

from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402
c = TestClient(app.app)

CSV = (b"Date,Description,Amount\n"
      b"2026-03-01,SPINDLE SUPPLY CO,-42.50\n"
      b"2026-03-02,SQUARE DEPOSIT,150.00\n")
(STMT_DIR / "Business_Checking_Statement.csv").write_bytes(CSV)

before_batches = con.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"]
rr = watcher.run_once(con, app._watch_statement, app._watch_receipt)
con.commit()
ok(rr["statements"]["enabled"] and rr["statements"]["scanned"] == 1, "the dropped CSV was scanned")
ok(rr["statements"]["counts"].get("imported") == 1, "the CSV was staged (status='imported')")
after_batches = con.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"]
ok(after_batches == before_batches + 1, "exactly one new batch was created")
staged = con.execute("SELECT date, description, amount_cents, status FROM staged ORDER BY id DESC LIMIT 2").fetchall()
descs = {s["description"] for s in staged}
ok(descs == {"SPINDLE SUPPLY CO", "SQUARE DEPOSIT"}, "both CSV rows landed as staged transactions")
ok(all(s["status"] == "pending" for s in staged), "staged rows are pending, not posted (Review still confirms)")
acct_name = con.execute(
    "SELECT a.name FROM batches b JOIN accounts a ON a.id=b.account_id ORDER BY b.id DESC LIMIT 1"
).fetchone()["name"]
ok(acct_name == "Business Checking", "the account was correctly auto-detected from the filename")

# re-scanning the SAME unchanged file is a no-op (path/mtime/size dedupe)
rr2 = watcher.run_once(con, app._watch_statement, app._watch_receipt)
con.commit()
ok(rr2["statements"]["scanned"] == 0, "the same statement file is not reprocessed on the next tick")

# a DIFFERENT file with the same content -> caught by is_duplicate_statement, not double-staged
(STMT_DIR / "Business_Checking_Statement_v2.csv").write_bytes(CSV)
staged_before = con.execute("SELECT COUNT(*) c FROM staged").fetchone()["c"]
rr3 = watcher.run_once(con, app._watch_statement, app._watch_receipt)
con.commit()
ok(rr3["statements"]["counts"].get("duplicate") == 1, "a near-identical statement under a new filename is flagged duplicate")
staged_after = con.execute("SELECT COUNT(*) c FROM staged").fetchone()["c"]
ok(staged_after == staged_before, "the duplicate did not add any new staged rows")

# ---------------------------------------------------------------- _watch_receipt (real pipeline)

(RCPT_DIR / "receipt1.jpg").write_bytes(b"\xff\xd8\xff\xe0fake jpeg bytes for a receipt")
docs_before = con.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
rr4 = watcher.run_once(con, app._watch_statement, app._watch_receipt)
con.commit()
ok(rr4["receipts"]["enabled"] and rr4["receipts"]["scanned"] == 1, "the dropped receipt image was scanned")
docs_after = con.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
ok(docs_after == docs_before + 1, "a new document row was created for the receipt")
doc = con.execute("SELECT filename, path FROM documents ORDER BY id DESC LIMIT 1").fetchone()
ok(doc["filename"] == "receipt1.jpg" and Path(doc["path"]).exists(), "the receipt file was saved into the docs folder")

# re-dropping the exact same bytes under a new name -> content-hash duplicate (via _ingest_receipt)
(RCPT_DIR / "receipt1_copy.jpg").write_bytes(b"\xff\xd8\xff\xe0fake jpeg bytes for a receipt")
rr5 = watcher.run_once(con, app._watch_statement, app._watch_receipt)
con.commit()
ok(rr5["receipts"]["counts"].get("duplicate") == 1, "identical receipt bytes under a new filename are flagged duplicate")

con.close()

# ---------------------------------------------------------------- HTTP surface

page = c.get("/settings")
ok(page.status_code == 200 and "Folder watchers" in page.text, "settings page shows the Folder watchers section")
ok('name="statements_watch_folder"' in page.text and 'name="receipts_watch_folder"' in page.text,
   "both folder path inputs are present")

r = c.post("/watch/scan-now", follow_redirects=False)
ok(r.status_code == 303 and "msg=" in r.headers["location"], "POST /watch/scan-now redirects with a summary")

r2 = c.post("/settings", data={"statements_watch_folder": str(STMT_DIR), "receipts_watch_folder": ""},
           follow_redirects=False)
ok(r2.status_code == 303, "saving the watch-folder settings via the main form redirects")
con = db.connect()
ok(db.get_setting(con, "statements_watch_folder", "") == str(STMT_DIR), "the statements folder setting was saved")
con.close()

# ---------------------------------------------------------------- thread lifecycle smoke test

def noop(con, path, data):
    return "imported", ""
watcher.start(noop, noop, interval=1)
ok(watcher._thread is not None and watcher._thread.is_alive(), "start() launches a live background thread")
watcher.start(noop, noop, interval=1)  # idempotent
watcher.stop()
ok(not watcher._thread.is_alive(), "stop() cleanly joins the background thread")

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nWATCHER TESTS DONE")
