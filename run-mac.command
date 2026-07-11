#!/bin/bash
# ShopBooks launcher for macOS — double-click in Finder (or run ./run-mac.command).
# The Mac equivalent of run.bat: builds the venv on first run, then hands off to desktop.py,
# which frees the port, serves on the REAL default data location (no SHOPBOOKS_DATA_DIR, so
# cloud sync + backups are active), and opens the app-mode window (browser-tab fallback).
# Prefer the built dist/ShopBooks.app for a no-Terminal launch; this stays as the from-source path.
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"   # this script's folder = repo root
cd "$REPO"

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

echo "ShopBooks starting (close the app window to stop; Ctrl+C here also works)"
exec $ARCH .venv/bin/python desktop.py
