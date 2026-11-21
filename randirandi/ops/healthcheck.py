# SPDX-License-Identifier: AGPL-3.0-only
"""Asks the app's own health page, from inside its container.

Docker runs this to know whether the app is up. It speaks to the app the way
Caddy does: with a hostname the app answers to, and as HTTPS.
"""

import os
import sys
import urllib.request

URL = "http://127.0.0.1:8000/health/"


def main() -> int:
    host = os.environ["RANDIRANDI_ALLOWED_HOSTS"].split(",")[0].strip()
    if host.startswith("."):
        host = "health" + host
    request = urllib.request.Request(
        URL, headers={"Host": host, "X-Forwarded-Proto": "https"}
    )
    try:
        # A fixed address on this machine.
        with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
