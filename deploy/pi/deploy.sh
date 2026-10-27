#!/bin/bash
# Copy comma-osm-speed to the Pi, install it, and (re)start its services.
# Never overwrites the Pi's data/, output/, .env or ignored_ways.txt: the Pi's copies are the current ones.
# .env and ignored_ways.txt are copied over only if the Pi doesn't have them yet.
set -euo pipefail
cd "$(dirname "$0")/../.."
PI=${PI:-shafman@shafpi.local}

rsync -a --delete --itemize-changes \
  --exclude .git --exclude .venv --exclude data/ --exclude output/ --exclude history/ \
  --exclude .env --exclude ignored_ways.txt --exclude __pycache__ --exclude .DS_Store \
  --exclude '*.egg-info' --exclude build/ --exclude .pytest_cache --exclude .ruff_cache \
  ./ "$PI":comma-osm-speed/
rsync -a --ignore-existing .env ignored_ways.txt "$PI":comma-osm-speed/

ssh "$PI" bash -s <<'REMOTE'
set -euo pipefail
cd ~/comma-osm-speed
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade .
mkdir -p ~/.config/systemd/user
cp deploy/pi/*.service deploy/pi/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now comma-osm-speed-review.service comma-osm-speed-refresh.timer
systemctl --user restart comma-osm-speed-review.service
REMOTE
echo "Deployed. Review page: http://${PI#*@}:8765/"
