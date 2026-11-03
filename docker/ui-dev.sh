#!/usr/bin/env bash
#
# Runs the browser UI's dev server inside the GS container: tmux session `ui`,
# started by the compose command next to `gs`.
#
# ui/node_modules is a docker volume (see the compose file), not the host's
# folder: the container's npm never writes root-owned files into the checkout,
# and the host's own `npm install` stays independent of this one.
#
# Dependencies are installed only when package-lock.json is newer than the last
# install, so a normal start goes straight to vite.

set -e

cd /work/ui

stamp=node_modules/.package-lock.json   # written by npm on every install
if [ ! -f "$stamp" ] || [ package-lock.json -nt "$stamp" ]; then
    echo "Installing UI dependencies (npm ci)..."
    npm ci --no-audit --no-fund
fi

exec npm run dev
