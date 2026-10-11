#!/usr/bin/env bash
# Run Ferry Watch locally with the real Chromium (in a window) and secrets from 1Password.
#
#   scripts/run-local.sh            # http://localhost:8765
#
# Needs: uv, npm, and the 1Password CLI signed in. Set BCF_USER/BCF_PASS/PUSHOVER_* yourself
# to skip 1Password, or leave them unset to run without logins or alerts.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f web/dist/index.html ]]; then
  (cd web && npm ci --no-audit --no-fund && npm run build)
fi

if command -v op >/dev/null 2>&1; then
  export PUSHOVER_USER_KEY="${PUSHOVER_USER_KEY:-$(op read 'op://kubernetes/pushover/PUSHOVER_USER_KEY')}"
  export PUSHOVER_TOKEN="${PUSHOVER_TOKEN:-$(op read 'op://kubernetes/pushover/PUSHOVER_GATUS_TOKEN')}"
  export BCF_USER="${BCF_USER:-$(op read 'op://kubernetes/ferry-watch/BCF_USER')}"
  export BCF_PASS="${BCF_PASS:-$(op read 'op://kubernetes/ferry-watch/BCF_PASS')}"
fi

export BCF_STATIC="${BCF_STATIC:-web/dist}"
exec uv run ferry-watch "$@"
