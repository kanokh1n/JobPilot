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
# hh sign-ins made on the Mac (jobpilot hh-auth) go up once: the server refreshes its token from then on,
# and an older copy from the Mac would break the refresh.
for dir in data/hh/*/; do
  [ -f "$dir/token.json" ] || continue
  ssh "$HOST" "test -f $DIR/$dir/token.json" && continue
  ssh "$HOST" "mkdir -p $DIR/$dir"
  scp "$dir"token.json "$dir"cookies.txt "$HOST:$DIR/$dir"
done
for file in .env accounts.toml; do
  [ -f "$file" ] && scp "$file" "$HOST:$DIR/$file"
done
ssh "$HOST" "cd $DIR && docker compose up -d --build"
echo "Deployed. First time only, sign the Telegram userbot in:"
echo "  ssh -t $HOST 'cd $DIR && docker compose run --rm jobpilot jobpilot login'"
