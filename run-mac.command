#!/bin/bash
# ShopBooks launcher for macOS — double-click in Finder (or run ./run-mac.command).
# The Mac equivalent of run.bat: builds the venv on first run, frees the port, starts the
# server on the REAL default data location (no SHOPBOOKS_DATA_DIR, so cloud sync + backups
# are active), and opens the browser. Lives in the repo, so it works wherever you clone it.
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"   # this script's folder = repo root
cd "$REPO"
PORT=8765
URL="http://127.0.0.1:${PORT}/"

# On Apple Silicon, run Python as arm64 (matches the native wheels). hw.optional.arm64 is 1 on
# Apple Silicon even under a Rosetta terminal; left empty on Intel so it still works there.
ARCH=""
if sysctl -n hw.optional.arm64 2>/dev/null | grep -q 1; then ARCH="arch -arm64"; fi

# First run: create the virtual environment and install dependencies (kept arch-consistent).
if [ ! -x ".venv/bin/python" ]; then
  echo "First run — setting up the Python environment (this takes a minute)…"
  $ARCH python3 -m venv .venv
  $ARCH .venv/bin/python -m pip install --upgrade pip >/dev/null
  $ARCH .venv/bin/python -m pip install -r requirements.txt
fi

# Always serve one clean instance: free the port if something is already bound to it.
lsof -ti:"$PORT" | xargs kill 2>/dev/null || true
sleep 1

# Open the browser once the server is actually answering.
( for _ in $(seq 1 40); do curl -s -o /dev/null "$URL" && break; sleep 0.5; done; open "$URL" ) &

echo "ShopBooks running at $URL  (press Ctrl+C in this window to stop)"
exec $ARCH .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port "$PORT"
