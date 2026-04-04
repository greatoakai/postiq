#!/usr/bin/env bash
# EC2 User Data — runs once on first boot (HIPAA Compliant).
# This is passed to the EC2 launch command.

set -euo pipefail
exec > /var/log/postiq-bootstrap.log 2>&1

# -- System packages --
apt-get update -y
apt-get install -y docker.io docker-compose-v2 git nginx

# Start Docker
systemctl enable docker
systemctl start docker

# -- Clone repository --
cd /opt
git clone -b claude/debug-bot-issues-ILtby https://github.com/greatoakai/postiq.git
cd postiq

# -- Write .env with AWS references --
ACCOUNT_ID=$(curl -s http://169.254.169.254/latest/meta-data/identity-credentials/ec2/info | python3 -c "import sys,json;print(json.load(sys.stdin)['AccountId'])" 2>/dev/null || echo "unknown")
REGION=$(curl -s http://169.254.169.254/latest/meta-data/placement/region)

cat > .env <<ENVEOF
# AWS Mode - credentials fetched from Secrets Manager
S3_BUCKET=postiq-data-${ACCOUNT_ID}
AWS_REGION=${REGION}
HEADLESS=true
ENVEOF

# -- Build and start --
docker compose build
docker compose up -d web

# Install Playwright browsers inside the container
docker compose run --rm --no-deps web playwright install chromium

# -- HTTPS: Self-signed certificate + Nginx reverse proxy --
# (Replace with ACM + ALB for production trusted cert)
mkdir -p /etc/nginx/ssl
openssl req -x509 -nodes -days 365 \
    -newkey rsa:2048 \
    -keyout /etc/nginx/ssl/postiq.key \
    -out /etc/nginx/ssl/postiq.crt \
    -subj "/CN=postiq/O=GreatOakCounseling"
chmod 600 /etc/nginx/ssl/postiq.key

cat > /etc/nginx/sites-available/postiq <<'NGINXEOF'
server {
    listen 443 ssl;
    server_name _;

    ssl_certificate     /etc/nginx/ssl/postiq.crt;
    ssl_certificate_key /etc/nginx/ssl/postiq.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 86400;
    }
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}
NGINXEOF

ln -sf /etc/nginx/sites-available/postiq /etc/nginx/sites-enabled/postiq
rm -f /etc/nginx/sites-enabled/default
systemctl enable nginx
systemctl restart nginx

# -- CloudWatch agent for audit logging --
wget -q https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb
dpkg -i amazon-cloudwatch-agent.deb
rm -f amazon-cloudwatch-agent.deb

cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<'CWEOF'
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/postiq-cron.log",
            "log_group_name": "/postiq/bot",
            "log_stream_name": "{instance_id}",
            "retention_in_days": 90
          },
          {
            "file_path": "/var/log/postiq-bootstrap.log",
            "log_group_name": "/postiq/streamlit",
            "log_stream_name": "{instance_id}-bootstrap",
            "retention_in_days": 90
          }
        ]
      }
    }
  }
}
CWEOF

/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config \
    -m ec2 \
    -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json \
    -s

# -- Set up cron for scheduled bot runs --
cat > /etc/cron.d/postiq <<CRONEOF
# PostIQ: Run bot at 7:00 AM EST (12:00 UTC) Monday-Friday
0 12 * * 1-5 root cd /opt/postiq && /opt/postiq/deploy/cron_run.sh >> /var/log/postiq-cron.log 2>&1
CRONEOF

chmod 644 /etc/cron.d/postiq
echo "PostIQ HIPAA-compliant setup complete."
