#!/usr/bin/env bash
# Rebuild dashboard.json and sync the static page into Caddy's web root.
#
# Caddy runs as the `caddy` user and /root is 0700, so nothing under the repo is
# servable directly — the page lives in /var/www/cryptoindodax instead and this
# script is the only thing that puts files there. Run it after changing anything
# in web/, and hourly from cron to refresh the data.
set -euo pipefail

ROOT=/root/CryptoIndodaxBot
DEST=/var/www/cryptoindodax

install -d -m 755 "$DEST"
install -m 644 "$ROOT/web/index.html" "$ROOT/web/style.css" "$ROOT/web/app.js" "$DEST/"

cd "$ROOT"
# Write beside the target and rename: a reader mid-refresh gets the old file
# whole, never half of the new one.
"$ROOT/.venv/bin/python" -m cryptoindodax.dashboard "$DEST/.dashboard.json.new"
chmod 644 "$DEST/.dashboard.json.new"
mv -f "$DEST/.dashboard.json.new" "$DEST/dashboard.json"
