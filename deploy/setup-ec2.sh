#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu EC2 server. Run ON THE SERVER, with sudo:
#
#   scp -i ~/keys/mykey.pem -r deploy ubuntu@<PUBLIC-IP>:~
#   ssh -i ~/keys/mykey.pem ubuntu@<PUBLIC-IP>
#   sudo bash deploy/setup-ec2.sh
#
# Shouldnt die if ran more than once?
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run this with sudo: sudo bash deploy/setup-ec2.sh" >&2
  exit 1
fi

APP_NAME="basecamp"
APP_DIR="/opt/${APP_NAME}"
APP_USER="${SUDO_USER:-ubuntu}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for f in "${APP_NAME}.service" "nginx-${APP_NAME}.conf"; do
  if [ ! -f "${SCRIPT_DIR}/${f}" ]; then
    echo "ERROR: ${SCRIPT_DIR}/${f} not found. Copy the whole deploy folder to the server first." >&2
    exit 1
  fi
done

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip nginx rsync

echo "==> Creating ${APP_DIR}"
mkdir -p "${APP_DIR}"
chown "${APP_USER}:${APP_USER}" "${APP_DIR}"

echo "==> Installing systemd service"
sed "s/^User=.*/User=${APP_USER}/; s/^Group=.*/Group=${APP_USER}/" \
  "${SCRIPT_DIR}/${APP_NAME}.service" > "/etc/systemd/system/${APP_NAME}.service"
systemctl daemon-reload
# Start on boot by deploy.sh once the code and venv exist.
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
