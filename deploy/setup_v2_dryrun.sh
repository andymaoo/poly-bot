#!/usr/bin/env bash
# Setup and start V2 bot in dry-run mode alongside the live V1 bot.
# Run on EC2: bash deploy/setup_v2_dryrun.sh
# Live V1 stays at ~/poly-autobetting (unchanged).

set -e

V2_DIR="${HOME}/poly-bot-v2"
LIVE_DIR="${HOME}/poly-autobetting"
REPO_URL="https://github.com/andymaoo/poly-bot.git"
BRANCH="algo-v2"
TMUX_SESSION="v2-dryrun"

echo "=== V2 dry-run setup ==="

if [[ -d "$V2_DIR" ]]; then
  echo "Directory $V2_DIR exists; pulling and checking out $BRANCH..."
  cd "$V2_DIR"
  git fetch origin
  git checkout "$BRANCH"
  git pull origin "$BRANCH"
else
  echo "Cloning poly-bot to $V2_DIR..."
  git clone "$REPO_URL" "$V2_DIR"
  cd "$V2_DIR"
  git checkout "$BRANCH"
fi

if [[ -f "$LIVE_DIR/.env" ]]; then
  echo "Copying .env from live bot..."
  cp "$LIVE_DIR/.env" "$V2_DIR/.env"
else
  echo "WARNING: $LIVE_DIR/.env not found. Create .env in $V2_DIR with API keys."
fi

echo "Installing dependencies..."
if [[ ! -d "$V2_DIR/venv" ]]; then
  python3 -m venv "$V2_DIR/venv"
fi
"$V2_DIR/venv/bin/pip" install -q -r "$V2_DIR/requirements.txt"

echo "Starting dry-run in tmux session: $TMUX_SESSION"
tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
tmux new-session -d -s "$TMUX_SESSION" -c "$V2_DIR" \
  "$V2_DIR/venv/bin/python scripts/place_45.py --dry-run"

echo ""
echo "Done. Attach to the dry-run with:  tmux attach -t $TMUX_SESSION"
echo "Detach without stopping: Ctrl+B then D"
echo "After 1-2 hours, stop with Ctrl+C, then run:  python scripts/analyze_dryrun.py"
