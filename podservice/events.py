"""Kafka lifecycle events and the local dashboard projection."""

import json
import logging
import sqlite3
import threading
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union
from uuid import uuid4

from kafka import KafkaAdminClient, KafkaConsumer, KafkaProducer, TopicPartition
from kafka.admin import NewTopic
from kafka.errors import KafkaError, TopicAlreadyExistsError

from .config import KafkaConfig

logger = logging.getLogger(__name__)


PARTIAL_UPLOAD_SUFFIX = ".partial"

# A fragment older than this cannot belong to an upload still in flight, so it
# is safe to discard while the service is running.
PARTIAL_UPLOAD_STALE_SECONDS = 3600

SOURCE_TYPE_URL = "url"
SOURCE_TYPE_FILE = "file"
SOURCE_TYPES = frozenset({SOURCE_TYPE_URL, SOURCE_TYPE_FILE})


@dataclass(frozen=True)
class LifecycleEvent:
    """An ingestion lifecycle fact published to Kafka."""

    event_id: str
    event_type: str
    occurred_at: str
    job_id: str
    source: str
    attempt: int
    source_type: str = SOURCE_TYPE_URL
    batch_id: Optional[str] = None
    detail: Optional[str] = None

    @classmethod
    def create(
        cls,
        event_type: str,
        job_id: str,
        source: str,
        attempt: int,
        source_type: str = SOURCE_TYPE_URL,
        batch_id: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> "LifecycleEvent":
        return cls(
            event_id=str(uuid4()),
            event_type=event_type,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            job_id=job_id,
            source=source,
            attempt=attempt,
            source_type=source_type,
            batch_id=batch_id,
            detail=detail,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "LifecycleEvent":
        if not isinstance(data, dict):
            raise ValueError("Lifecycle event must be an object")
        # Events produced before the source rename are still in the topic.
        if "url" in data and "source" not in data:
            data = {**data, "source": data["url"]}
            data.pop("url")
        try:
            event = cls(**data)
        except TypeError as exc:
            raise ValueError("Invalid lifecycle event") from exc
        if (
            not all(
                isinstance(value, str) and value
                for value in (
                    event.event_id,
                    event.event_type,
                    event.occurred_at,
                    event.job_id,
                    event.source,
                )
            )
            or event.source_type not in SOURCE_TYPES
            or not isinstance(event.attempt, int)
            or isinstance(event.attempt, bool)
            or event.attempt < 0
            or (event.batch_id is not None and not isinstance(event.batch_id, str))
            or (event.detail is not None and not isinstance(event.detail, str))
        ):
            raise ValueError("Invalid lifecycle event")
        return event


@dataclass(frozen=True)
class DatabaseStatus:
    connected: bool
    path: str
    size_bytes: int = 0
    event_count: int = 0
    outbox_pending: int = 0
    last_event_at: Optional[str] = None
    error: Optional[str] = None


class LifecycleEventStore:
    """Idempotent SQLite projection of Kafka lifecycle events."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS lifecycle_events (
                        event_id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        job_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        source_type TEXT NOT NULL DEFAULT 'url',
                        batch_id TEXT,
                        detail TEXT,
                        kafka_published_at TEXT
                    )
                    """
                )
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(lifecycle_events)")
                }
                if "kafka_published_at" not in columns:
                    connection.execute(
                        "ALTER TABLE lifecycle_events "
                        "ADD COLUMN kafka_published_at TEXT"
                    )
                    connection.execute(
                        "UPDATE lifecycle_events SET kafka_published_at = occurred_at"
                    )
                if "source" not in columns and "url" in columns:
                    connection.execute(
                        "ALTER TABLE lifecycle_events RENAME COLUMN url TO source"
                    )
                if "source_type" not in columns:
                    connection.execute(
                        "ALTER TABLE lifecycle_events "
                        "ADD COLUMN source_type TEXT NOT NULL DEFAULT 'url'"
                    )
                if "batch_id" not in columns:
                    connection.execute(
                        "ALTER TABLE lifecycle_events ADD COLUMN batch_id TEXT"
                    )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS lifecycle_events_occurred_at
                    ON lifecycle_events (occurred_at DESC)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS lifecycle_events_outbox
                    ON lifecycle_events (kafka_published_at, occurred_at)
                    """
                )

    def append(self, event: LifecycleEvent, kafka_published: bool = False) -> None:
        published_at = (
            datetime.now(timezone.utc).isoformat() if kafka_published else None
        )
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO lifecycle_events (
                        event_id, event_type, occurred_at, job_id, source, attempt,
                        source_type, batch_id, detail, kafka_published_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.event_type,
                        event.occurred_at,
                        event.job_id,
                        event.source,
                        event.attempt,
                        event.source_type,
                        event.batch_id,
                        event.detail,
                        published_at,
                    ),
                )
                if kafka_published:
                    connection.execute(
                        """
                        UPDATE lifecycle_events
                        SET kafka_published_at = COALESCE(kafka_published_at, ?)
                        WHERE event_id = ?
                        """,
                        (published_at, event.event_id),
                    )

    def pending(self, limit: int = 100) -> list[LifecycleEvent]:
        safe_limit = max(1, min(limit, 1000))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT event_id, event_type, occurred_at, job_id, source, attempt,
                       source_type, batch_id, detail
                FROM lifecycle_events
                WHERE kafka_published_at IS NULL
                ORDER BY occurred_at, event_id
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [LifecycleEvent(**dict(row)) for row in rows]

    def mark_published(self, event_id: str) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE lifecycle_events
                    SET kafka_published_at = ?
                    WHERE event_id = ?
                    """,
                    (datetime.now(timezone.utc).isoformat(), event_id),
                )

    def pending_count(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM lifecycle_events
                WHERE kafka_published_at IS NULL
                """
            ).fetchone()
        return int(row["count"])

    def status(self) -> DatabaseStatus:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS event_count,
                        SUM(CASE WHEN kafka_published_at IS NULL THEN 1 ELSE 0 END)
                            AS outbox_pending,
                        MAX(occurred_at) AS last_event_at
                    FROM lifecycle_events
                    """
                ).fetchone()
            related_files = [
                self.path,
                Path(f"{self.path}-wal"),
                Path(f"{self.path}-shm"),
            ]
            size_bytes = sum(
                path.stat().st_size for path in related_files if path.exists()
            )
            return DatabaseStatus(
                connected=True,
                path=str(self.path),
                size_bytes=size_bytes,
                event_count=int(row["event_count"]),
                outbox_pending=int(row["outbox_pending"] or 0),
                last_event_at=row["last_event_at"],
            )
        except (OSError, sqlite3.Error) as exc:
            logger.warning("SQLite status probe failed: %s", exc)
            return DatabaseStatus(
                connected=False,
                path=str(self.path),
                error="SQLite database is unavailable",
            )

    def recent(self, limit: int = 50) -> list[LifecycleEvent]:
        safe_limit = max(1, min(limit, 200))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT event_id, event_type, occurred_at, job_id, source, attempt,
                       source_type, batch_id, detail
                FROM lifecycle_events
                ORDER BY occurred_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [LifecycleEvent(**dict(row)) for row in rows]


