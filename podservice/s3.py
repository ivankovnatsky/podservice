"""Mirror the podcast feed and its media to S3-compatible storage (e.g. R2).

Every write is gated by free-tier guardrails: the bucket (and, with Cloudflare
analytics configured, the whole account) must stay under the storage limit,
and the class A/B operation budgets must not be exhausted. Older episodes are
evicted from the mirror rather than letting it grow past the limit.
"""

import json
import logging
import mimetypes
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from .cloudflare import CloudflareAnalyticsError, CloudflareR2Analytics, R2AccountUsage
from .config import S3Config
from .feed import Episode, PodcastFeed

logger = logging.getLogger(__name__)

FEED_KEY = "feed.xml"
MEDIA_DIRS = ("audio/", "thumbnails/")
CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
FEED_CACHE_CONTROL = "public, max-age=300"
MEDIA_CACHE_CONTROL = "public, max-age=86400"
# Uploads go up as a single PutObject (one class A operation) instead of
# multipart; R2 caps a single PUT at 5 GiB.
SINGLE_PUT_LIMIT = 4 * 1024**3
LEDGER_WINDOW_DAYS = 31


class BudgetExceeded(RuntimeError):
    """A free-tier limit is reached; the mirror must not write."""


def _read_secret(path: str) -> str:
    return Path(path).read_text().strip()


def make_client(config: S3Config):
    import boto3
    from botocore.config import Config

    credentials = {}
    if config.access_key_id_file:
        credentials = {
            "aws_access_key_id": _read_secret(config.access_key_id_file),
            "aws_secret_access_key": _read_secret(config.secret_access_key_file),
        }
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        region_name=config.region,
        config=Config(
            # R2 rejects some of the default flexible checksums newer botocore sends.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            # Every request is charged to the ledger before it is sent; silent
            # botocore retries would send requests the ledger never sees.
            retries={"mode": "standard", "total_max_attempts": 1},
        ),
        **credentials,
    )


def _transfer_config():
    from boto3.s3.transfer import TransferConfig

    return TransferConfig(multipart_threshold=SINGLE_PUT_LIMIT + 1)


def _content_type(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return (
        CONTENT_TYPES.get(suffix)
        or mimetypes.guess_type(name)[0]
        or "application/octet-stream"
    )


class OperationLedger:
    """Write-ahead count of class A operations this service performed.

    Counts are kept per UTC day over a rolling window that covers any
    billing period, and persisted before each request is sent, so a crash
    or restart can never lose a charge.
    """

    def __init__(self, path: Optional[Path], limit: int):
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()
        self._days: dict[str, int] = {}
        self.error: Optional[str] = None
        if path is not None and path.exists():
            try:
                self._days = {
                    str(k): int(v) for k, v in json.loads(path.read_text()).items()
                }
            except (ValueError, TypeError, AttributeError, OSError) as exc:
                # Past charges are unknown, so nothing may be sent until the
                # file is repaired or removed.
                self.error = f"operation ledger {path} is unreadable: {exc}"
                logger.error("%s; S3 mirror will not write", self.error)

    def used(self, today: Optional[date] = None) -> int:
        with self._lock:
            self._prune(today)
            return sum(self._days.values())

    def remaining(self) -> int:
        if self.error:
            return 0
        return max(0, self.limit - self.used())

    def charge(self, count: int = 1, today: Optional[date] = None) -> None:
        with self._lock:
            if self.error:
                raise BudgetExceeded(self.error)
            self._prune(today)
            used = sum(self._days.values())
            if used + count > self.limit:
                raise BudgetExceeded(
                    f"class A operation budget exhausted: {used} used of "
                    f"{self.limit} in the last {LEDGER_WINDOW_DAYS} days"
                )
            key = (today or datetime.now(timezone.utc).date()).isoformat()
            self._days[key] = self._days.get(key, 0) + count
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    f.write(json.dumps(self._days, sort_keys=True))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)

    def _prune(self, today: Optional[date]) -> None:
        cutoff = (today or datetime.now(timezone.utc).date()) - timedelta(
            days=LEDGER_WINDOW_DAYS
        )
        self._days = {k: v for k, v in self._days.items() if k >= cutoff.isoformat()}


