# SPDX-License-Identifier: AGPL-3.0-only
"""Read dump and snapshot metadata hourly; never change a recovery point.

The separate read-only key comes from Infisical at startup. Provider
responses and exception text never enter logs. The deployment key is not
used here. The monitor reads the configured instance in the Paris region.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

logger = logging.getLogger(__name__)
MAX_AGE = timedelta(hours=30)
PAGE_SIZE = 100
MAX_PAGES = 5
MAX_BYTES = 1024 * 1024
type RecoveryKind = Literal["backups", "snapshots"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Missing backup timestamp")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Backup timestamp has no time zone")
    return result


@dataclass(frozen=True)
class BackupCheck:
    instance: str
    database: str
    key: str = field(repr=False)

    def read_page(self, page: int, kind: RecoveryKind = "backups") -> dict[str, Any]:
        # Validate before constructing a URL. Neither a setting nor a server
        # response can choose another host/path to receive this key.
        if str(UUID(self.instance)) != self.instance:
            raise ValueError("Invalid database instance")
        if kind not in ("backups", "snapshots"):
            raise ValueError("Invalid recovery-point kind")
        query = urllib.parse.urlencode(
            {"instance_id": self.instance, "page_size": PAGE_SIZE, "page": page}
        )
        request = urllib.request.Request(
            f"https://api.scaleway.com/rdb/v1/regions/fr-par/{kind}?" + query,
            headers={"X-Auth-Token": self.key},
            method="GET",
        )
        # No redirects or environment proxies may receive the credential.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )
        try:
            response = opener.open(request, timeout=8)
        except urllib.error.HTTPError as error:
            error.close()
            raise ValueError("Backup read refused") from None
        with response:
            if response.status != 200:
                raise ValueError("Backup read failed")
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Backup response too large")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Invalid backup response")
        return result

    def newest(self, kind: RecoveryKind, now: datetime) -> datetime | None:
        """Require a complete listing of ready, nonempty, unexpired points.

        Dumps protect one database; snapshots protect the whole instance.
        Both listings must be readable and complete, even when the first has
        a good point. A metadata failure must not become a successful check.
        """
        newest: datetime | None = None
        seen: set[str] = set()
        total: int | None = None
        for page in range(1, MAX_PAGES + 1):
            result = self.read_page(page, kind)
            rows = result["database_backups" if kind == "backups" else "snapshots"]
            count = result["total_count"]
            if (
                type(count) is not int
                or count < 0
                or not isinstance(rows, list)
                or len(rows) > PAGE_SIZE
                or (total is not None and total != count)
            ):
                raise ValueError("Incomplete backup listing")
            total = count
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Invalid backup metadata")
                identity = row["id"]
                if not isinstance(identity, str) or not identity or identity in seen:
                    raise ValueError("Duplicate or missing backup identity")
                seen.add(identity)
                if row["instance_id"] != self.instance or row["status"] != "ready":
                    continue
                if kind == "backups" and row["database_name"] != self.database:
                    continue
                created = timestamp(row["created_at"])
                expires = timestamp(row["expires_at"])
                if created > now or expires <= created:
                    raise ValueError("Invalid backup dates")
                size = row["size"]
                if type(size) is not int or size < 0:
                    raise ValueError("Invalid backup size")
                if expires <= now or size == 0:
                    continue
                newest = max(newest, created) if newest else created
            if len(seen) == total:
                break
            if len(rows) != PAGE_SIZE or len(seen) > total:
                raise ValueError("Incomplete backup listing")
        else:
            raise ValueError("Backup listing limit reached")
        return newest

    def assess(self, now: datetime) -> str:
        if not self.key:
            return "failed: credential missing"
        points = [self.newest(kind, now) for kind in ("backups", "snapshots")]
        usable = [point for point in points if point is not None]
        if not usable:
            return "failed: no usable backup"
        age = now - max(usable)
        if age > MAX_AGE:
            return "failed: newest backup older than 30 hours"
        return f"ok: newest backup age {int(age.total_seconds())} seconds"

    def run(self) -> None:
        try:
            outcome = self.assess(datetime.now(UTC))
        except Exception:  # noqa: BLE001 -- no provider or credential text in logs
            outcome = "failed: metadata read"
        if outcome.startswith("ok:"):
            logger.info("backup check: %s", outcome)
        else:
            logger.error("backup check: %s", outcome)
