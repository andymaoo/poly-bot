#!/usr/bin/env bash
# Deploy from local machine to EC2 and start the bot.
# Usage: EC2_HOST=ec2-xx-xx-xx-xx.compute-1.amazonaws.com bash deploy/deploy.sh
#    or: bash deploy/deploy.sh ec2-xx-xx-xx-xx.compute-1.amazonaws.com

set -e

HOST="${EC2_HOST:-$1}"
if [[ -z "$HOST" ]]; then
  echo "Usage: EC2_HOST=your-ec2-address bash deploy/deploy.sh"
  echo "   or: bash deploy/deploy.sh your-ec2-address"
  exit 1
fi

echo "Deploying to ubuntu@$HOST ..."
ssh -o ConnectTimeout=10 ubuntu@"$HOST" "cd /home/ubuntu/poly-autobetting && git pull && bash deploy/update_and_restart.sh"
