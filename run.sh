#!/usr/bin/env bash
# Start the watcher + web UI (http://localhost:8765) with Pushover creds from 1Password.
set -euo pipefail
cd "$(dirname "$0")"

export PUSHOVER_USER_KEY="${PUSHOVER_USER_KEY:-$(op read 'op://kubernetes/pushover/PUSHOVER_USER_KEY')}"
# Reuses the gatus app token; add a dedicated PUSHOVER_BCFERRIES_TOKEN field to swap it.
export PUSHOVER_TOKEN="${PUSHOVER_TOKEN:-$(op read 'op://kubernetes/pushover/PUSHOVER_GATUS_TOKEN')}"

# BC Ferries login, used only to add an open sailing to the cart in a browser window on this Mac.
export BCF_USER="${BCF_USER:-$(op read 'op://Private/Bcferries/username')}"
export BCF_PASS="${BCF_PASS:-$(op read 'op://Private/Bcferries/password')}"

exec uv run watch.py "$@"
