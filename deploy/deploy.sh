#!/bin/bash
# Deploy place_45 updates to EC2 and restart the bot.
# Usage: ./deploy/deploy.sh
#   Optional overrides:
#     ./deploy/deploy.sh ubuntu@other-host /path/to/polybot.pem

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_HOST="ubuntu@ec2-54-171-136-147.eu-west-1.compute.amazonaws.com"
HOST="${1:-${EC2_HOST:-$DEFAULT_HOST}}"
KEY_PATH="${2:-${EC2_KEY_PATH:-$REPO_ROOT/polybot.pem}}"
REMOTE_REPO_PATH="${REMOTE_REPO_PATH:-/home/ubuntu/poly-autobetting}"
SERVICE_NAME="${SERVICE_NAME:-poly-autobetting}"

if [ ! -f "$KEY_PATH" ]; then
  echo "SSH key not found: $KEY_PATH"
  exit 1
fi

echo "Deploying to $HOST..."
ssh -i "$KEY_PATH" -o StrictHostKeyChecking=accept-new "$HOST" "cd $REMOTE_REPO_PATH && git checkout stable-v1 && git pull --ff-only origin stable-v1 && sudo systemctl restart $SERVICE_NAME && sudo systemctl status $SERVICE_NAME --no-pager"
echo "Deploy complete. Bot restarted."
