#!/bin/bash
# Checks the sites that only answer from an Indian IP, then pushes the results
# to the same repository GitHub Actions writes to. One archive, two runners.
#
# First-time setup is in README.md under "The local tier".

set -euo pipefail
export PYTHONUNBUFFERED=1
cd "$(dirname "$0")/.."

LOG="$HOME/Library/Logs/gov-watch.log"
mkdir -p "$(dirname "$LOG")"
# tee, not redirect: when run by hand you see progress; when run by launchd it
# still lands in the log. A silent script that is waiting for a password looks
# identical to one that has crashed.
exec > >(tee -a "$LOG") 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') ==="

# Fail fast instead of blocking forever on a hidden credential prompt.
export GIT_TERMINAL_PROMPT=0

if [ ! -d .venv ]; then
  echo "no .venv -- run the setup steps in README.md first"; exit 1
fi
source .venv/bin/activate

# Take whatever Actions has committed since the last local run.
git pull --rebase --autostash || { echo "pull failed, skipping this run"; exit 1; }

python scripts/check.py --tier local
python scripts/build_site.py

git add -A snapshots data docs
if git diff --staged --quiet; then
  echo "nothing changed"
  exit 0
fi
git commit -m "local check $(date -u +'%Y-%m-%d %H:%M UTC')"

# Actions may have pushed while we were fetching; rebase and try again.
for attempt in 1 2 3; do
  if git push; then echo "pushed"; exit 0; fi
  if [ "$attempt" = 1 ]; then
    echo "push failed -- if this is a credentials error, run 'git push' by hand"
    echo "once to store a personal access token in the keychain."
  fi
  echo "push rejected, rebasing (attempt $attempt)"
  git pull --rebase --autostash
done
echo "could not push after 3 attempts"
exit 1
