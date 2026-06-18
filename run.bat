@echo off
rem ShopBooks launcher. Clears any stale server on the port first (so you never view a
rem leftover/duplicate instance), then runs ONE clean server in this window.
rem Close this window to stop ShopBooks. Your books live in %LOCALAPPDATA%\ShopBooks.
cd /d "%~dp0"
title ShopBooks - close this window to stop

for /f "tokens=5" %%p in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8765 "') do taskkill /f /pid %%p >nul 2>&1

start "" http://127.0.0.1:8765
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765
