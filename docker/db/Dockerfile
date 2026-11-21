# SPDX-License-Identifier: AGPL-3.0-only
# Ship the bootstrap SQL to the daemon without a host-path bind mount.
ARG POSTGRES_IMAGE
FROM ${POSTGRES_IMAGE}
COPY roles.sql /roles.sql
