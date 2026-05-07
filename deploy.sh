#!/bin/bash
# Deploy script - runs on the VPS
set -e

echo "=== Installing system dependencies ==="
apt-get update -qq
apt-get install -y -qq python3-pip python3-venv nginx certbot python3-certbot-nginx

echo "=== Setting up app directory ==="
mkdir -p /opt/backyard-leads
cd /opt/backyard-leads

echo "=== Creating virtual environment ==="
python3 -m venv venv
source venv/bin/activate

echo "=== Installing Python dependencies ==="
pip install --quiet -r requirements.txt

echo "=== Setting up systemd service ==="
cat > /etc/systemd/system/backyard-leads.service << 'SERVICEEOF'
[Unit]
Description=Backyard Leads App
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/backyard-leads
Environment=PATH=/opt/backyard-leads/venv/bin:/usr/bin
EnvironmentFile=/opt/backyard-leads/.env
ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_signature_fields
ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_leads_to_companies
ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_netrows_caches
ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_runtime_config
ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_roles_and_views
ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_campaigns
ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_twilio_fields
ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_call_fields
ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_deepgram_key
ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_dial_modes
ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_sms_optout
ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_blooio_key
ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_phone_type
ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_sequence_engine
ExecStartPre=/opt/backyard-leads/venv/bin/python -m scripts.migrate_messaging_direction
ExecStart=/opt/backyard-leads/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICEEOF

echo "=== Setting up Nginx ==="
cat > /etc/nginx/sites-available/backyard-leads << 'NGINXEOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }

    location /static {
        alias /opt/backyard-leads/static;
        expires 1d;
    }
}
NGINXEOF

ln -sf /etc/nginx/sites-available/backyard-leads /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

echo "=== Starting services ==="
systemctl daemon-reload
systemctl enable backyard-leads
systemctl restart backyard-leads
nginx -t && systemctl restart nginx

echo "=== Done! App is running ==="
systemctl status backyard-leads --no-pager
