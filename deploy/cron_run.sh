#!/usr/bin/env bash
# Cron wrapper: runs the bot in scheduled mode.
# Picks up the latest unprocessed CSV from S3 and posts payments.
#
# Called by /etc/cron.d/postiq at 7 AM EST (12 UTC) Mon-Fri.

set -euo pipefail

PROJECT_DIR="/opt/postiq"
LOG_FILE="/var/log/postiq-cron.log"

echo ""
echo "=========================================="
echo "PostIQ Scheduled Run — $(date)"
echo "=========================================="

cd "${PROJECT_DIR}"

# Pull latest code (optional — remove if you prefer pinned deployments)
git pull origin main --quiet 2>/dev/null || true

# Run the bot in scheduled mode via Docker
docker compose run --rm bot --scheduled

echo "Scheduled run complete — $(date)"
