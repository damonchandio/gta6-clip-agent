#!/usr/bin/env bash
# GTA 6 Clip Agent setup for macOS and Linux. Needs Python 3.10+ and ffmpeg (brew install ffmpeg / apt install ffmpeg).
set -e
cd "$(dirname "$0")"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "Python 3.10 or newer is needed."; exit 1; }
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
.venv/bin/python -m gta6 --selftest
echo "All set. Run ./start.sh to open the GTA 6 Clip Agent."
