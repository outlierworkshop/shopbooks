"""ShopBooks desktop launcher: serve locally, open an app-mode window, stop when it closes.

Implements docs/standalone-app.md: the app gets its own chromeless window (Chrome/Edge `--app=`
mode) with a dock/taskbar presence, the server runs hidden in-process, and closing the window
shuts the server down GRACEFULLY — so the `shutdown` event still runs `sync.export_on_close()`.
Falls back to a normal browser tab (and Ctrl-C to stop) when no Chromium browser is installed.

Stdlib + the existing uvicorn only; also the entry point for the bundled ShopBooks.app
(see build-mac.sh / shopbooks.spec). Importing this module has no side effects beyond importing
`app` (which runs the launch sequence, same as uvicorn loading it).
"""
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def _shim_stdio():
    """A PyInstaller windowed build (console=False on Windows) — and pythonw.exe — start with
    sys.stdout/sys.stderr as None. uvicorn's log formatter calls sys.stdout.isatty() while
    uvicorn.Config configures logging, so the bundled ShopBooks.exe crashed on launch with
    "Unable to configure formatter 'default'". Point any missing stream at devnull BEFORE
    importing uvicorn/app (logutil also attaches a console handler at import time). macOS app
    bundles keep real streams, which is why only the Windows build hit this."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


_shim_stdio()

import uvicorn  # noqa: E402  (must come after the stdio shim — see _shim_stdio)

from app import app  # noqa: E402  runs db.init() -> sync fast-forward -> backup snapshot, same as uvicorn

PORT = 8765
URL = f"http://127.0.0.1:{PORT}/"

_CHROMIUM_MAC = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
_CHROMIUM_WIN = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_chromium():
    """Path to a Chromium-family browser for `--app=` mode, or None (-> tab fallback)."""
    import shutil
    if sys.platform == "darwin":
        candidates = _CHROMIUM_MAC
    elif os.name == "nt":
        candidates = _CHROMIUM_WIN
    else:
        candidates = []
    for p in candidates:
        if Path(p).exists():
            return p
    for name in ("google-chrome", "chromium", "chromium-browser", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def app_profile_dir():
    """Dedicated browser profile so the app window is its own dock/taskbar entry with its own
    persistent localStorage (theme, column widths) — isolated from normal browsing."""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ShopBooks-app"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ShopBooks-app"
    return Path.home() / ".local" / "share" / "ShopBooks-app"


def already_serving(url=URL):
    """True if a healthy ShopBooks is already answering on the port. Launching again must NOT
    kill it: free_port() on a second double-click killed the first instance's server and left
    its app window orphaned on ERR_CONNECTION_REFUSED. Reuse the live server instead."""
    try:
        with urllib.request.urlopen(url, timeout=1.5) as r:
            return b"ShopBooks" in r.read(8192)
    except Exception:
        return False


def open_app_window(browser):
    """Open the chromeless app-mode window; blocks until the window's browser process exits."""
    profile = app_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.run([browser, f"--app={URL}", f"--user-data-dir={profile}",
                    "--no-first-run", "--no-default-browser-check"])


def _profile_windows_ps():
    """PowerShell 'where' clause selecting browser processes whose command line references OUR
    dedicated app profile (matches the main window and its renderer/gpu children)."""
    return ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
            f"$_.CommandLine -like '*{app_profile_dir()}*' }}")


def close_orphan_window():
    """Kill any leftover browser process still holding OUR app profile. A window from a previous run
    whose server has since died can linger; a fresh `--app --user-data-dir=<profile>` launch would
    then just hand off to that dead window and return immediately, so open_app_window() wouldn't
    block and the server we just started would shut down at once (the "app won't start / starts then
    exits" bug). Best-effort and platform-specific — safe to fail (the hand-off guard in main() is
    the backstop). Only call this when starting a fresh server: never when reusing a live one."""
    profile = str(app_profile_dir())
    try:
        if os.name == "nt":
            ps = _profile_windows_ps() + " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=15)
        else:
            subprocess.run(["pkill", "-f", profile], capture_output=True, timeout=10)
        time.sleep(0.4)
    except Exception:
        pass


def app_window_open():
    """True if a browser process is currently using our app profile (i.e. an app window is on
    screen). Lets us tell a hand-off — the window is still open under another browser instance — from
    a real window close, so we don't tear the server down while the app is still up. Best-effort;
    any error returns False (fall through to a normal shutdown)."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"({_profile_windows_ps()} | Measure-Object).Count"],
                capture_output=True, text=True, timeout=15).stdout.strip()
            return out.isdigit() and int(out) > 0
        return subprocess.run(["pgrep", "-f", str(app_profile_dir())],
                              capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def free_port(port=PORT):
    """Always serve one clean instance: kill whatever already holds the port (stale server)."""
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True).stdout
            pids = {line.split()[-1] for line in out.splitlines()
                    if f":{port}" in line and "LISTENING" in line}
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        else:
            subprocess.run(f"lsof -ti:{port} | xargs kill 2>/dev/null", shell=True, capture_output=True)
        time.sleep(0.5)
    except Exception:
        pass  # a bound port will surface as a bind error with its own message


def wait_ready(timeout=20, url=URL):
    """Poll until the server answers (it's starting on a daemon thread)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def main():
    if already_serving():
        # A healthy ShopBooks (another launch of this app, or the dev server) already owns the
        # port. Open a window onto it and leave its lifecycle alone — the instance that started
        # the server stops it when ITS window closes.
        browser = find_chromium()
        if browser:
            open_app_window(browser)
        else:
            webbrowser.open(URL)
        return

    free_port()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(cfg)
    server.install_signal_handlers = lambda: None  # required to run off the main thread
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    if not wait_ready():
        print("ShopBooks server did not start (is another copy running?)", file=sys.stderr)
        sys.exit(1)

    browser = find_chromium()
    if browser:
        # Clear a previous run's orphaned app window first so the one we open is ours to block on.
        close_orphan_window()
        # Blocks until the app window (its whole dedicated profile) closes...
        open_app_window(browser)
        # ...unless the browser still handed off to another instance and returned immediately. If an
        # app window is in fact still open, keep serving (polling until it closes) instead of exiting
        # the moment we launched — otherwise the server would die and the window show a dead app.
        while t.is_alive() and app_window_open():
            try:
                time.sleep(2)
            except KeyboardInterrupt:
                break
    else:
        # No Chromium browser: normal tab, keep serving until Ctrl-C (today's behavior).
        webbrowser.open(URL)
        print(f"ShopBooks running at {URL}  (no Chromium browser found for app mode; "
              f"press Ctrl+C to stop)")
        try:
            while t.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

    # Graceful stop: the shutdown event runs (watcher stops, sync exports), then we exit.
    server.should_exit = True
    t.join(timeout=15)


if __name__ == "__main__":
    main()
