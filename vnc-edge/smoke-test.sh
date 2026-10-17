#!/usr/bin/env bash
# Smoke test for a running vnc-edge container (docs/multi-tenant/design-reference.md §11.3).
#   vnc-edge/smoke-test.sh http://127.0.0.1:8080
# Only the expected locations answer, vnc.html loads the shim before the client, the CSP's script hash is
# the hash of the page's one inline script, and every page carries the security headers. The streamed
# viewer's page has its own policy, its modules are never cached, and vnc.html's policy is still 0.6.4's.
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

for path in "/t/$trip/g/1/" "/t/$trip/g/123456/" /vnc.html /wl/shim.js /wl/landing.js /gone.html \
  /wl/viewer.html /wl/session.js /wl/viewer.css /wl/viewer/main.js /wl/viewer/core/protocol.js; do
  expect 200 "$base$path"
done
for path in / /index.html /screen.html /package.json /disconnected.html "/t/$trip/g/1" "/t/$trip/g/1234567/" \
  "/t/${trip}0/g/1/" /t/XYZ/g/1/ /wl /etc/passwd; do
  expect 404 "$base$path"
done
expect 400 "$base/wl/../../etc/passwd"
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
assert scripts[1:] == ['<script src="/wl/shim.js">', scripts[2]] and 'type="module"' in scripts[2], scripts
inline = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
digest = base64.b64encode(hashlib.sha256(inline[0].encode()).digest()).decode()
assert len(inline) == 1 and f"'sha256-{digest}'" in csp, (inline, csp)
assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp, csp
EOF

# The streamed viewer's page has a policy and headers of its own, and its modules are never cached, so a deploy
# can't mix old modules with a new page (docs/multi-tenant/streamed-viewer/DESIGN.md §9).
curl -fsS -D "$work/viewer-headers" -o /dev/null "$base/wl/viewer.html"
grep -qi '^referrer-policy: no-referrer' "$work/viewer-headers"
grep -qi '^x-content-type-options: nosniff' "$work/viewer-headers"
grep -qi '^cache-control: no-store' "$work/viewer-headers"
for path in /wl/viewer/main.js /wl/viewer/core/protocol.js /wl/session.js /wl/viewer.css; do
  curl -fsS -D "$work/module-headers" -o /dev/null "$base$path"
  if ! grep -qi '^cache-control: no-store' "$work/module-headers"; then
    echo "FAIL: $path may be cached" >&2
    exit 1
  fi
done
curl -fsS -o "$work/shim.js" "$base/wl/shim.js"
curl -fsS -o "$work/landing.js" "$base/wl/landing.js"
curl -fsS -o "$work/session.js" "$base/wl/session.js"
python3 -I - "$work/headers" "$work/viewer-headers" "$work/session.js" "$work/shim.js" "$work/landing.js" <<'EOF'
import re, sys

classic_headers, viewer_headers, session, shim, landing = (
    open(path, encoding="utf-8").read() for path in sys.argv[1:]
)
policy = lambda headers: re.search(r"(?im)^content-security-policy: (.*?)\r?$", headers).group(1)
classic, viewer = policy(classic_headers), policy(viewer_headers)
host = re.search(r"connect-src 'self' wss://([a-z0-9.:-]+) data:", classic).group(1)
digest = re.search(r"'(sha256-[A-Za-z0-9+/=]+)'", classic).group(1)
# vnc.html's policy is 0.6.4's; the viewer's is C38.
assert classic == (
    f"default-src 'self'; script-src 'self' '{digest}' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; "
    f"img-src 'self' data: blob:; connect-src 'self' wss://{host} data:; worker-src 'self'; object-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
), classic
assert viewer == (
    f"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src wss://{host}; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; require-trusted-types-for 'script'; "
    "trusted-types 'none'"
), viewer
# Each page's script stands on its own while replicas of two versions serve (a rolling update, a rollback).
assert shim.startswith(session) and len(shim) > len(session), "wl/shim.js doesn't start with wl/session.js"
assert landing.startswith(session) and len(landing) > len(session), "wl/landing.js doesn't start with wl/session.js"
EOF
echo "vnc-edge smoke test passed"
