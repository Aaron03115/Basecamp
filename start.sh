#!/usr/bin/env bash
# Installs Basecamp's dependencies, sets up its PostgreSQL database, and starts the app.
# Works on a fresh clone on Linux (Ubuntu/Debian). Safe to run again.
#
# Prerequisite a person installs first: python3 (3.9+) with the venv module
#   sudo apt install python3 python3-venv
#
# On a local machine this script also:
#   - installs PostgreSQL with apt if it is missing (uses sudo; you may be asked for your password)
#   - starts the PostgreSQL server if it is not running
#   - creates the database role, the app database, and a test database (<name>_test)
#
# Point the app at a different database with DATABASE_URL:
#   DATABASE_URL=postgresql://user:pass@host:5432/dbname ./start.sh
# Change the port with:  PORT=8080 ./start.sh
set -euo pipefail

cd "$(dirname "$0")"
PORT="${PORT:-5000}"
export DATABASE_URL="${DATABASE_URL:-postgresql://basecamp:basecamp@localhost:5432/basecamp}"

die() { echo "ERROR: $*" >&2; exit 1; }

# Run a command as root (directly if we are root, otherwise through sudo).
as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    return 127
  fi
}

# Run a command as the "postgres" system user, from a directory it can read.
as_postgres() {
  ( cd / && {
      if [ "$(id -u)" -eq 0 ] && command -v runuser >/dev/null 2>&1; then
        runuser -u postgres -- "$@"
      elif command -v sudo >/dev/null 2>&1; then
        sudo -u postgres "$@"
      else
        return 127
      fi
    } )
}

# ---------------------------------------------------------------------------
# 1. Check the tools the app needs are installed; stop with a clear message if not
# ---------------------------------------------------------------------------
command -v python3 >/dev/null 2>&1 \
  || die "python3 is not installed. On Ubuntu/Debian run: sudo apt install python3 python3-venv"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python 3.9 or newer is required (found $(python3 --version 2>&1))."

python3 -c 'import venv, ensurepip' >/dev/null 2>&1 \
  || die "the Python venv module is missing. On Ubuntu/Debian run: sudo apt install python3-venv"

# Pull host, port, user, password and database name out of DATABASE_URL.
DB_PARTS="$(python3 - <<'PY'
import os, urllib.parse as u
p = u.urlparse(os.environ["DATABASE_URL"])
for v in (p.hostname or "localhost", p.port or 5432, u.unquote(p.username or ""),
          u.unquote(p.password or ""), (p.path or "/").lstrip("/")):
    print(v)
PY
)"
PG_HOST="$(echo "$DB_PARTS" | sed -n 1p)"
PG_PORT="$(echo "$DB_PARTS" | sed -n 2p)"
DB_USER="$(echo "$DB_PARTS" | sed -n 3p)"
DB_PASS="$(echo "$DB_PARTS" | sed -n 4p)"
DB_NAME="$(echo "$DB_PARTS" | sed -n 5p)"
TEST_DB_NAME="${DB_NAME}_test"

