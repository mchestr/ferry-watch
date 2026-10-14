# syntax=docker/dockerfile:1

# ---- frontend
FROM node:22.11.0-alpine@sha256:b64ced2e7cd0a4816699fe308ce6e8a08ccba463c757c00c14cd372e3d2c763e AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- runtime
# Playwright's image ships Chromium and its system libraries. Its version must match the
# `playwright` package in uv.lock (CI checks this).
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble@sha256:a7f6cf3ae520c9d670ad956572c13747ed5abdbba5123a01526f873ed1662528

# KasmVNC is the virtual display: an X server with a built-in web viewer that resizes the
# screen to fit whoever is looking (phone or desktop). matchbox keeps the browser window
# full-screen at that size. tini forwards signals so the app can shut down cleanly.
ARG TARGETARCH
ARG KASMVNC_VERSION=1.5.0
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) sum=f599fe02e2175b9817b6165f74a5d2bebdc73118dde9181ba3410963bed7ae1e ;; \
      arm64) sum=c9199cf4753208bfb69fd016a9780242bebfc43370cc38c97d61e90a3c783e04 ;; \
      *) echo "unsupported architecture ${TARGETARCH}"; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl matchbox-window-manager tini; \
    curl -fsSL -o /tmp/kasmvnc.deb \
      "https://github.com/kasmtech/KasmVNC/releases/download/v${KASMVNC_VERSION}/kasmvncserver_noble_${KASMVNC_VERSION}_${TARGETARCH}.deb"; \
    echo "${sum}  /tmp/kasmvnc.deb" | sha256sum -c -; \
    apt-get install -y --no-install-recommends /tmp/kasmvnc.deb; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /tmp/kasmvnc.deb /var/lib/apt/lists/*; \
    # Serve KasmVNC's viewer under /vnc so it shares a hostname with the app.
    mkdir -p /opt/vnc-web /config; \
    ln -s /usr/share/kasmvnc/www /opt/vnc-web/vnc; \
    chown pwuser:pwuser /config

COPY --from=ghcr.io/astral-sh/uv:0.7.8@sha256:0178a92d156b6f6dbe60e3b52b33b421021f46d634aa9f81f42b91445bb81cdf /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --extra browser --no-install-project
COPY src/ src/
RUN uv sync --locked --no-dev --extra browser --no-editable && rm /usr/local/bin/uv

COPY --from=web /web/dist /app/static
COPY entrypoint.sh /app/entrypoint.sh

ENV BCF_HOST=0.0.0.0 \
    BCF_PORT=8765 \
    BCF_CONFIG=/config/config.json \
    BCF_STATIC=/app/static \
    DISPLAY=:99 \
    PYTHONUNBUFFERED=1

USER pwuser
EXPOSE 8765 6080
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/app/entrypoint.sh"]
