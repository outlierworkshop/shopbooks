"""Desktop launcher basics: importing desktop is side-effect-free (beyond the app import), and its
helpers behave. No server or browser window is spawned here. Isolated via SHOPBOOKS_DATA_DIR."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_desktop_")
from testutil import ok  # prints PASS/FAIL and forces a nonzero exit if any check failed
import desktop  # noqa: E402  (importing must not start a server or open a window)

ok(desktop.PORT == 8765, "serves on the standard ShopBooks port")
ok(desktop.URL == "http://127.0.0.1:8765/", "URL is loopback-only (no external bind)")

b = desktop.find_chromium()
ok(b is None or os.path.exists(b), f"find_chromium returns an existing path or None (got {b!r})")

p = desktop.app_profile_dir()
ok("ShopBooks-app" in str(p), "app-mode browser profile is the dedicated ShopBooks-app dir")
ok(str(p) != str(os.environ["SHOPBOOKS_DATA_DIR"]), "browser profile is separate from the books")

ok(callable(desktop.free_port) and callable(desktop.wait_ready) and callable(desktop.main),
   "launcher entry points exist")

# wait_ready must return False quickly against a dead port. (Deliberately NOT the real :8765 —
# the owner's live server may be running on this machine while tests run.)
ok(desktop.wait_ready(timeout=1, url="http://127.0.0.1:9/") is False,
   "wait_ready times out cleanly when nothing answers")

# Windowed-exe regression (the v1.0.0 ShopBooks.exe launch crash): a PyInstaller console=False
# build (and pythonw) starts with sys.stdout/stderr = None; uvicorn's formatter calls
# sys.stdout.isatty() while Config configures logging -> "Unable to configure formatter 'default'".
# Simulate the None streams in a subprocess and prove that importing desktop (whose _shim_stdio
# runs at import) makes uvicorn.Config constructible.
import subprocess  # noqa: E402
import sys  # noqa: E402

_snippet = (
    "import sys, os, tempfile;"
    "os.environ['SHOPBOOKS_DATA_DIR'] = tempfile.mkdtemp(prefix='shopbooks_shimtest_');"
    "sys.stdout = None; sys.stderr = None;"           # what a windowed exe looks like
    "import desktop;"                                 # module-level _shim_stdio() must repair them
    "assert sys.stdout is not None and sys.stderr is not None, 'shim did not run';"
    "import uvicorn; from app import app;"
    "uvicorn.Config(app, host='127.0.0.1', port=18766, log_level='warning');"
    "sys.exit(0)"
)
r = subprocess.run([sys.executable, "-c", _snippet], capture_output=True, text=True,
                   cwd=os.path.dirname(os.path.abspath(__file__)), timeout=120)
ok(r.returncode == 0,
   f"windowed-exe stdio shim lets uvicorn.Config configure logging (rc={r.returncode}, err={r.stderr[-300:]!r})")

print("\nDESKTOP LAUNCHER TESTS DONE")