@dataclass
class MirrorStatus:
    public_feed_url: str
    storage_limit_bytes: int
    class_a_limit: int
    class_b_limit: int
    class_a_used_local: int = 0
    paused: Optional[str] = None
    last_sync: Optional[str] = None
    last_error: Optional[str] = None
    bucket_bytes: Optional[int] = None
    account_bytes: Optional[int] = None
    class_a_used_account: Optional[int] = None
    class_b_used_account: Optional[int] = None
    mirrored_episodes: int = 0
    evicted_episodes: int = 0
    evicted_titles: list[str] = field(default_factory=list)


@dataclass
class _PlannedEpisode:
    episode: Episode
    objects: dict[str, Path]
    size: int


class S3Mirror:
    """Keep a bucket prefix in sync with the local feed.

    The local library is the source of truth: each sync uploads missing
    audio/thumbnails, rewrites feed.xml with public URLs, then deletes
    audio/thumbnail objects no longer referenced. Syncs run on a background
    thread and coalesce, so callers never wait on uploads.
    """

    def __init__(
        self,
        config: S3Config,
        feed: PodcastFeed,
        thumbnails_dir: str,
        client=None,
        retry_delay: float = 60,
        budget_retry_delay: float = 3600,
        ledger_path: Optional[Path] = None,
        analytics: Optional[CloudflareR2Analytics] = None,
    ):
        self.config = config
        self.feed = feed
        self.thumbnails_dir = Path(thumbnails_dir)
        self.client = client
        self.retry_delay = retry_delay
        self.budget_retry_delay = budget_retry_delay
        self.ledger = OperationLedger(ledger_path, config.max_class_a_operations)
        self.analytics = analytics
        if self.analytics is None and config.analytics_enabled:
            self.analytics = CloudflareR2Analytics(
                config.cloudflare_account_id, config.cloudflare_api_token_file
            )
        self._status_lock = threading.Lock()
        self._status = MirrorStatus(
            public_feed_url=self.feed_url,
            storage_limit_bytes=config.max_storage_bytes,
            class_a_limit=config.max_class_a_operations,
            class_b_limit=config.max_class_b_operations,
        )
        self._last_feed_xml: Optional[bytes] = None
        self._pending = threading.Event()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def feed_url(self) -> str:
        return f"{self.config.public_prefix_url}/{FEED_KEY}"

    def status(self) -> MirrorStatus:
        with self._status_lock:
            self._status.class_a_used_local = self.ledger.used()
            return MirrorStatus(**vars(self._status))

    def start(self) -> None:
        if self.client is None:
            self.client = make_client(self.config)
        self.feed.add_listener(self.request_sync)
        self._thread = threading.Thread(target=self._run, name="s3-mirror", daemon=True)
        self._thread.start()
        self.request_sync()
        logger.info("S3 mirror enabled, public feed: %s", self.feed_url)

    def stop(self) -> None:
        self._stopping.set()
        self._pending.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def request_sync(self) -> None:
        self._pending.set()

    def _run(self) -> None:
        while True:
            self._pending.wait()
            if self._stopping.is_set():
                return
            self._pending.clear()
            try:
                self.sync()
            except BudgetExceeded as exc:
                logger.error(
                    "S3 mirror paused, not writing to the bucket: %s (retrying in %ss)",
                    exc,
                    self.budget_retry_delay,
                )
                self._set_status(paused=str(exc), last_error=str(exc))
                if self._stopping.wait(self.budget_retry_delay):
                    return
                self._pending.set()
            except Exception as exc:
                logger.exception("S3 sync failed, retrying in %ss", self.retry_delay)
                self._set_status(paused=None, last_error=str(exc))
                if self._stopping.wait(self.retry_delay):
                    return
                self._pending.set()

    def _set_status(self, **changes) -> None:
        with self._status_lock:
            for key, value in changes.items():
                setattr(self._status, key, value)

    def sync(self) -> None:
        # Cheapest checks first: nothing below costs an operation until the
        # budgets are known to have room.
        if self.ledger.remaining() == 0:
            raise BudgetExceeded(
                self.ledger.error
                or f"class A operation budget exhausted: {self.ledger.used()} used "
                f"of {self.ledger.limit} in the last {LEDGER_WINDOW_DAYS} days"
            )
        account = self._account_usage()

        prefix = self.config.key_prefix
        objects = self._list_objects()
        bucket_bytes = sum(objects.values())
        media_prefixes = tuple(f"{prefix}{d}" for d in MEDIA_DIRS)
        remote_media = {
            key: size for key, size in objects.items() if key.startswith(media_prefixes)
        }
        self._set_status(bucket_bytes=bucket_bytes)

        # Everything we do not manage counts against the budget before our
        # media does: other keys in the bucket and, account-wide, other buckets.
        reserved = bucket_bytes - sum(remote_media.values())
        if account is not None:
            # Both figures come from the same (lagging) analytics sample; mixing
            # it with the live listing would misattribute our own growth.
            outside = account.storage_outside(self.config.bucket)
            if outside is None:
                # No sample for this bucket yet, so the account total is
                # entirely other buckets.
                outside = account.storage_bytes
            reserved += max(0, outside)
        budget = self.config.max_storage_bytes - reserved
        if budget <= 0:
            raise BudgetExceeded(
                f"storage limit {self.config.max_storage_bytes} bytes is already "
                f"used by objects outside the mirror ({reserved} bytes)"
            )

        episodes = sorted(self.feed.episodes, key=lambda ep: ep.pub_date, reverse=True)
        if not episodes and remote_media:
            # An unmounted or unreadable library also yields an empty feed;
            # never let that wipe the bucket.
            logger.warning(
                "Local feed is empty but %s remote media object(s) exist; "
                "skipping S3 sync",
                len(remote_media),
            )
            return

        planned, evicted = self._plan(episodes, remote_media, budget)
        if evicted:
            logger.warning(
                "Storage limit reached: %s of %s episode(s) kept in the mirror, "
                "oldest %s left out: %s",
                len(planned),
                len(episodes),
                len(evicted),
                ", ".join(ep.title for ep in evicted[:5]),
            )

        wanted_keys = {key for item in planned for key in item.objects}
        orphans = [key for key in remote_media if key not in wanted_keys]
        upload_bytes = sum(
            item.size
            for item in planned
            if any(k not in remote_media for k in item.objects)
        )
        # Orphans normally go after the feed so it never references a missing
        # object, but when old plus new media would exceed the limit, even for
        # a moment, the evicted objects must go first.
        deleted = 0
        if sum(remote_media.values()) + upload_bytes > budget:
            self._delete(orphans)
            deleted, orphans = len(orphans), []

        uploaded = 0
        for item in planned:
            for key, path in item.objects.items():
                if key in remote_media:
                    continue
                self.ledger.charge(1)
                self.client.upload_file(
                    str(path),
                    self.config.bucket,
                    key,
                    ExtraArgs={
                        "ContentType": _content_type(path.name),
                        "CacheControl": MEDIA_CACHE_CONTROL,
                    },
                    Config=_transfer_config(),
                )
                uploaded += 1
                logger.info("Uploaded %s", key)

        # Feed goes up after its media and before orphans are removed, so it
        # never references a missing object.
        feed_xml = self.feed.generate_xml(
            media_base_url=self.config.public_prefix_url,
            episodes=[item.episode for item in planned],
        ).encode("utf-8")
        if feed_xml != self._last_feed_xml or uploaded:
            self.ledger.charge(1)
            self.client.put_object(
                Bucket=self.config.bucket,
                Key=f"{prefix}{FEED_KEY}",
                Body=feed_xml,
                ContentType="application/rss+xml; charset=utf-8",
                CacheControl=FEED_CACHE_CONTROL,
            )
            self._last_feed_xml = feed_xml

        self._delete(orphans)
        deleted += len(orphans)

        self._set_status(
            paused=None,
            last_error=None,
            last_sync=datetime.now(timezone.utc).isoformat(),
            mirrored_episodes=len(planned),
            evicted_episodes=len(evicted),
            evicted_titles=[ep.title for ep in evicted],
        )
        logger.info(
            "S3 sync done: %s uploaded, %s deleted, %s of %s episodes mirrored, "
            "%s class A ops used of %s",
            uploaded,
            deleted,
            len(planned),
            len(episodes),
            self.ledger.used(),
            self.ledger.limit,
        )

    def _delete(self, keys: list[str]) -> None:
        for start in range(0, len(keys), 1000):
            batch = keys[start : start + 1000]
            # DeleteObjects is not on R2's free list; count it as class A.
            self.ledger.charge(1)
            self.client.delete_objects(
                Bucket=self.config.bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
            for key in batch:
                logger.info("Deleted %s", key)

    def _account_usage(self) -> Optional[R2AccountUsage]:
        if self.analytics is None:
            return None
        try:
            usage = self.analytics.usage()
        except CloudflareAnalyticsError as exc:
            # Without a usage reading we cannot prove we are under the free
            # tier, so treat it like a limit until the API answers again.
            raise BudgetExceeded(f"Cloudflare usage unknown: {exc}") from exc
        self._set_status(
            account_bytes=usage.storage_bytes,
            class_a_used_account=usage.class_a_operations,
            class_b_used_account=usage.class_b_operations,
        )
        if usage.class_a_operations >= self.config.max_class_a_operations:
            raise BudgetExceeded(
                f"account used {usage.class_a_operations} class A operations "
                f"in the last 31 days (limit {self.config.max_class_a_operations})"
            )
        if usage.class_b_operations >= self.config.max_class_b_operations:
            # Reads come from podcast clients, not from us; writing more would
            # only attract more of them. Stopping them needs the bucket's
            # public domain disabled in the Cloudflare dashboard.
            raise BudgetExceeded(
                f"account used {usage.class_b_operations} class B operations "
                f"in the last 31 days (limit {self.config.max_class_b_operations}); "
                "disable the bucket's public domain to stop reads"
            )
        return usage

    def _plan(
        self,
        episodes: list[Episode],
        remote_media: dict[str, int],
        budget: int,
    ) -> tuple[list[_PlannedEpisode], list[Episode]]:
        """Keep the newest episodes whose media fits in the storage budget.

        Episodes are newest first; the first one that does not fit and every
        older one are evicted, so the mirror is always a contiguous window of
        the latest episodes.
        """
        prefix = self.config.key_prefix
        planned: list[_PlannedEpisode] = []
        evicted: list[Episode] = []
        used = 0
        full = False
        for episode in episodes:
            if full:
                evicted.append(episode)
                continue
            objects: dict[str, Path] = {}
            size = 0
            audio = Path(episode.audio_file)
            candidates = [(f"{prefix}audio/{audio.name}", audio)]
            thumbnail = self._thumbnail_path(episode.image_url)
            if thumbnail is not None:
                candidates.append((f"{prefix}thumbnails/{thumbnail.name}", thumbnail))
            for key, path in candidates:
                if key in remote_media:
                    object_size = remote_media[key]
                elif path.exists():
                    object_size = path.stat().st_size
                    if object_size > SINGLE_PUT_LIMIT:
                        logger.warning(
                            "Skipping %s: %s bytes exceeds single upload limit",
                            path,
                            object_size,
                        )
                        continue
                else:
                    continue
                objects[key] = path
                size += object_size
            if f"{prefix}audio/{audio.name}" not in objects:
                # No audio anywhere means nothing to publish for this episode.
                continue
            if size > budget:
                # Can never fit on its own; leave it out without closing the
                # window, or one oversized episode would empty the mirror.
                logger.warning(
                    "Skipping %s: %s bytes exceeds the whole storage budget",
                    episode.title,
                    size,
                )
                evicted.append(episode)
                continue
            if used + size > budget:
                evicted.append(episode)
                full = True
                continue
            used += size
            planned.append(_PlannedEpisode(episode, objects, size))
        return planned, evicted

    def _list_objects(self) -> dict[str, int]:
        objects: dict[str, int] = {}
        paginator = self.client.get_paginator("list_objects_v2")
        # The whole bucket is listed, not just our prefix: the storage limit
        # applies to everything in it.
        pages = paginator.paginate(Bucket=self.config.bucket)
        # Each page is one request; charge it before it is sent.
        self.ledger.charge(1)
        for page in pages:
            for item in page.get("Contents", []):
                objects[item["Key"]] = int(item.get("Size", 0))
            if page.get("IsTruncated"):
                self.ledger.charge(1)
        return objects

    def _thumbnail_path(self, image_url: str) -> Optional[Path]:
        local_prefix = f"{self.feed.base_url}/thumbnails/"
        if not image_url.startswith(local_prefix):
            return None
        name = os.path.basename(unquote(image_url[len(local_prefix) :]))
        return self.thumbnails_dir / name if name else None
