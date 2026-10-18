#!/usr/bin/env bash
# Smoke test for the browser image (docs/multi-tenant/design-reference.md §11.3).
#   docker/browser/smoke-test.sh <image>
# Runs it as a pod would (read-only, uid 1001, no capabilities, memory /tmp and /dev/shm, the eight env
# vars) with no network at all, then checks: the probes answer; the policy is in all three directories
# and only Chromium ships; xrandr and xsettingsd (a streamed viewer's screen) are installed; the agent's
# lockdown self-test passes against a real Chromium; the agent's Xvnc shows that Chromium to a client on its
# unix socket and takes its clicks and keys; a streamed viewer's screen (a phone's size at 2x) comes and goes;
# SIGTERM ends it cleanly with one enum-only termination line.
set -euo pipefail

image="${1:?usage: smoke-test.sh <image>}"
here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
name="wl-browser-smoke-$$"
trap 'docker rm -f "$name" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT

echo "not-a-real-token" >"$work/token"
: >"$work/termination-log"
chmod 0644 "$work/token"
chmod 0666 "$work/termination-log"
chmod 0755 "$work"
pubkey="$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="))')"
trip=3f9c0a1b2c3d4e5f

docker run -d --name "$name" --network none --read-only \
  --tmpfs /tmp:size=512m,mode=1777 --tmpfs /dev/shm:size=256m \
  --cap-drop ALL --security-opt no-new-privileges --user 1001:1001 \
  -v "$work/token:/var/run/wl/token:ro" -v "$work/termination-log:/dev/termination-log" \
  -v "$here/selftest.py:/smoke/selftest.py:ro" \
  -e WL_TRIP_ID="$trip" -e WL_OWNER=uaaaaaaaaaaaaaaaaaaa -e WL_POD_NAME="wl-$trip-g1" \
  -e WL_DEADLINE_AT="$(($(date +%s) + 93600))" -e WL_HUB_URL=ws://waitlist-control.invalid:8766/agent/v1 \
  -e WL_VNC_PUBKEYS="k1:$pubkey" -e WL_VNC_ORIGIN=https://waitlist-checkout.chestr.dev -e WL_SANDBOX=0 \
  "$image" >/dev/null

probe() { docker exec "$name" python3 -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080$1', timeout=2).status)"; }
for _ in $(seq 30); do probe /livez >/dev/null 2>&1 && break; sleep 1; done
test "$(probe /livez)" = 200
test "$(probe /readyz)" = 200

docker exec "$name" sh -euc '
  for dir in /etc/chromium /etc/opt/chrome_for_testing /etc/opt/chrome; do
    test "$(stat -c "%a %u" "$dir/policies/managed/waitlist.json")" = "444 0"
  done
  test -z "$(ls -d /ms-playwright/firefox-* /ms-playwright/webkit-* 2>/dev/null)"
  test "$(id -u)" = 1001
  ! touch /etc/smoke 2>/dev/null
  for tool in xrandr xsettingsd; do command -v "$tool" >/dev/null; done
'
docker exec "$name" python3 -I /smoke/selftest.py

docker kill -s TERM "$name" >/dev/null
test "$(docker wait "$name")" = 0
line="$(cat "$work/termination-log")"
echo "termination message: $line"
[[ "$line" =~ ^[a-z_]+\ ([a-z_]+|-)$ ]]
echo "browser image smoke test passed"
