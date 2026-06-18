@echo off
rem ShopBooks launcher. Frees the port first so you never end up viewing a stale
rem server (the "blank books" scare), then runs ONE clean server in this window.
rem Your real books live in %LOCALAPPDATA%\ShopBooks regardless of where this runs.
cd /d "%~dp0"
title ShopBooks server - keep this window open; close it to stop ShopBooks

rem Kill any leftover server still listening on 8765 (e.g. from a previous launch).
for /f "tokens=5" %%p in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8765 "') do (
  taskkill /f /pid %%p >nul 2>&1
)

rem Open the browser a few seconds from now (after the server is up), without blocking.
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8765"

echo Starting ShopBooks at http://127.0.0.1:8765
echo (Keep this window open while you work. Close it to stop the app.)
rem Run the server in THIS window so closing the window stops it.
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765
