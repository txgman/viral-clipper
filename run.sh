#!/usr/bin/env bash
# One-click start for macOS / Linux. Usage: ./run.sh   (extra args go to the app, e.g. --port 9000)
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "FFmpeg is required. Install it first:"
  echo "  macOS:  brew install ffmpeg"
  echo "  Ubuntu: sudo apt install ffmpeg"
  exit 1
fi
if ! command -v deno >/dev/null 2>&1 && ! command -v node >/dev/null 2>&1; then
  echo "Note: YouTube downloads need Deno or Node.js (macOS: brew install deno). Uploads and other sites work without it."
fi

PY=${PYTHON:-python3}
if [ ! -d .venv ]; then
  echo "Creating virtual environment…"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "Installing dependencies (first run takes a few minutes)…"
  python -m pip install --upgrade pip -q
  python -m pip install -r requirements.txt -q
  touch .venv/.installed
fi
exec python -m app "$@"
