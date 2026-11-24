#!/usr/bin/env bash
# Mac / Linux launcher
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt
echo "Open http://127.0.0.1:8000 (Ctrl+C to stop)"
.venv/bin/python -m uvicorn main:app --reload
