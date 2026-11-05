#!/usr/bin/env bash
set -euo pipefail

# Puts the Portfolio Risk KPI tool at /risk/ behind the site's sign-in page.
# Idempotent. Run on the server from the app directory, after the Mazao app
# with the sign-in code is deployed:
#
#   bash deploy/enable-risk.sh [risk_app_dir]
#
#   risk_app_dir  where the "UK Futures Analysis Tool" project (its Dockerfile)
#                 lives on this server; default ~/risk-kpi
#
# What it does:
#   1. installs deploy/nginx-risk.conf as an Nginx snippet and includes it in the
#      site's server block (once), then reloads Nginx
#   2. builds and starts the `risk` container (compose profile "risk")
# It does NOT create accounts: run `gbpw create-user` (see README, "Sign-in").

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RISK_DIR="${1:-$HOME/risk-kpi}"
SITE=/etc/nginx/sites-available/gbpw
SNIPPET=/etc/nginx/snippets/gbpw-risk.conf

[ -f "$RISK_DIR/Dockerfile" ] || { echo "No Dockerfile in $RISK_DIR -- put the UK Futures Analysis Tool project there (or pass its path)."; exit 1; }
[ -f "$SITE" ] || { echo "$SITE not found -- was the site set up with deploy/setup.sh?"; exit 1; }

echo "==> Installing Nginx rules"
sudo mkdir -p /etc/nginx/snippets
sudo cp "$APP_DIR/deploy/nginx-risk.conf" "$SNIPPET"
if ! grep -qF "include snippets/gbpw-risk.conf;" "$SITE"; then
  # Before the first `location / {` of the server block (certbot keeps it in the same block).
  sudo sed -i '0,/^[[:space:]]*location \/ {/s//    include snippets\/gbpw-risk.conf;\n\n    location \/ {/' "$SITE"
fi
grep -qF "include snippets/gbpw-risk.conf;" "$SITE" || { echo "Could not add the include to $SITE -- add 'include snippets/gbpw-risk.conf;' inside the server block by hand."; exit 1; }
sudo nginx -t
sudo systemctl reload nginx

echo "==> Building and starting the risk container"
cd "$APP_DIR"
RISK_APP_DIR="$RISK_DIR" sudo -E docker compose --profile risk up -d --build risk

echo
echo "==> Done. /risk/ now asks for a sign-in. Create an account with:"
echo "    sudo docker compose exec gbpw gbpw create-user --email someone@example.com"
