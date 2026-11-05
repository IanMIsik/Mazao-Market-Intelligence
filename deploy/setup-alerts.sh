#!/usr/bin/env bash
set -euo pipefail

# Turns on the alert emails (see web/alerts.py). Run it ON THE SERVER, in the app directory:
#
#   cd ~/Mazao-Market-Intelligence && bash deploy/setup-alerts.sh
#
# It asks for the recipient address and your SMTP login, writes them to .env (mode 600, a backup
# is kept), restarts the app, and sends a test email. The password is typed hidden and goes
# straight into .env -- it is never printed, logged, or put in git.
#
# For Gmail: turn on 2-step verification, then create an "App password" at
# https://myaccount.google.com/apppasswords and use that as the password (not your normal one).
#
# Test hooks (used by the tests, not needed in normal use):
#   ENV_FILE=/path/to/.env        write somewhere else
#   ALERT_SETUP_NO_DOCKER=1       skip restarting the app and the test email

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"

current() { [ -f "$ENV_FILE" ] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -1 | sed "s/^'\(.*\)'$/\1/" || true; }

ask() {  # ask "Prompt" "default" -> echoes the answer
  local prompt="$1" default="${2:-}" reply
  if [ -n "$default" ]; then read -r -p "$prompt [$default]: " reply; echo "${reply:-$default}"
  else read -r -p "$prompt: " reply; echo "$reply"; fi
}

upsert() {  # upsert KEY VALUE -- replaces any existing KEY= line, appends otherwise
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  { [ -f "$ENV_FILE" ] && grep -v "^$key=" "$ENV_FILE" || true; } > "$tmp"
  printf "%s='%s'\n" "$key" "$value" >> "$tmp"
  cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
}

echo "==> Alert email setup"
TO="$(ask "Send alerts to (comma-separate several)" "$(current ALERT_EMAIL_TO)")"
HOST="$(ask "SMTP server" "$(current SMTP_HOST || true)")"; HOST="${HOST:-smtp.gmail.com}"
PORT="$(ask "SMTP port (587 = STARTTLS, 465 = SSL)" "$(current SMTP_PORT || true)")"; PORT="${PORT:-587}"
USER_="$(ask "SMTP username (usually your email address)" "$(current SMTP_USER || true)")"; USER_="${USER_:-$TO}"
read -r -s -p "SMTP password / app password (hidden; leave empty to keep the saved one): " PASS; echo
PASS="${PASS// /}"   # Google shows app passwords in groups of four with spaces; they are not part of it
[ -z "$PASS" ] && PASS="$(current SMTP_PASSWORD)"

[ -n "$TO" ] || { echo "No recipient given."; exit 1; }
[ -n "$PASS" ] || { echo "No password given."; exit 1; }
case "$PASS$TO$HOST$USER_" in *"'"*) echo "A value contains a single quote, which .env cannot hold here."; exit 1;; esac

[ -f "$ENV_FILE" ] && cp "$ENV_FILE" "$ENV_FILE.bak-before-alerts"
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
upsert ALERT_EMAIL_TO "$TO"
upsert SMTP_HOST "$HOST"
upsert SMTP_PORT "$PORT"
upsert SMTP_USER "$USER_"
upsert SMTP_PASSWORD "$PASS"
echo "==> Saved to $ENV_FILE (previous copy: $ENV_FILE.bak-before-alerts)"

if [ -n "${ALERT_SETUP_NO_DOCKER:-}" ]; then exit 0; fi

cd "$APP_DIR"
echo "==> Restarting the app to pick the settings up"
sudo docker compose --profile risk up -d gbpw
echo "==> Waiting for it to start"
for _ in $(seq 1 30); do sudo docker compose exec -T gbpw true 2>/dev/null && break; sleep 2; done
echo "==> Sending a test email to $TO"
if sudo docker compose exec -T gbpw gbpw test-alert; then
  echo "==> Done. Check the inbox (and spam) for \"[Mazao] Test alert\"."
else
  echo "==> The test email failed. For Gmail, make sure you used an App Password and 2-step verification is on."
  echo "    The error is above; re-run this script to try different settings."
  exit 1
fi
