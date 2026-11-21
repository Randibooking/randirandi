# SPDX-License-Identifier: AGPL-3.0-only
"""How gunicorn runs the app."""

import os

from randirandi import logs

# Inside the container. Only Caddy reaches it.
bind = "0.0.0.0:8000"
# One process with four threads. Each process holds its own copy of the
# app, about 80 MB: two of them and the process that made them left the
# sandbox's 256 MB no room for a request (M6 part 3.53).
workers = int(os.environ.get("RANDIRANDI_WEB_WORKERS", "1"))
worker_class = "gthread"
threads = 4
# The container's own memory, not a shared folder.
worker_tmp_dir = "/dev/shm"  # noqa: S108
control_socket_disable = True
# The app is loaded once, before the workers are made. The secrets are
# fetched once for each start, and a worker that is replaced while the app
# runs gets them from the process that made it, not from Infisical.
preload_app = True

# Caddy is the only caller, and it sets the forwarded headers itself.
forwarded_allow_ips = "*"

# The access log has the method, the path without its query, the status and
# the time taken. A query can hold what a person typed.
accesslog = "-"
access_log_format = "%(m)s %(U)s %(s)s %(M)sms"
logconfig_dict = logs.config(gunicorn=True)
