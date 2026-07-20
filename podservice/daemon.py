"""Main daemon service."""

import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from .config import ServiceConfig, load_config
from .downloader import MediaDownloader
from .episodes import EpisodeService
from .events import (
    PARTIAL_UPLOAD_SUFFIX,
    SOURCE_TYPE_FILE,
    SOURCE_TYPE_URL,
    KafkaLifecyclePublisher,
    KafkaProjectionConsumer,
    KafkaStatusProbe,
    KafkaTopicManager,
    LifecycleEvent,
    LifecycleEventStore,
)
from .feed import PodcastFeed
from .messaging import (
    DownloadJob,
    MessagePublishError,
    PartialPublishError,
    RabbitMQConsumer,
    RabbitMQPublisher,
)
from .server import PodcastServer
from .status import RabbitMQStatusProbe

logger = logging.getLogger(__name__)


class PodService:
    """Main pod service daemon."""

    def __init__(self, config: ServiceConfig):
        self.config = config
        self.running = False

        # Ensure directories exist
        Path(self.config.storage.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.config.storage.audio_dir).mkdir(parents=True, exist_ok=True)

        # Create metadata directory
        self.metadata_dir = Path(self.config.storage.metadata_dir)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)

        # Initialize components
        self.feed = PodcastFeed(
            title=self.config.podcast.title,
            description=self.config.podcast.description,
            author=self.config.podcast.author,
            base_url=self.config.server.base_url,
            language=self.config.podcast.language,
            category=self.config.podcast.category,
            image_url=self.config.podcast.image_url,
        )

        # Load existing episodes from metadata
        self.feed.load_episodes_from_metadata(
            str(self.metadata_dir),
            audio_dir=self.config.storage.audio_dir,
            thumbnails_dir=self.config.storage.thumbnails_dir,
        )

        # Initialize downloader
        self.downloader = MediaDownloader(
            output_dir=self.config.storage.audio_dir,
            base_url=self.config.server.base_url,
            metadata_dir=str(self.metadata_dir),
            thumbnails_dir=self.config.storage.thumbnails_dir,
        )

        self.episode_service = EpisodeService(self.downloader, self.feed)
        self.publisher = RabbitMQPublisher(self.config.rabbitmq)
        self.event_store = LifecycleEventStore(
            Path(self.config.storage.data_dir) / "db" / "podservice.sqlite3"
        )
        self.kafka_topic_manager = KafkaTopicManager(self.config.kafka)
        self.kafka_publisher = KafkaLifecyclePublisher(
            self.config.kafka,
            self.kafka_topic_manager,
            self.event_store,
        )
        self.kafka_projection = KafkaProjectionConsumer(
            self.config.kafka,
            self.kafka_topic_manager,
            self.event_store,
        )
        self.consumer = RabbitMQConsumer(
            self.config.rabbitmq,
            self.episode_service.process_download,
            lifecycle_handler=self._emit_lifecycle,
        )
        self.rabbitmq_status = RabbitMQStatusProbe(self.config.rabbitmq)
        self.kafka_status = KafkaStatusProbe(self.config.kafka, self.event_store)
        self.server = PodcastServer(
            self.config,
            self.feed,
            submit_urls=self.submit_urls,
            rabbitmq_status=self.rabbitmq_status.snapshot,
            kafka_status=self.kafka_status.snapshot,
            database_status=self.event_store.status,
            recent_events=self.event_store.recent,
            emit_upload_event=self._emit_upload_lifecycle,
        )

        # Set up signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info(f"Received signal {signum}, shutting down...")
        self.stop()

    def start(self):
        """Start the service daemon."""
        logger.info("Starting Pod Service...")
        logger.info("Configuration:")
        logger.info(f"  Server: {self.config.server.base_url}")
        logger.info(f"  Port: {self.config.server.port}")
        logger.info(f"  Audio directory: {self.config.storage.audio_dir}")
        logger.info(
            "  RabbitMQ: %s:%s/%s",
            self.config.rabbitmq.host,
            self.config.rabbitmq.port,
            self.config.rabbitmq.queue,
        )
        logger.info(f"  Podcast: {self.config.podcast.title}")

        self.running = True

        try:
            self.kafka_publisher.start()
            self.kafka_projection.start()
            self._cleanup_partial_uploads()
            self._migrate_legacy_urls()
            self.consumer.start()
            self.server.start()
            logger.info("Service running")
            while self.running:
                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Service interrupted by user")
        except Exception as e:
            logger.error(f"Service error: {e}", exc_info=True)
        finally:
            self.cleanup()

    def stop(self):
        """Stop the service daemon."""
        self.running = False

    def cleanup(self):
        """Cleanup resources."""
        logger.info("Cleaning up...")

        self.server.stop()
        self.publisher.close()
        self.consumer.stop()
        self.kafka_projection.stop()
        self.kafka_publisher.close()

        logger.info("Pod Service stopped")

    def submit_urls(self, urls: list[str]) -> list[DownloadJob]:
        try:
            jobs = self.publisher.publish_urls(urls)
        except PartialPublishError as exc:
            for job in exc.accepted_jobs:
                self._emit_lifecycle("download.requested", job)
            raise
        for job in jobs:
            self._emit_lifecycle("download.requested", job)
        return jobs

    def _emit_lifecycle(
        self,
        event_type: str,
        job: DownloadJob,
        detail: Optional[str] = None,
    ) -> None:
        self._emit_event(
            event_type=event_type,
            job_id=job.job_id,
            source=job.url,
            attempt=job.attempt,
            source_type=SOURCE_TYPE_URL,
            detail=detail,
        )

    def _emit_upload_lifecycle(
        self,
        event_type: str,
        job_id: str,
        filename: str,
        batch_id: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        self._emit_event(
            event_type=event_type,
            job_id=job_id,
            source=filename,
            attempt=0,
            source_type=SOURCE_TYPE_FILE,
            batch_id=batch_id,
            detail=detail,
        )

    def _emit_event(
        self,
        event_type: str,
        job_id: str,
        source: str,
        attempt: int,
        source_type: str,
        batch_id: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        try:
            self.kafka_publisher.publish(
                LifecycleEvent.create(
                    event_type=event_type,
                    job_id=job_id,
                    source=source,
                    attempt=attempt,
                    source_type=source_type,
                    batch_id=batch_id,
                    detail=detail,
                )
            )
        except Exception:
            logger.exception(
                "Unable to emit Kafka lifecycle event %s for job %s",
                event_type,
                job_id,
            )

    def _cleanup_partial_uploads(self) -> None:
        """Discard uploads interrupted by a previous shutdown.

        A .partial file is a truncated fragment, never a recoverable episode.
        """
        audio_dir = Path(self.config.storage.audio_dir)
        if not audio_dir.is_dir():
            return
        for partial in audio_dir.glob(f"*{PARTIAL_UPLOAD_SUFFIX}"):
            try:
                partial.unlink()
                logger.info("Removed interrupted upload: %s", partial.name)
            except OSError as exc:
                logger.warning("Unable to remove %s: %s", partial.name, exc)

    def _migrate_legacy_urls(self) -> None:
        if not self.config.legacy_urls_file:
            return
        path = Path(self.config.legacy_urls_file)
        if not path.exists():
            return
        pending = [
            line.strip() for line in path.read_text().splitlines() if line.strip()
        ]
        valid = [
            url
            for url in pending
            if url.startswith("http://") or url.startswith("https://")
        ]
        invalid = [url for url in pending if url not in valid]
        if not valid:
            return
        try:
            migrated = len(self.submit_urls(valid))
            remaining = invalid
        except PartialPublishError as exc:
            migrated = len(exc.accepted_jobs)
            remaining = invalid + [job.url for job in exc.unaccepted_jobs]
        except MessagePublishError:
            logger.warning(
                "Legacy URL migration deferred because RabbitMQ is unavailable"
            )
            return
        temporary = path.with_name(f".{path.name}.migrating")
        temporary.write_text("".join(f"{url}\n" for url in remaining))
        temporary.replace(path)
        logger.info(
            "Migrated %s legacy URL(s) into RabbitMQ",
            migrated,
        )


def run_service(config_path: str = None, foreground: bool = True):
    """Run the pod service daemon."""
    try:
        config = load_config(config_path)
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        sys.exit(1)

    # Set up logging
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # Configure stdout handler for INFO and below
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.setFormatter(logging.Formatter(log_format))
    stdout_handler.addFilter(lambda record: record.levelno <= logging.INFO)

    # Configure stderr handler for WARNING and above
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(logging.Formatter(log_format))

    handlers = [stdout_handler, stderr_handler]

    logging.basicConfig(level=log_level, format=log_format, handlers=handlers)

    logger.info(
        f"Pod Service starting in {'foreground' if foreground else 'daemon'} mode"
    )

    # Create and start service
    service = PodService(config)
    service.start()
