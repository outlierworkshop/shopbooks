#!/usr/bin/env python3
"""Run every test_*.py in its own process and exit nonzero if anything failed.

Why a runner and not bare pytest: the suite is script-style (each file is a self-contained
scenario that must set SHOPBOOKS_DATA_DIR *before importing db/app*). Running each file as a
subprocess preserves that isolation exactly as written.

What counts as a failure for a test file:
  - a nonzero exit code (asserts/exceptions, or testutil.ok's forced exit), or
  - a FAIL line in its output (belt-and-suspenders for any future file that prints FAIL
    without going through testutil).

The runner also ENFORCES the repo's #1 safety rule: a test file that never mentions
SHOPBOOKS_DATA_DIR is refused outright — such a file would read (and possibly destroy)
the user's real books. See CLAUDE.md.

Usage: python run_tests.py [substring ...]   # optional filters on file names
"""
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TIMEOUT = 300  # seconds per file
FAIL_LINE = re.compile(r"^FAIL\b", re.MULTILINE)


def main(filters):
    files = sorted(ROOT.glob("test_*.py"))
    if filters:
        files = [f for f in files if any(s in f.name for s in filters)]
    if not files:
        print("no test files matched", file=sys.stderr)
        return 2

    failed, t0 = [], time.time()
    for f in files:
        if "SHOPBOOKS_DATA_DIR" not in f.read_text(encoding="utf-8", errors="replace"):
            print(f"REFUSED {f.name}: does not set SHOPBOOKS_DATA_DIR (would touch real books)")
            failed.append(f.name)
            continue
        t = time.time()
        try:
            r = subprocess.run([sys.executable, str(f)], cwd=ROOT, timeout=TIMEOUT,
                               capture_output=True, text=True)
            out = (r.stdout or "") + (r.stderr or "")
            bad = r.returncode != 0 or FAIL_LINE.search(out)
        except subprocess.TimeoutExpired as e:
            out = ((e.stdout or b"").decode(errors="replace") +
                   (e.stderr or b"").decode(errors="replace") + f"\nTIMEOUT after {TIMEOUT}s")
            bad = True
        dur = time.time() - t
        print(f"{'FAIL' if bad else 'ok':4} {f.name:42} {dur:5.1f}s")
        if bad:
            failed.append(f.name)
            print("      " + "\n      ".join(out.strip().splitlines()[-15:]))  # last lines of output

    total = time.time() - t0
    print(f"\n{len(files) - len(failed)}/{len(files)} files passed in {total:.0f}s")
    if failed:
        print("FAILED: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
