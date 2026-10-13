#!/usr/bin/env bash
# Deploy the latest code to your EC2 server. Run from your LAPTOP:
#
#   ./deploy/deploy.sh -h <PUBLIC-IP> -i ~/keys/mykey.pem [-u ubuntu]
#
# Runs tests (in the future when I make them), copies the code, installs
# dependencies on the server, restarts the service, and checks /api/health.
set -euo pipefail

APP_NAME="basecamp"
APP_DIR="/opt/${APP_NAME}"
REMOTE_USER="ubuntu"
HOST=""
KEY=""

usage() {
  echo "Usage: $0 -h <PUBLIC-IP> -i <path-to-key.pem> [-u <login-user>]" >&2
  exit 1
}

while getopts "h:i:u:" opt; do
  case "$opt" in
    h) HOST="$OPTARG" ;;
    i) KEY="$OPTARG" ;;
    u) REMOTE_USER="$OPTARG" ;;
    *) usage ;;
  esac
done

[ -n "$HOST" ] && [ -n "$KEY" ] || usage
[ -f "$KEY" ] || { echo "ERROR: key file not found: $KEY" >&2; exit 1; }
command -v rsync >/dev/null 2>&1 || { echo "ERROR: rsync is not installed on this machine." >&2; exit 1; }

cd "$(dirname "$0")/.."

SSH_OPTS="-i ${KEY} -o StrictHostKeyChecking=accept-new"
SSH="ssh ${SSH_OPTS} ${REMOTE_USER}@${HOST}"

# 1. Run tests first
if [ -d tests ]; then
  echo "==> Running tests"
  if [ -x .venv/bin/python ]; then
    .venv/bin/python -m pytest -q
  else
    python3 -m pytest -q
  fi
else
  echo "==> No tests/ folder yet, skipping tests"
fi

# 2. Copy the code (never the key, .env, local venv, or the server's database since on blacklist)
echo "==> Copying code to ${HOST}"
rsync -az --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude '*.pem' --exclude '.env' --exclude '.env.*' \
  --exclude 'instance' --exclude '*.db' --exclude 'uploads' \
  -e "ssh ${SSH_OPTS}" \
  ./ "${REMOTE_USER}@${HOST}:${APP_DIR}/"

# 3. Install dependencies and restart the service
echo "==> Installing dependencies and restarting"
$SSH "cd ${APP_DIR} \
  && { [ -x .venv/bin/python ] || python3 -m venv .venv; } \
  && .venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt \
  && sudo systemctl restart ${APP_NAME}"

# 4. Check that the app answers
echo "==> Checking http://${HOST}/api/health"
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "http://${HOST}/api/health" >/dev/null 2>&1; then
    echo "Deploy succeeded: http://${HOST}/"
    exit 0
  fi
  sleep 2
done

echo "ERROR: the health check failed. On the server, run: sudo journalctl -u ${APP_NAME} -n 50" >&2
exit 1
