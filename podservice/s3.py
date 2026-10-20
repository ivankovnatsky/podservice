"""Mirror the podcast feed and its media to S3-compatible storage (e.g. R2)."""

import logging
import mimetypes
import os
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from .config import S3Config
from .feed import PodcastFeed

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
        # R2 rejects some of the default flexible checksums newer botocore sends.
        config=Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
        **credentials,
    )


def _content_type(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return (
        CONTENT_TYPES.get(suffix)
        or mimetypes.guess_type(name)[0]
        or "application/octet-stream"
    )


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
    ):
        self.config = config
        self.feed = feed
        self.thumbnails_dir = Path(thumbnails_dir)
        self.client = client
        self.retry_delay = retry_delay
        self._pending = threading.Event()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def feed_url(self) -> str:
        return f"{self.config.public_prefix_url}/{FEED_KEY}"

    def start(self) -> None:
        if self.client is None:
            self.client = make_client(self.config)
        self.feed.add_listener(self.request_sync)
        self._thread = threading.Thread(
            target=self._run, name="s3-mirror", daemon=True
        )
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
            except Exception:
                logger.exception(
                    "S3 sync failed, retrying in %ss", self.retry_delay
                )
                if self._stopping.wait(self.retry_delay):
                    return
                self._pending.set()

    def sync(self) -> None:
        prefix = self.config.key_prefix
        existing = self._list_keys(prefix)
        wanted: dict[str, Path] = {}
        for episode in list(self.feed.episodes):
            audio = Path(episode.audio_file)
            wanted[f"{prefix}audio/{audio.name}"] = audio
            thumbnail = self._thumbnail_path(episode.image_url)
            if thumbnail is not None:
                wanted[f"{prefix}thumbnails/{thumbnail.name}"] = thumbnail

        remote_media = [
            key
            for key in existing
            if any(key.startswith(f"{prefix}{d}") for d in MEDIA_DIRS)
        ]
        if not wanted and remote_media:
            # An unmounted or unreadable library also yields an empty feed;
            # never let that wipe the bucket.
            logger.warning(
                "Local feed is empty but %s remote media object(s) exist; "
                "skipping S3 sync",
                len(remote_media),
            )
            return

        uploaded = 0
        for key, path in wanted.items():
            if key in existing or not path.exists():
                continue
            self.client.upload_file(
                str(path),
                self.config.bucket,
                key,
                ExtraArgs={
                    "ContentType": _content_type(path.name),
                    "CacheControl": MEDIA_CACHE_CONTROL,
                },
            )
            uploaded += 1
            logger.info("Uploaded %s", key)

        # Feed goes up after its media and before orphans are removed, so it
        # never references a missing object.
        self.client.put_object(
            Bucket=self.config.bucket,
            Key=f"{prefix}{FEED_KEY}",
            Body=self.feed.generate_xml(
                media_base_url=self.config.public_prefix_url
            ).encode("utf-8"),
            ContentType="application/rss+xml; charset=utf-8",
            CacheControl=FEED_CACHE_CONTROL,
        )

        orphans = [key for key in remote_media if key not in wanted]
        for start in range(0, len(orphans), 1000):
            batch = orphans[start : start + 1000]
            self.client.delete_objects(
                Bucket=self.config.bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
        for key in orphans:
            logger.info("Deleted %s", key)

        logger.info(
            "S3 sync done: %s uploaded, %s deleted, %s episodes",
            uploaded,
            len(orphans),
            len(self.feed.episodes),
        )

    def _list_keys(self, prefix: str) -> set[str]:
        keys = set()
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.config.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                keys.add(item["Key"])
        return keys

    def _thumbnail_path(self, image_url: str) -> Optional[Path]:
        local_prefix = f"{self.feed.base_url}/thumbnails/"
        if not image_url.startswith(local_prefix):
            return None
        name = os.path.basename(unquote(image_url[len(local_prefix) :]))
        return self.thumbnails_dir / name if name else None
