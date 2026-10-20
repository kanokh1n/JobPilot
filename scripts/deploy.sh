#!/usr/bin/env bash
# Usage: scripts/deploy.sh root@SERVER_IP
# Installs Docker on the server if needed, copies the project (and your local .env / accounts.toml)
# to /opt/jobpilot and (re)starts it. Server data (userbot session, database) stays in /opt/jobpilot/data.
set -euo pipefail

HOST="${1:?usage: scripts/deploy.sh user@host}"
DIR=/opt/jobpilot

ssh "$HOST" "command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh; mkdir -p $DIR"
rsync -az --delete \
  --exclude .git --exclude .env --exclude accounts.toml --exclude 'data/' \
  --exclude __pycache__ --exclude .venv --exclude .pytest_cache \
  ./ "$HOST:$DIR/"
ssh "$HOST" "mkdir -p $DIR/data/letters"
rsync -az data/letters/ "$HOST:$DIR/data/letters/"
# hh logins: only the exported cookies travel (a browser profile is not portable between Mac and Linux)
for state in data/browser/*/state.json; do
  [ -f "$state" ] || continue
  ssh "$HOST" "mkdir -p $DIR/$(dirname "$state")"
  scp "$state" "$HOST:$DIR/$state"
done
for file in .env accounts.toml; do
  [ -f "$file" ] && scp "$file" "$HOST:$DIR/$file"
done
ssh "$HOST" "cd $DIR && docker compose up -d --build"
echo "Deployed. First time only, sign the Telegram userbot in:"
echo "  ssh -t $HOST 'cd $DIR && docker compose run --rm jobpilot jobpilot login'"
