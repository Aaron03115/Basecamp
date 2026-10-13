#!/usr/bin/env bash
# Installs everything Basecamp needs and starts it
#
# Requires first: python3 (3.9+) with the venv module.
#   Ubuntu/Debian: sudo apt install python3 python3-venv
set -euo pipefail

cd "$(dirname "$0")"
PORT="${PORT:-5000}"

# 1. Check that the tools we need are installed
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is not installed. On Ubuntu/Debian run: sudo apt install python3 python3-venv" >&2
  exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "ERROR: Python 3.9 or newer is required (found $(python3 --version 2>&1))." >&2
  exit 1
fi

if ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
  echo "ERROR: the Python venv module is missing. On Ubuntu/Debian run: sudo apt install python3-venv" >&2
  exit 1
fi

# 2. Install project dependencies into a virtual environment
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi
echo "Installing dependencies..."
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt

# 3. Create the database and sample data (idempotent; also runs on import)
echo "Setting up database..."
.venv/bin/python -c "import app"

# 4. Start the app
echo
echo "Basecamp is starting. Open http://localhost:${PORT} in your browser (Ctrl+C to stop)."
export PORT
exec .venv/bin/python app.py
