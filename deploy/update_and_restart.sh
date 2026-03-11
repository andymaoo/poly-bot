#!/usr/bin/env bash
# Run this script ON the EC2 instance after pulling latest code.
# Usage: cd /home/ubuntu/poly-autobetting && bash deploy/update_and_restart.sh

set -e

BOT_DIR="${BOT_DIR:-/home/ubuntu/poly-autobetting}"
cd "$BOT_DIR"

echo "=== Updating poly-autobetting ==="
git pull origin main || git pull origin master || git pull

echo "Installing dependencies..."
"$BOT_DIR/venv/bin/pip" install -q -r requirements.txt

echo "Restarting poly-autobetting service..."
sudo systemctl restart poly-autobetting

echo "Status:"
sudo systemctl status poly-autobetting --no-pager || true
echo ""
echo "Done. Bot is running. Logs: journalctl -u poly-autobetting -f"
