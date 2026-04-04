#!/usr/bin/env bash
# EC2 User Data — runs once on first boot to configure the PostIQ instance.
# This is passed to the EC2 launch command.

set -euo pipefail

# ── System packages ──────────────────────────────────────────────────
apt-get update -y
apt-get install -y docker.io docker-compose-v2 git awscli

# Start Docker
systemctl enable docker
systemctl start docker

# ── Clone repository ─────────────────────────────────────────────────
cd /opt
git clone https://github.com/greatoakai/postiq.git
cd postiq

# ── Write .env with AWS references ──────────────────────────────────
# The EC2 instance uses IAM role credentials (no keys needed).
# These env vars tell the app to use AWS services.
ACCOUNT_ID=$(curl -s http://169.254.169.254/latest/meta-data/identity-credentials/ec2/info | python3 -c "import sys,json;print(json.load(sys.stdin)['AccountId'])" 2>/dev/null || echo "unknown")
REGION=$(curl -s http://169.254.169.254/latest/meta-data/placement/region)

cat > .env <<ENVEOF
# AWS Mode — credentials fetched from Secrets Manager
S3_BUCKET=postiq-data-${ACCOUNT_ID}
AWS_REGION=${REGION}
HEADLESS=true
ENVEOF

# ── Build and start ─────────────────────────────────────────────────
docker compose build
docker compose up -d web

# ── Install Playwright browsers inside the container ────────────────
docker compose run --rm --no-deps web playwright install chromium

# ── Set up cron for scheduled bot runs ──────────────────────────────
cat > /etc/cron.d/postiq <<CRONEOF
# PostIQ: Run bot at 7:00 AM EST (12:00 UTC) Monday-Friday
0 12 * * 1-5 root cd /opt/postiq && /opt/postiq/deploy/cron_run.sh >> /var/log/postiq-cron.log 2>&1
CRONEOF

chmod 644 /etc/cron.d/postiq
echo "PostIQ setup complete."
