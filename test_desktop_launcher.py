"""Desktop launcher hardening: the helpers that clear a prior run's orphaned app window and detect a
browser hand-off are best-effort and never raise. subprocess is mocked — no real processes touched.
Isolation: SHOPBOOKS_DATA_DIR before importing desktop (which imports app -> the launch sequence)."""
import os
import subprocess
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_desktop_")

import desktop  # noqa: E402
from testutil import ok  # noqa: E402


class _R:
    def __init__(self, out="", rc=0):
        self.stdout = out
        self.returncode = rc


# the process filter targets OUR dedicated app profile (won't touch the user's normal browser)
ok(str(desktop.app_profile_dir()) in desktop._profile_windows_ps(),
   "the window filter references our dedicated app profile")

_orig = subprocess.run
try:
    # no app window: Windows count '0' / posix pgrep exit 1  -> False
    subprocess.run = lambda *a, **k: _R(out="0\n", rc=1)
    ok(desktop.app_window_open() is False, "no browser on our profile -> app_window_open() False")
    # an app window is open: count '3' / pgrep exit 0  -> True
    subprocess.run = lambda *a, **k: _R(out="3\n", rc=0)
    ok(desktop.app_window_open() is True, "a browser on our profile -> app_window_open() True")
    # a subprocess failure is swallowed (never blocks launch): open -> False, close -> no raise
    def _boom(*a, **k):
        raise RuntimeError("no shell here")
    subprocess.run = _boom
    ok(desktop.app_window_open() is False, "a subprocess error -> False (safe fallback)")
    desktop.close_orphan_window()
    ok(True, "close_orphan_window() swallows errors and never raises")
finally:
    subprocess.run = _orig

print("\nDESKTOP LAUNCHER TESTS DONE")
