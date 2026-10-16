# syntax=docker/dockerfile:1

# Targets:
#   control  ferry-watch-control: ferry-web and ferry-control (no browser libraries at all)
#   browser  ferry-watch-browser: one browser pod (docs/multi-tenant/design-reference.md §5, §11.2)
#   app      today's single-container app, the default target until cutover

# ---- frontend
FROM node:22.11.0-alpine@sha256:b64ced2e7cd0a4816699fe308ce6e8a08ccba463c757c00c14cd372e3d2c763e AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
# The dashboard shows its version; vite.config.ts reads it from ../pyproject.toml.
COPY pyproject.toml /pyproject.toml
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.7.8@sha256:0178a92d156b6f6dbe60e3b52b33b421021f46d634aa9f81f42b91445bb81cdf AS uv

# ---- the control plane: python:3.12-slim (with tzdata, which models.py needs at import), uid 65532
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS control
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --locked --no-dev --extra control --no-install-project
COPY src/ src/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --locked --no-dev --extra control --no-editable && rm -rf src
COPY --from=web /web/dist /app/static
USER 65532:65532
EXPOSE 8765 8766 9090
CMD ["ferry-web", "run"]

# ---- the virtual display, shared by the browser pod and the single-container app
# Playwright's image ships Chromium and its system libraries. Its version must match the
# `playwright` package in uv.lock (CI checks this).
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble@sha256:a7f6cf3ae520c9d670ad956572c13747ed5abdbba5123a01526f873ed1662528 AS display

# KasmVNC is the virtual display: an X server with a built-in web viewer that resizes the
# screen to fit whoever is looking (phone or desktop). matchbox keeps the browser window
# full-screen at that size. tini forwards signals so the app can shut down cleanly. xrandr
# (x11-xserver-utils) and xsettingsd size the screen and set its scale for a streamed viewer.
ARG TARGETARCH
ARG KASMVNC_VERSION=1.5.0
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) sum=f599fe02e2175b9817b6165f74a5d2bebdc73118dde9181ba3410963bed7ae1e ;; \
      arm64) sum=c9199cf4753208bfb69fd016a9780242bebfc43370cc38c97d61e90a3c783e04 ;; \
      *) echo "unsupported architecture ${TARGETARCH}"; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl matchbox-window-manager tini xsettingsd x11-xserver-utils; \
    curl -fsSL -o /tmp/kasmvnc.deb \
      "https://github.com/kasmtech/KasmVNC/releases/download/v${KASMVNC_VERSION}/kasmvncserver_noble_${KASMVNC_VERSION}_${TARGETARCH}.deb"; \
    echo "${sum}  /tmp/kasmvnc.deb" | sha256sum -c -; \
    apt-get install -y --no-install-recommends /tmp/kasmvnc.deb; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /tmp/kasmvnc.deb /var/lib/apt/lists/*

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --locked --no-dev --extra browser --no-install-project
COPY src/ src/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --locked --no-dev --extra browser --no-editable

# ---- one browser pod: the agent supervises Xvnc (unix socket only), matchbox and Chromium (§5.1)
FROM display AS browser
# Chromium's managed policy (§5.6), root-owned and read-only, in every directory a Chromium build
# reads, so a switch to Chrome for Testing can't silently drop it. Only Chromium is kept.
# (COPY --chmod would also apply 0444 to the directories it creates, leaving them untraversable.)
RUN --mount=source=docker/browser/chromium-policy.json,target=/run/policy.json \
    for dir in /etc/chromium /etc/opt/chrome_for_testing /etc/opt/chrome; do \
      install -D -m 0444 -o root -g root /run/policy.json "${dir}/policies/managed/ferry-watch.json"; \
    done \
    && rm -rf /ms-playwright/firefox-* /ms-playwright/webkit-* /ms-playwright/ffmpeg-* /app/src
USER 1001:1001
EXPOSE 6080 8080
ENTRYPOINT ["/usr/bin/tini", "--", "ferry-watch-agent"]

# ---- today's single-container app (the default target)
FROM display AS app
# Serve KasmVNC's viewer under /vnc so it shares a hostname with the app.
RUN mkdir -p /opt/vnc-web /config \
    && ln -s /usr/share/kasmvnc/www /opt/vnc-web/vnc \
    && chown pwuser:pwuser /config
COPY --from=web /web/dist /app/static
COPY entrypoint.sh /app/entrypoint.sh

ENV BCF_HOST=0.0.0.0 \
    BCF_PORT=8765 \
    BCF_CONFIG=/config/config.json \
    BCF_STATIC=/app/static \
    DISPLAY=:99

USER pwuser
EXPOSE 8765 6080
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/app/entrypoint.sh"]
