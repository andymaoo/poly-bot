#!/bin/bash
# Deploy place_45 updates to EC2 and restart the bot.
# Usage: ./deploy/deploy.sh [ec2-host]
#   ec2-host: SSH target (e.g. ubuntu@ec2-xx-xx-xx-xx.compute.amazonaws.com)
#   Or set EC2_HOST env var, or add Host poly to ~/.ssh/config

set -e
HOST="${1:-$EC2_HOST}"
if [ -z "$HOST" ]; then
  echo "Usage: ./deploy/deploy.sh ubuntu@your-ec2-host"
  echo "   Or: EC2_HOST=ubuntu@your-ec2-host ./deploy/deploy.sh"
  exit 1
fi

echo "Deploying to $HOST..."
ssh "$HOST" "cd /home/ubuntu/poly-autobetting && git pull origin stable-v1 && sudo systemctl restart poly-autobetting && sudo systemctl status poly-autobetting"
echo "Deploy complete. Bot restarted."
