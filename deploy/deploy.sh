#!/usr/bin/env bash
# Deploy the latest code to your EC2 server. Run from your LAPTOP:
#
#   ./deploy/deploy.sh -h <PUBLIC-IP> -i ~/keys/mykey.pem [-u ubuntu] [-f]
#
#   -f   deploy even if you have uncommitted changes (not recommended: the server
#        would then run code that is not in your git tag)
#
# Steps: check the git tree is clean, run the tests (if a local test database is
# available), copy the code with rsync, install dependencies on the server, restart
# the service, and check /api/health.
set -euo pipefail

APP_NAME="basecamp"
APP_DIR="/opt/${APP_NAME}"
REMOTE_USER="ubuntu"
HOST=""
KEY=""
FORCE=0

usage() {
  echo "Usage: $0 -h <PUBLIC-IP> -i <path-to-key.pem> [-u <login-user>] [-f]" >&2
  exit 1
}

while getopts "h:i:u:f" opt; do
  case "$opt" in
    h) HOST="$OPTARG" ;;
    i) KEY="$OPTARG" ;;
    u) REMOTE_USER="$OPTARG" ;;
    f) FORCE=1 ;;
    *) usage ;;
  esac
done

[ -n "$HOST" ] && [ -n "$KEY" ] || usage
[ -f "$KEY" ] || { echo "ERROR: key file not found: $KEY" >&2; exit 1; }
command -v rsync >/dev/null 2>&1 || { echo "ERROR: rsync is not installed on this machine." >&2; exit 1; }

cd "$(dirname "$0")/.."

SSH_OPTS="-i ${KEY} -o StrictHostKeyChecking=accept-new"
SSH="ssh ${SSH_OPTS} ${REMOTE_USER}@${HOST}"

# 1. Make sure what we deploy is what is committed (so it matches your git tag)
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
  if [ -n "$(git status --porcelain)" ] && [ "$FORCE" -ne 1 ]; then
    echo "ERROR: you have uncommitted changes. Commit (and tag) first so the server runs exactly" >&2
    echo "       what is in git, or re-run with -f to deploy anyway." >&2
    git status --short >&2
    exit 1
  fi
  echo "==> Deploying commit $(git rev-parse --short HEAD) ($(git describe --tags --always 2>/dev/null))"
fi

# 2. Run the tests if a local test database is available; otherwise warn and continue
TEST_URL="${TEST_DATABASE_URL:-postgresql://basecamp:basecamp@localhost:5432/basecamp_test}"
if [ -d tests ]; then
  if [ -x .venv/bin/python ] && command -v psql >/dev/null 2>&1 \
     && psql "$TEST_URL" -Atqc 'SELECT 1' >/dev/null 2>&1; then
    echo "==> Running tests"
    .venv/bin/python -m pytest -q
  else
    echo "WARNING: skipping tests (needs the local .venv and the test database)." >&2
    echo "         Run ./start.sh once on this machine to set them up." >&2
  fi
else
  echo "==> No tests/ folder, skipping tests"
fi

# 3. Copy the code (never the key, .env, local venv, or the server's data)
echo "==> Copying code to ${HOST}"
rsync -az --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '*.pem' --exclude '.env' --exclude '.env.*' \
  --exclude 'instance' --exclude '*.db' --exclude 'uploads' \
  -e "ssh ${SSH_OPTS}" \
  ./ "${REMOTE_USER}@${HOST}:${APP_DIR}/"

# 4. Install dependencies and restart the service
echo "==> Installing dependencies and restarting"
$SSH "cd ${APP_DIR} \
  && { [ -x .venv/bin/python ] || python3 -m venv .venv; } \
  && .venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt \
  && sudo systemctl restart ${APP_NAME}"

# 5. Check that the app answers
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
