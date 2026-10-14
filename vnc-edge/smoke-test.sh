#!/usr/bin/env bash
# Smoke test for a running vnc-edge container (docs/multi-tenant/design-reference.md §11.3).
#   vnc-edge/smoke-test.sh http://127.0.0.1:8080
# Only the expected locations answer, vnc.html loads the shim before the client, the CSP's script hash is
# the hash of the page's one inline script, and every page carries the security headers.
set -euo pipefail

base="${1:?usage: smoke-test.sh <base url>}"
trip=0123456789abcdef
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

status() { curl -s -o /dev/null -w '%{http_code}' --path-as-is "$@"; }
expect() {
  local want="$1"
  shift
  local got
  got="$(status "$@")"
  if [[ "$got" != "$want" ]]; then
    echo "FAIL: ${*: -1} answered $got, expected $want" >&2
    exit 1
  fi
}

for path in "/t/$trip/g/1/" "/t/$trip/g/123456/" /vnc.html /fw/shim.js /fw/landing.js /gone.html; do
  expect 200 "$base$path"
done
for path in / /index.html /screen.html /package.json /disconnected.html "/t/$trip/g/1" "/t/$trip/g/1234567/" \
  "/t/${trip}0/g/1/" /t/XYZ/g/1/ /fw /etc/passwd; do
  expect 404 "$base$path"
done
expect 400 "$base/fw/../../etc/passwd"
expect 400 "$base/t/$trip/g/1/ws" # not a WebSocket upgrade

curl -fsS -D "$work/headers" -o "$work/vnc.html" "$base/vnc.html"
grep -qi '^referrer-policy: no-referrer' "$work/headers"
grep -qi '^x-content-type-options: nosniff' "$work/headers"
grep -qi '^cache-control: no-store' "$work/headers"
python3 -I - "$work/headers" "$work/vnc.html" <<'EOF'
import base64, hashlib, re, sys

headers, html = (open(path, encoding="utf-8").read() for path in sys.argv[1:])
csp = re.search(r"(?im)^content-security-policy: (.*?)\r?$", headers).group(1)
scripts = re.findall(r"<script[^>]*>", html)
assert scripts[1:] == ['<script src="/fw/shim.js">', scripts[2]] and 'type="module"' in scripts[2], scripts
inline = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
digest = base64.b64encode(hashlib.sha256(inline[0].encode()).digest()).decode()
assert len(inline) == 1 and f"'sha256-{digest}'" in csp, (inline, csp)
assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp, csp
EOF
echo "vnc-edge smoke test passed"