is_local_host() { case "$PG_HOST" in localhost|127.0.0.1|::1) return 0 ;; *) return 1 ;; esac; }
pg_installed() { command -v psql >/dev/null 2>&1 && command -v pg_isready >/dev/null 2>&1 \
                 && ls /usr/lib/postgresql/*/bin/postgres >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# 2. PostgreSQL: install it if needed, start it, and create the role + databases
# ---------------------------------------------------------------------------
if is_local_host; then
  if ! pg_installed; then
    command -v apt-get >/dev/null 2>&1 \
      || die "PostgreSQL is not installed and this is not an apt-based system. Install PostgreSQL, then run ./start.sh again."
    echo "Installing PostgreSQL (needs sudo)..."
    as_root apt-get update -y >/dev/null \
      && as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql postgresql-contrib libpq-dev >/dev/null \
      || die "could not install PostgreSQL automatically. Run: sudo apt install postgresql postgresql-contrib libpq-dev   then run ./start.sh again."
  fi

  if ! pg_isready -q -h "$PG_HOST" -p "$PG_PORT"; then
    echo "Starting PostgreSQL..."
    as_root systemctl start postgresql >/dev/null 2>&1 \
      || as_root service postgresql start >/dev/null 2>&1 \
      || { command -v pg_lsclusters >/dev/null 2>&1 \
           && pg_lsclusters -h | while read -r ver name _; do as_root pg_ctlcluster "$ver" "$name" start || true; done; } \
      || true
    for _ in $(seq 1 20); do
      pg_isready -q -h "$PG_HOST" -p "$PG_PORT" && break
      sleep 1
    done
  fi
  pg_isready -q -h "$PG_HOST" -p "$PG_PORT" \
    || die "PostgreSQL is not running at ${PG_HOST}:${PG_PORT}. Try: sudo systemctl start postgresql   (or: sudo service postgresql start)"

  # Create the role and databases if they do not exist yet (idempotent).
  if ! psql "$DATABASE_URL" -Atqc 'SELECT 1' >/dev/null 2>&1; then
    echo "Setting up database role '${DB_USER}' and database '${DB_NAME}'..."
    as_postgres psql -v ON_ERROR_STOP=1 -q \
      -v dbuser="$DB_USER" -v dbpass="$DB_PASS" -v dbname="$DB_NAME" -v testdb="$TEST_DB_NAME" >/dev/null <<'SQL' \
      || die "could not create the database role/database. Create them yourself, or set DATABASE_URL to a database you can already use."
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'dbuser', :'dbpass')
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'dbuser') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'dbname', :'dbuser')
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'dbname') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'testdb', :'dbuser')
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'testdb') \gexec
SQL
  else
    # App database already works; make sure the test database exists too (best effort).
    as_postgres psql -q -v dbuser="$DB_USER" -v testdb="$TEST_DB_NAME" >/dev/null 2>&1 <<'SQL' || true
SELECT format('CREATE DATABASE %I OWNER %I', :'testdb', :'dbuser')
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'testdb') \gexec
SQL
  fi
else
  # Remote database: we cannot install or create anything there, so just check it.
  command -v pg_isready >/dev/null 2>&1 \
    || die "the PostgreSQL client tools are missing. On Ubuntu/Debian run: sudo apt install postgresql-client"
  pg_isready -q -h "$PG_HOST" -p "$PG_PORT" \
    || die "PostgreSQL is not reachable at ${PG_HOST}:${PG_PORT}."
fi

psql "$DATABASE_URL" -Atqc 'SELECT 1' >/dev/null 2>&1 \
  || die "could not log in to PostgreSQL with DATABASE_URL (user '${DB_USER}', database '${DB_NAME}'). Check the password and that the database exists."

# ---------------------------------------------------------------------------
# 3. Install project dependencies into a virtual environment
# ---------------------------------------------------------------------------
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi
echo "Installing dependencies..."
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt

# ---------------------------------------------------------------------------
# 4. Create the schema and sample data (idempotent; runs on import of app.py)
# ---------------------------------------------------------------------------
echo "Setting up database tables at ${PG_HOST}:${PG_PORT} ..."
ERR_FILE="$(mktemp)"
trap 'rm -f "$ERR_FILE"' EXIT
if ! .venv/bin/python -c "import app" 2>"$ERR_FILE"; then
  echo "ERROR: connected to PostgreSQL but could not create the schema." >&2
  echo "       Details:" >&2
  sed 's/^/         /' "$ERR_FILE" >&2
  exit 1
fi
rm -f "$ERR_FILE"; trap - EXIT

# ---------------------------------------------------------------------------
# 5. Start the app and print the URL to open
# ---------------------------------------------------------------------------
echo
echo "Basecamp is running. Open http://localhost:${PORT} in your browser (Ctrl+C to stop)."
export PORT
exec .venv/bin/python app.py
