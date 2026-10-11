# Playwright's image ships Chromium + its system deps; the tag must match the playwright pip version.
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/web /tmp/.X11-unix /config \
    && chmod 1777 /tmp/.X11-unix \
    && ln -s /usr/share/novnc /opt/web/vnc \
    && chown pwuser:pwuser /config \
    && pip install --no-cache-dir --break-system-packages playwright==1.56.0

WORKDIR /app
COPY watch.py ui.html entrypoint.sh ./

ENV BCF_HOST=0.0.0.0 \
    BCF_PORT=8765 \
    BCF_CONFIG=/config/config.json \
    DISPLAY=:99 \
    PYTHONUNBUFFERED=1

USER pwuser
EXPOSE 8765 6080
ENTRYPOINT ["/app/entrypoint.sh"]
