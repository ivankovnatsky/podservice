"""Account-wide R2 usage from the Cloudflare GraphQL analytics API."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
# Longest range the R2 datasets accept; it always covers the current billing
# period, so counting a rolling window never under-reports it.
WINDOW = timedelta(days=31)

# Operation classes as billed by R2. Anything not listed is counted as class A,
# the scarcer budget, so an unknown action type can only make us more cautious.
CLASS_B_ACTIONS = frozenset(
    {
        "HeadBucket",
        "HeadObject",
        "GetObject",
        "UsageSummary",
        "GetBucketEncryption",
        "GetBucketLocation",
        "GetBucketCors",
        "GetBucketLifecycleConfiguration",
    }
)
FREE_ACTIONS = frozenset({"DeleteObject", "DeleteBucket", "AbortMultipartUpload"})

USAGE_QUERY = """
query R2Usage($account: string!, $since: Time!, $until: Time!) {
  viewer {
    accounts(filter: { accountTag: $account }) {
      operations: r2OperationsAdaptiveGroups(
        limit: 10000
        filter: { datetime_geq: $since, datetime_leq: $until }
      ) {
        sum { requests }
        dimensions { actionType }
      }
      storage: r2StorageAdaptiveGroups(
        limit: 10000
        filter: { datetime_geq: $since, datetime_leq: $until }
        orderBy: [datetime_DESC]
      ) {
        max { payloadSize metadataSize }
        dimensions { bucketName datetime }
      }
    }
  }
}
"""


class CloudflareAnalyticsError(RuntimeError):
    """The analytics API could not be queried or returned errors."""


@dataclass(frozen=True)
class R2AccountUsage:
    storage_bytes: int
    class_a_operations: int
    class_b_operations: int
    bucket_bytes: dict[str, int] = field(default_factory=dict)

    @property
    def buckets(self) -> tuple[str, ...]:
        return tuple(sorted(self.bucket_bytes))

    def storage_outside(self, bucket: str) -> Optional[int]:
        """Bytes stored in other buckets, or None if this bucket has no sample."""
        if bucket not in self.bucket_bytes:
            return None
        return self.storage_bytes - self.bucket_bytes[bucket]


class CloudflareR2Analytics:
    """Read account-wide storage and operation counts for the last 31 days."""

    def __init__(self, account_id: str, api_token_file: str, session=requests):
        self.account_id = account_id
        self.api_token_file = api_token_file
        self.session = session

    def usage(self, now: Optional[datetime] = None) -> R2AccountUsage:
        until = now or datetime.now(timezone.utc)
        since = until - WINDOW
        try:
            token = Path(self.api_token_file).read_text().strip()
            response = self.session.post(
                GRAPHQL_URL,
                json={
                    "query": USAGE_QUERY,
                    "variables": {
                        "account": self.account_id,
                        "since": _iso(since),
                        "until": _iso(until),
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
        except (OSError, requests.RequestException, ValueError) as exc:
            raise CloudflareAnalyticsError(f"analytics request failed: {exc}") from exc
        if payload.get("errors"):
            raise CloudflareAnalyticsError(
                f"analytics query errors: {payload['errors']}"
            )
        try:
            return parse_usage(payload)
        except (KeyError, TypeError, IndexError) as exc:
            raise CloudflareAnalyticsError(
                f"unexpected analytics response: {exc}"
            ) from exc


def parse_usage(payload: dict) -> R2AccountUsage:
    accounts = payload["data"]["viewer"]["accounts"]
    if not accounts:
        raise CloudflareAnalyticsError("analytics returned no account data")
    account = accounts[0]

    class_a = class_b = 0
    for group in account.get("operations") or []:
        action = group["dimensions"]["actionType"]
        requests_count = int(group["sum"]["requests"])
        if action in FREE_ACTIONS:
            continue
        if action in CLASS_B_ACTIONS:
            class_b += requests_count
        else:
            class_a += requests_count

    # Groups are newest first; the first group per bucket is its latest sample.
    latest: dict[str, int] = {}
    for group in account.get("storage") or []:
        bucket = group["dimensions"]["bucketName"]
        if bucket in latest:
            continue
        latest[bucket] = int(group["max"]["payloadSize"]) + int(
            group["max"]["metadataSize"]
        )

    return R2AccountUsage(
        storage_bytes=sum(latest.values()),
        class_a_operations=class_a,
        class_b_operations=class_b,
        bucket_bytes=latest,
    )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
