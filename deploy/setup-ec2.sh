#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu EC2 server. Run ON THE SERVER, with sudo:
#
#   scp -i ~/keys/mykey.pem -r deploy ubuntu@<PUBLIC-IP>:~
#   ssh -i ~/keys/mykey.pem ubuntu@<PUBLIC-IP>
#   sudo bash deploy/setup-ec2.sh
#
# Idempotent: safe to run more than once.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run this with sudo: sudo bash deploy/setup-ec2.sh" >&2
  exit 1
fi

APP_NAME="basecamp"
APP_DIR="/opt/${APP_NAME}"
APP_USER="${SUDO_USER:-ubuntu}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Database connection details. The app reads DATABASE_URL (set in the systemd unit).
DB_NAME="${DB_NAME:-basecamp}"
DB_USER="${DB_USER:-basecamp}"
DB_PASSWORD="${DB_PASSWORD:-basecamp}"

for f in "${APP_NAME}.service" "nginx-${APP_NAME}.conf"; do
  if [ ! -f "${SCRIPT_DIR}/${f}" ]; then
    echo "ERROR: ${SCRIPT_DIR}/${f} not found. Copy the whole deploy folder to the server first." >&2
    exit 1
  fi
done

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip nginx rsync postgresql postgresql-contrib libpq-dev

echo "==> Starting PostgreSQL"
systemctl enable postgresql
systemctl start postgresql

echo "==> Creating database role and database (idempotent)"
# Create the role if it does not exist, then (re)set its password.
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASSWORD}';
  END IF;
END
\$\$;
ALTER ROLE ${DB_USER} WITH PASSWORD '${DB_PASSWORD}';
SQL

# Create the database only if it is missing (CREATE DATABASE cannot run inside a DO block).
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'" | grep -q 1; then
  sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"
fi

echo "==> Creating ${APP_DIR}"
mkdir -p "${APP_DIR}"
chown "${APP_USER}:${APP_USER}" "${APP_DIR}"

echo "==> Installing systemd service"
# Fill in the runtime user and the DATABASE_URL the app connects with.
DB_URL="postgresql://${DB_USER}:${DB_PASSWORD}@localhost:5432/${DB_NAME}"
sed -e "s/^User=.*/User=${APP_USER}/" \
    -e "s/^Group=.*/Group=${APP_USER}/" \
    -e "s#^Environment=DATABASE_URL=.*#Environment=DATABASE_URL=${DB_URL}#" \
  "${SCRIPT_DIR}/${APP_NAME}.service" > "/etc/systemd/system/${APP_NAME}.service"
systemctl daemon-reload
# deploy.sh starts the service once the code and venv exist.
systemctl enable "${APP_NAME}"

echo "==> Configuring nginx (port 80 -> app on 127.0.0.1:8000)"
cp "${SCRIPT_DIR}/nginx-${APP_NAME}.conf" "/etc/nginx/sites-available/${APP_NAME}"
ln -sf "/etc/nginx/sites-available/${APP_NAME}" "/etc/nginx/sites-enabled/${APP_NAME}"
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx
systemctl restart nginx

echo
echo "Server is ready. Now run ./deploy/deploy.sh from your laptop."
