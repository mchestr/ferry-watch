#!/usr/bin/env bash
# Runs the virtual display, a window manager and the app in one container.
#
# KasmVNC's Xvnc is the X server and also serves its web viewer and WebSocket on :6080
# (under /vnc). There's no password: the service must sit behind an authenticating proxy, and
# the Kubernetes NetworkPolicy only lets that proxy reach it. If any process exits, so does the
# container, and Kubernetes restarts it. tini (PID 1) forwards SIGTERM to all of them.
set -euo pipefail

export DISPLAY=:99
mkdir -p /tmp/.X11-unix

# With a read-only root filesystem, give Chromium (profile, crash reports, caches) a writable home.
if [[ ! -w "${HOME:-/nonexistent}" ]]; then
  export HOME=/tmp/home
  mkdir -p "$HOME"
fi

# -publicIP: no UDP streaming here, so don't look up the pod's public IP on the internet at startup.
Xvnc "$DISPLAY" \
  -geometry "${SCREEN_WIDTH:-1280}x${SCREEN_HEIGHT:-1000}" -depth 24 \
  -websocketPort 6080 -interface 0.0.0.0 -httpd /opt/vnc-web \
  -DisableBasicAuth -SecurityTypes None -sslOnly 0 \
  -AlwaysShared -AcceptSetDesktopSize -FrameRate 30 \
  -publicIP 127.0.0.1 \
  -Log '*:stderr:10' &

for _ in $(seq 100); do
  [[ -S /tmp/.X11-unix/X99 ]] && break
  sleep 0.1
done

# Keeps every browser window full-screen, at whatever size the viewer resizes the screen to.
matchbox-window-manager -use_titlebar no &

ferry-watch &

wait -n
exit 1
