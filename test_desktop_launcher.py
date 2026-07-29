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

# --- no console flash: every helper subprocess goes through _run, which suppresses the console
# window on the Windows GUI build (this was the "terminal keeps flashing" bug). ------------------
ok(desktop._NO_WINDOW == (0x08000000 if os.name == "nt" else 0),
   "_NO_WINDOW is CREATE_NO_WINDOW on Windows, 0 elsewhere")

_calls = []
try:
    subprocess.run = lambda *a, **k: (_calls.append((a, k)), _R(out="0\n", rc=1))[1]
    _calls.clear()
    desktop._run(["whoami"], capture_output=True)
    _kw = _calls[-1][1]
    if os.name == "nt":
        ok(_kw.get("creationflags") == desktop._NO_WINDOW,
           "_run sets creationflags=CREATE_NO_WINDOW on Windows (no console flash)")
    else:
        ok("creationflags" not in _kw or _kw.get("creationflags") == 0,
           "_run adds no Windows-only flags off Windows")
    ok(_kw.get("capture_output") is True, "_run forwards the caller's kwargs")

    # the flash-prone helpers actually route through _run (would carry creationflags on Windows)
    _calls.clear()
    desktop.app_window_open()
    ok(len(_calls) == 1 and (os.name != "nt" or _calls[0][1].get("creationflags") == desktop._NO_WINDOW),
       "app_window_open() shells out via _run (no flash)")
finally:
    subprocess.run = _orig

# --- hand-off timing guard: main() only polls (app_window_open every 2s) when open_app_window
# returned near-instantly. A real, now-closed session must not spin on Edge's lingering profile
# processes. We assert the knob exists and is a short, sane threshold. -------------------------
ok(isinstance(desktop._HANDOFF_SECONDS, (int, float)) and 0 < desktop._HANDOFF_SECONDS <= 10,
   "a short hand-off threshold gates the keep-serving poll")

print("\nDESKTOP LAUNCHER TESTS DONE")
