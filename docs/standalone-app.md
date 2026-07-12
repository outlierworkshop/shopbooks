# Standalone App (app-mode window) — implementation guide

**Status: SHIPPED on macOS (2026-07-11)** — `desktop.py` implements the launcher below verbatim;
`run-mac.command` now execs it; and `build-mac.sh` + `shopbooks.spec` go one step further than this
doc scoped (it had deferred the "bundled installer" problem): a PyInstaller-built, code-signed
`dist/ShopBooks.app` with its own bundled Python 3.13, ad-hoc signed by default and taking a
`IDENTITY`/`NOTARIZE=1` env for Developer ID + notarization later.

**Windows: SHIPPED (2026-07-12)** — same `desktop.py` entry point (its Windows branches for the
Edge/Chrome path, `%LOCALAPPDATA%` profile, and `netstat`/`taskkill` port-free were already
written). `shopbooks.spec` is now platform-conditional (onedir `.exe` on Windows), and
`installer.iss` (Inno Setup) wraps it into `dist/ShopBooks-Setup.exe` — a per-user, no-admin
double-click installer with Start-Menu + Desktop shortcuts and an uninstaller. Built in CI
(`.github/workflows/build-windows.yml` on `windows-latest`, since PyInstaller/Inno can't
cross-compile from macOS). Unsigned for now (SmartScreen "More info → Run anyway"); the signing
step is left as a documented hook for when a code-signing cert exists. The original design follows.

## Goal
Make ShopBooks feel like a **standalone desktop app** rather than a browser tab: its own chromeless
window (no tabs / address bar), a taskbar/dock icon, and **no visible server console** — while keeping
the app exactly as-is (local FastAPI/uvicorn) with **no new dependencies and no build step** (per the
CLAUDE.md "boring tech, no build step" ethos).

## Chosen approach: app-mode browser window
Launch Edge/Chrome with `--app=<url>`, which opens a dedicated chromeless window that adopts the site
favicon as its icon. A single stdlib launcher owns the lifecycle so the server is hidden and stops when
the window closes. (Rejected alternatives: pywebview native window — one dependency; and a full
PyInstaller/Electron installer — adds a build step. Revisit only if requirements change.)

## Implementation

### `desktop.py` (new, stdlib + the existing `uvicorn`)
1. `from app import app` — importing already runs the startup side-effects (`db.init()`,
   `sync.import_on_boot()`, `backup.snapshot()`), same as uvicorn loading it.
2. **Free port 8765** first, reusing the current per-OS idiom (Windows `netstat`+`taskkill`; macOS
   `lsof`+kill — see `run.bat` line ~10 and `run-mac.command` line ~26).
3. **Serve in-process, gracefully stoppable:** `cfg = uvicorn.Config(app, host="127.0.0.1",
   port=8765, log_level="warning")`; `server = uvicorn.Server(cfg)`;
   `server.install_signal_handlers = lambda: None` (so it can run off the main thread); start
   `server.run` in a **daemon thread**.
4. **Wait until ready:** poll `http://127.0.0.1:8765/` with `urllib.request` for a few seconds.
5. **Open the app window (blocking):** find a Chromium browser (`shutil.which` + common install paths
   for `msedge`/`chrome` on Windows; `/Applications/Google Chrome.app/...` and
   `/Applications/Microsoft Edge.app/...` on macOS) and run:
   `<browser> --app=http://127.0.0.1:8765 --user-data-dir=<app-profile> --no-first-run
   --no-default-browser-check`. Use a dedicated `--user-data-dir`
   (`%LOCALAPPDATA%\ShopBooks-app` / `~/Library/Application Support/ShopBooks-app`) so it's its own
   window/taskbar entry with **its own persistent localStorage** (theme, column widths, sort order
   stick; isolated from normal browsing).
6. **On window close** (the blocking call returns): set `server.should_exit = True` and join — a
   **graceful** uvicorn shutdown, so `@app.on_event("shutdown")` → `sync.export_on_close()` still
   pushes to the cloud. (A hard `terminate()` on Windows would skip that; in-process `should_exit`
   avoids the problem.)
7. **Fallback:** if no Chromium browser is found, `webbrowser.open(url)` (a normal tab) and keep
   serving until Ctrl-C — today's behavior, so a Safari-only Mac still works.

### Launchers / shortcut (no console, real icon)
- **Windows:** retarget the desktop **`ShopBooks.lnk`** to `<repo>\.venv\Scripts\pythonw.exe` with
  argument `desktop.py`, "Start in" = repo, Icon = `static\favicon.ico` (update via a `WScript.Shell`
  script, the same technique that created the shortcut originally). `pythonw.exe` = **no console**.
  Keep `run.bat` as the terminal fallback. Optional `run-app.vbs` one-liner for a double-clickable
  no-console entry point.
- **macOS:** update `run-mac.command` to exec `desktop.py`. For a true Dock app without Terminal, add a
  minimal **`ShopBooks.app`** bundle (`Contents/Info.plist` + `Contents/MacOS/ShopBooks` shell script
  running `.venv/bin/python desktop.py` + `Contents/Resources/ShopBooks.icns`). A `.app` is just a
  folder — no build step.
- **Icon:** `static/favicon.ico` already exists (green `$`, via `make_icon.py`); Edge/Chrome `--app`
  auto-adopts it for the window & taskbar. Make an `.icns` from `static/logo.png` for the `.app` icon.

### Files
- New: `desktop.py`, `ShopBooks.app/` (macOS bundle).
- Edit: `run.bat` (note the shortcut now uses `desktop.py`), `run-mac.command` (exec `desktop.py`),
  `README.md` / `CLAUDE.md` (document standalone launch + Chromium-browser requirement), `docs/ROADMAP.md`
  (move this out of "Next up" into the changelog when shipped).
- Local (not a repo file): retarget the desktop `ShopBooks.lnk`.

## Verification
- Launch via the retargeted shortcut / `pythonw.exe desktop.py`: a **chromeless window** opens, **no
  console** appears, taskbar shows the ShopBooks icon.
- Server answers while open; **closing the window frees port 8765** (graceful stop) and runs a sync
  export (version bump in Settings → Sync).
- Relaunch frees any stale server and starts one clean instance.
- Fallback: hide Edge & Chrome → opens a normal tab and keeps serving.
- Persistence: change theme / resize a column, reopen → settings persist (dedicated profile).
- macOS: double-click `ShopBooks.app` → window opens without a lingering Terminal.
- Existing test suite still passes (importing `app` in `desktop.py` changes nothing).

## Trade-offs
- Requires a Chromium browser for the chromeless window (Windows Edge is built-in; macOS needs
  Chrome/Edge — Safari has no `--app` mode → graceful tab fallback).
- Not a bundled installer: Python/venv are still required, exactly as today.
