@echo off
rem ShopBooks launcher. Clears any stale server on the port first (so you never view a
rem leftover/duplicate instance), then runs ONE clean server in this window.
rem Close this window to stop ShopBooks.
cd /d "%~dp0"
title ShopBooks - close this window to stop

rem Pin the data location to a fixed folder OUTSIDE %AppData% so it is read the same way no
rem matter how the app is launched. When the shortcut is opened from inside the Claude desktop
rem app, that app's MSIX sandbox silently redirects %LOCALAPPDATA% into a per-package cache, so
rem the old default (%LOCALAPPDATA%\ShopBooks) resolved to a DIFFERENT, empty database and the
rem books looked blank. %USERPROFILE%\ShopBooks is never redirected, so this is stable. Your
rem books live in %USERPROFILE%\ShopBooks (books.db + docs\ + backups\).
set "SHOPBOOKS_DATA_DIR=%USERPROFILE%\ShopBooks"

for /f "tokens=5" %%p in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8765 "') do taskkill /f /pid %%p >nul 2>&1

start "" http://127.0.0.1:8765
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765