class KafkaTopicManager:
    """Create the lifecycle topic before producers and consumers use it."""

    def __init__(self, config: KafkaConfig, admin_factory=KafkaAdminClient):
        self.config = config
        self.admin_factory = admin_factory
        self.lock = threading.Lock()
        self.ready = False

    def ensure_topic(self) -> None:
        if not self.config.enabled or self.ready:
            return
        with self.lock:
            if self.ready:
                return
            admin = self.admin_factory(
                bootstrap_servers=list(self.config.bootstrap_servers),
                client_id=f"{self.config.client_id}-admin",
                request_timeout_ms=5000,
            )
            try:
                if self.config.topic not in admin.list_topics():
                    try:
                        admin.create_topics(
                            [
                                NewTopic(
                                    name=self.config.topic,
                                    num_partitions=self.config.topic_partitions,
                                    replication_factor=self.config.topic_replication_factor,
                                )
                            ]
                        )
                    except TopicAlreadyExistsError:
                        pass
                self.ready = True
            finally:
                admin.close()


class KafkaLifecyclePublisher:
    """Publish download lifecycle events without blocking RabbitMQ callbacks."""

    def __init__(
        self,
        config: KafkaConfig,
        topic_manager: KafkaTopicManager,
        store: LifecycleEventStore,
        producer_factory=KafkaProducer,
    ):
        self.config = config
        self.topic_manager = topic_manager
        self.store = store
        self.producer_factory = producer_factory
        self.producer = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = None

    def start(self) -> None:
        if not self.config.enabled:
            return
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name="kafka-lifecycle-publisher",
            daemon=True,
        )
        self.thread.start()

    def _get_producer(self):
        with self.lock:
            if self.producer is not None:
                return self.producer
        try:
            self.topic_manager.ensure_topic()
            producer = self.producer_factory(
                bootstrap_servers=list(self.config.bootstrap_servers),
                client_id=f"{self.config.client_id}-producer",
                acks="all",
                retries=10,
                max_in_flight_requests_per_connection=1,
                value_serializer=lambda value: json.dumps(
                    value, separators=(",", ":"), sort_keys=True
                ).encode(),
                key_serializer=lambda value: value.encode(),
            )
        except (KafkaError, OSError) as exc:
            logger.warning("Kafka lifecycle publisher is unavailable: %s", exc)
            return None
        with self.lock:
            self.producer = producer
        return producer

    def publish(self, event: LifecycleEvent) -> None:
        self.store.append(event, kafka_published=not self.config.enabled)
        self.wake_event.set()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                delivered = self._deliver_pending()
            except sqlite3.Error as exc:
                logger.warning("Kafka outbox database is unavailable: %s", exc)
                self.stop_event.wait(self.config.reconnect_delay)
                continue
            if delivered:
                self.wake_event.wait(1)
                self.wake_event.clear()
            else:
                self.stop_event.wait(self.config.reconnect_delay)

    def _deliver_pending(self) -> bool:
        pending = self.store.pending()
        if not pending:
            return True
        producer = self._get_producer()
        if producer is None:
            return False
        for event in pending:
            if self.stop_event.is_set():
                return True
            try:
                producer.send(
                    self.config.topic,
                    key=event.job_id,
                    value=asdict(event),
                ).get(timeout=10)
                self.store.mark_published(event.event_id)
            except (KafkaError, OSError) as exc:
                logger.warning(
                    "Kafka lifecycle event delivery failed for job %s: %s",
                    event.job_id,
                    exc,
                )
                self._reset_producer(producer)
                return False
        return True

    def _reset_producer(self, producer) -> None:
        with self.lock:
            if self.producer is producer:
                self.producer = None
        try:
            producer.close(timeout=0)
        except (KafkaError, OSError):
            logger.debug("Kafka lifecycle producer was already closed")

    def close(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        if self.thread is not None:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                logger.warning(
                    "Kafka lifecycle publisher did not stop within 10 seconds"
                )
        with self.lock:
            producer = self.producer
            self.producer = None
        if producer is not None:
            try:
                producer.flush(timeout=10)
                producer.close(timeout=10)
            except (KafkaError, OSError):
                logger.warning("Kafka lifecycle publisher close failed", exc_info=True)


class KafkaProjectionConsumer:
    """Project Kafka lifecycle events into SQLite with manual offsets."""

    def __init__(
        self,
        config: KafkaConfig,
        topic_manager: KafkaTopicManager,
        store: LifecycleEventStore,
        consumer_factory=KafkaConsumer,
    ):
        self.config = config
        self.topic_manager = topic_manager
        self.store = store
        self.consumer_factory = consumer_factory
        self.stop_event = threading.Event()
        self.thread = None

    def start(self) -> None:
        if not self.config.enabled:
            return
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name="kafka-lifecycle-projection",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                logger.warning(
                    "Kafka projection consumer did not stop within 10 seconds"
                )

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.topic_manager.ensure_topic()
                self._consume_once()
            except KafkaError as exc:
                logger.error("Kafka projection consumer failed: %s", exc)
            except Exception:
                logger.exception("Kafka projection consumer stopped unexpectedly")
            if not self.stop_event.is_set():
                self.stop_event.wait(self.config.reconnect_delay)

    def _consume_once(self) -> None:
        consumer = self.consumer_factory(
            self.config.topic,
            bootstrap_servers=list(self.config.bootstrap_servers),
            group_id=self.config.consumer_group,
            client_id=f"{self.config.client_id}-projection",
            auto_offset_reset="earliest",
            enable_auto_commit=False,
            request_timeout_ms=15000,
        )
        try:
            while not self.stop_event.is_set():
                records = consumer.poll(timeout_ms=1000, max_records=50)
                consumed = False
                for messages in records.values():
                    for message in messages:
                        try:
                            payload = json.loads(message.value.decode())
                            event = LifecycleEvent.from_dict(payload)
                            self.store.append(event, kafka_published=True)
                        except (UnicodeDecodeError, ValueError, TypeError):
                            logger.error(
                                "Ignoring invalid Kafka lifecycle event at %s:%s:%s",
                                message.topic,
                                message.partition,
                                message.offset,
                            )
                        consumed = True
                if consumed:
                    consumer.commit()
        finally:
            consumer.close()


@dataclass(frozen=True)
class KafkaStatus:
    connected: bool
    broker_count: int = 0
    topic_exists: bool = False
    partition_count: int = 0
    consumer_lag: int = 0
    outbox_pending: int = 0
    error: Optional[str] = None


class KafkaStatusProbe:
    """Read broker, topic, and projection consumer state."""

    def __init__(
        self,
        config: KafkaConfig,
        store: Optional[LifecycleEventStore] = None,
        admin_factory=KafkaAdminClient,
        consumer_factory=KafkaConsumer,
    ):
        self.config = config
        self.store = store
        self.admin_factory = admin_factory
        self.consumer_factory = consumer_factory

    def snapshot(self) -> KafkaStatus:
        outbox_pending = self.store.pending_count() if self.store else 0
        if not self.config.enabled:
            return KafkaStatus(
                connected=False,
                outbox_pending=outbox_pending,
                error="Kafka is disabled",
            )
        admin = None
        consumer = None
        try:
            admin = self.admin_factory(
                bootstrap_servers=list(self.config.bootstrap_servers),
                client_id=f"{self.config.client_id}-status",
                request_timeout_ms=3000,
            )
            cluster = admin.describe_cluster()
            topics = admin.list_topics()
            topic_exists = self.config.topic in topics
            partitions = set()
            lag = 0
            if topic_exists:
                consumer = self.consumer_factory(
                    bootstrap_servers=list(self.config.bootstrap_servers),
                    group_id=self.config.consumer_group,
                    client_id=f"{self.config.client_id}-status-lag",
                    enable_auto_commit=False,
                    session_timeout_ms=6000,
                    request_timeout_ms=7000,
                )
                partitions = consumer.partitions_for_topic(self.config.topic) or set()
                topic_partitions = [
                    TopicPartition(self.config.topic, partition)
                    for partition in partitions
                ]
                end_offsets = consumer.end_offsets(topic_partitions)
                for topic_partition in topic_partitions:
                    committed = consumer.committed(topic_partition) or 0
                    lag += max(0, end_offsets.get(topic_partition, 0) - committed)
            return KafkaStatus(
                connected=True,
                broker_count=len(cluster.get("brokers", [])),
                topic_exists=topic_exists,
                partition_count=len(partitions),
                consumer_lag=lag,
                outbox_pending=outbox_pending,
            )
        except (KafkaError, OSError) as exc:
            logger.warning("Kafka status probe failed: %s", exc)
            return KafkaStatus(
                connected=False,
                outbox_pending=outbox_pending,
                error="Kafka is unavailable",
            )
        finally:
            if consumer is not None:
                consumer.close()
            if admin is not None:
                admin.close()
