#!/usr/bin/env bash
set -euo pipefail

# One-shot provisioning for a fresh EC2 instance (Ubuntu 24.04/26.04 LTS,
# full or Minimal -- both apt-based, nothing here depends on anything
# beyond base Ubuntu + apt). Installs Docker, clones/updates this repo,
# brings the app up via docker-compose.yml (see README's "Deployment"
# section, Option A -- this script is just that option automated), and
# fronts it with Nginx on port 80 so uvicorn itself is never exposed
# directly to the internet.
#
# Idempotent -- safe to re-run after a `git pull` to rebuild and pick up
# new commits, or if a step failed partway through the first time.
#
# Usage: bash deploy/setup.sh [repo_url] [server_name]
#   repo_url     defaults to this project's GitHub URL
#   server_name  Nginx server_name -- your domain if you have one, or
#                omit it to match any Host header (fine for an IP-only
#                demo; tighten this once you point a real domain at it)

REPO_URL="${1:-https://github.com/IanMIsik/Mazao-Market-Intelligence.git}"
SERVER_NAME="${2:-_}"
APP_DIR="$HOME/Mazao-Market-Intelligence"
APP_PORT=5000  # matches docker-compose.yml's "5000:5000" and cli.py's `serve` default

echo "==> Installing Docker + Nginx"
sudo apt update
sudo apt install -y ca-certificates curl nginx
if ! command -v docker >/dev/null 2>&1; then
  # Docker's own official convenience script -- installs Engine + the
  # `docker compose` plugin together, which is all this needs.
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  echo "NOTE: added $USER to the docker group -- that only takes effect in a NEW login session."
  echo "      This script still works right now (it uses sudo for docker below), but for"
  echo "      sudo-less 'docker compose ...' afterwards, log out and back in first."
fi

echo "==> Cloning/updating the repo"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull
else
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

# Bind-mounted into the container by docker-compose.yml -- must exist
# and must NOT be wiped by this script on a re-run, or you lose every
# bit of ingested history (see README's "Persist data/ across deploys").
mkdir -p data out

if [ ! -f .env ]; then
  echo "==> Writing a blank .env (both keys are optional -- see README's Environment Variables table)"
  cat > .env <<'EOF'
# Optional. Uncomment and fill in if you want these features:
# ANTHROPIC_API_KEY=   # AI-written GB Power Weekly narrative; falls back to a rule-based one without it
# ENTSOE_KEY=          # Interconnector flows on Live Market; that series just isn't ingested without it
EOF
fi

echo "==> Building and starting the app container"
sudo docker compose up -d --build

echo "==> Configuring Nginx reverse proxy (port 80 -> 127.0.0.1:$APP_PORT)"
sudo tee /etc/nginx/sites-available/gbpw > /dev/null <<EOF
server {
    listen 80;
    server_name $SERVER_NAME;

    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/gbpw /etc/nginx/sites-enabled/gbpw
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
sudo systemctl enable nginx

echo
echo "==> Done. Should be reachable at http://<this instance's public IP or domain>/"
echo "    (make sure your EC2 security group allows inbound port 80 from 0.0.0.0/0)"
echo
echo "    Container status: sudo docker compose ps"
echo "    Logs:             sudo docker compose logs -f"
echo
echo "==> One-time data loads -- these pages are empty until you run them (not on the 5-min refresh cycle):"
echo "    sudo docker compose exec gbpw gbpw ingest-cfd-auctions"
echo "    sudo docker compose exec gbpw gbpw ingest-desnz-prices"
echo "    sudo docker compose exec gbpw gbpw ingest-gdp-deflator"
echo
echo "==> For HTTPS, once a real domain's DNS points at this instance's IP:"
echo "    sudo apt install -y certbot python3-certbot-nginx"
echo "    sudo certbot --nginx -d yourdomain.example"
