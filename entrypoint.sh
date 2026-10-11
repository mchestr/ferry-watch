#!/usr/bin/env bash
# Virtual display for the cart browser, viewable through noVNC at /vnc/vnc.html on :6080.
set -euo pipefail

export DISPLAY=:99
W="${SCREEN_WIDTH:-1280}"
H="${SCREEN_HEIGHT:-1000}"
export BCF_WINDOW_SIZE="${W},${H}"

Xvfb "$DISPLAY" -screen 0 "${W}x${H}x24" -nolisten tcp &
for _ in $(seq 50); do [[ -S /tmp/.X11-unix/X99 ]] && break; sleep 0.1; done

# VNC only listens on localhost; websockify is the only way in (behind the gateway's auth).
x11vnc -display "$DISPLAY" -forever -shared -nopw -localhost -rfbport 5900 -quiet &
websockify --web /opt/web 6080 localhost:5900 &
python3 /app/watch.py &

# If any process exits, exit so Kubernetes restarts the pod.
wait -n
exit 1
