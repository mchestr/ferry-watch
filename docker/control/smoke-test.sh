#!/usr/bin/env bash
# Smoke test for the control image (docs/multi-tenant/design-reference.md §11.3).
#   docker/control/smoke-test.sh <image>
# Read-only and unprivileged: both entrypoints load, the SPA is there, and no browser library is
# installed at all.
set -euo pipefail

image="${1:?usage: smoke-test.sh <image>}"
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges "$image" sh -euc '
  test "$(id -u)" = 65532
  python -c "import ferry_watch.web.main, ferry_watch.control.main"
  ! python -c "import playwright" 2>/dev/null
  ! python -c "import selectolax" 2>/dev/null
  ferry-web --help >/dev/null
  ferry-control --help >/dev/null
  test -f /app/static/index.html
  python -c "import zoneinfo; zoneinfo.ZoneInfo(\"America/Vancouver\")"
'
echo "control image smoke test passed"
