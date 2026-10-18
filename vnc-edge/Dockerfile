# syntax=docker/dockerfile:1

# vnc-edge: the checkout host, waitlist-checkout.chestr.dev (docs/multi-tenant/design-reference.md §7.9).
# Stock nginx and a static directory: KasmVNC's web client, unmodified apart from one inserted line,
# plus the shim and landing page. No app code and no secrets.

# ---- the site, assembled at build time
FROM --platform=$BUILDPLATFORM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS site
# Must match the KasmVNC server in the browser image (CI checks this). Only the package's
# architecture-independent web client is used, so the amd64 package serves every platform.
ARG KASMVNC_VERSION=1.5.0
ARG KASMVNC_SHA256=f599fe02e2175b9817b6165f74a5d2bebdc73118dde9181ba3410963bed7ae1e
ARG CHECKOUT_HOST=waitlist-checkout.chestr.dev
ARG APP_ORIGIN=https://waitlist.chestr.dev
ADD --checksum=sha256:${KASMVNC_SHA256} \
    https://github.com/kasmtech/KasmVNC/releases/download/v${KASMVNC_VERSION}/kasmvncserver_noble_${KASMVNC_VERSION}_amd64.deb \
    /tmp/kasmvnc.deb
RUN dpkg-deb -x /tmp/kasmvnc.deb /tmp/kasmvnc
COPY build.py default.conf.in /src/
COPY site/ /src/site/
RUN python3 -I /src/build.py /tmp/kasmvnc/usr/share/kasmvnc/www /out \
      --checkout-host "${CHECKOUT_HOST}" --app-origin "${APP_ORIGIN}"

# ---- runtime
FROM nginxinc/nginx-unprivileged:1.30.4-alpine-slim@sha256:7bbe8940bc478e5618e6d4cfbeda406e5b4217cacc06faacd665aca5e468595e
COPY --from=site /out/default.conf /etc/nginx/conf.d/default.conf
COPY --from=site /out/www/ /usr/share/wl-vnc/
EXPOSE 8080
