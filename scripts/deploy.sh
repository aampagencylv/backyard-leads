#!/bin/bash
# One-line deploy: pulls latest main on the VPS and restarts the service.
# Migration runs automatically as systemd ExecStartPre.
#
# Usage (from any laptop with SSH access to vps):
#   ./scripts/deploy.sh
set -e

VPS="${VPS_HOST:-vps}"

echo "==> Deploying main to $VPS..."
ssh "$VPS" "set -e
  cd /opt/backyard-leads
  git fetch -q origin main
  BEFORE=\$(git rev-parse HEAD)
  git reset --hard origin/main
  AFTER=\$(git rev-parse HEAD)
  if [ \"\$BEFORE\" = \"\$AFTER\" ]; then
    echo 'Already at latest commit, restarting anyway...'
  else
    echo \"Updated: \$BEFORE -> \$AFTER\"
    git log --oneline \$BEFORE..\$AFTER
  fi
  ./venv/bin/pip install -q -r requirements.txt
  systemctl restart backyard-leads
  sleep 2
  systemctl is-active backyard-leads
  curl -s -H 'Host: prospector.backyardmarketingpros.com' http://127.0.0.1/health"
echo "==> Deploy complete: https://prospector.backyardmarketingpros.com"
