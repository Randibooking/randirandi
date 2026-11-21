# SPDX-License-Identifier: AGPL-3.0-only
# Caddy with the product's Caddyfile, running as a user that is not root.

ARG CADDY_IMAGE

FROM ${CADDY_IMAGE}
RUN addgroup -S -g 10002 caddy \
    && adduser -S -u 10002 -G caddy -H caddy \
    && chown -R caddy:caddy /data /config
COPY Caddyfile /etc/caddy/Caddyfile
USER 10002:10002
