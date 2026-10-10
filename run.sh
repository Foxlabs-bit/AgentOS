#!/usr/bin/env bash
cd "$(dirname "$0")"
source .venv/bin/activate
( sleep 2; (open "http://127.0.0.1:8000" || xdg-open "http://127.0.0.1:8000") >/dev/null 2>&1 ) &
exec python -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
