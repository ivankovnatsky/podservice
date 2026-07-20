"""Kafka lifecycle events and the local dashboard projection."""

import json
import logging
import sqlite3
import threading
import time
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


@dataclass(frozen=True)
class LifecycleEvent:
    """A download lifecycle fact published to Kafka."""

    event_id: str
    event_type: str
    occurred_at: str
    job_id: str
    url: str
    attempt: int
    detail: Optional[str] = None

    @classmethod
    def create(
        cls,
        event_type: str,
        job_id: str,
        url: str,
        attempt: int,
        detail: Optional[str] = None,
    ) -> "LifecycleEvent":
        return cls(
            event_id=str(uuid4()),
            event_type=event_type,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            job_id=job_id,
            url=url,
            attempt=attempt,
            detail=detail,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "LifecycleEvent":
        if not isinstance(data, dict):
            raise ValueError("Lifecycle event must be an object")
        try:
            event = cls(**data)
        except TypeError as exc:
            raise ValueError("Invalid lifecycle event") from exc
        if (
            not event.event_id
            or not event.event_type
            or not event.occurred_at
            or not event.job_id
            or not event.url
            or not isinstance(event.attempt, int)
            or isinstance(event.attempt, bool)
            or event.attempt < 0
        ):
            raise ValueError("Invalid lifecycle event")
        return event


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
                        url TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        detail TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS lifecycle_events_occurred_at
                    ON lifecycle_events (occurred_at DESC)
                    """
                )

    def append(self, event: LifecycleEvent) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO lifecycle_events (
                        event_id, event_type, occurred_at, job_id, url, attempt, detail
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.event_type,
                        event.occurred_at,
                        event.job_id,
                        event.url,
                        event.attempt,
                        event.detail,
                    ),
                )

    def recent(self, limit: int = 50) -> list[LifecycleEvent]:
        safe_limit = max(1, min(limit, 200))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT event_id, event_type, occurred_at, job_id, url, attempt, detail
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
                api_version_auto_timeout_ms=5000,
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
        producer_factory=KafkaProducer,
    ):
        self.config = config
        self.topic_manager = topic_manager
        self.producer_factory = producer_factory
        self.producer = None
        self.lock = threading.Lock()
        self.next_start_attempt = 0.0

    def start(self) -> None:
        if not self.config.enabled or self.producer is not None:
            return
        with self.lock:
            self._start_unlocked()

    def _start_unlocked(self) -> None:
        if self.producer is not None or time.monotonic() < self.next_start_attempt:
            return
        try:
            self.topic_manager.ensure_topic()
            self.producer = self.producer_factory(
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
            self.next_start_attempt = time.monotonic() + self.config.reconnect_delay
            logger.warning("Kafka lifecycle publisher is unavailable: %s", exc)

    def publish(self, event: LifecycleEvent) -> None:
        if not self.config.enabled:
            return
        if self.producer is None:
            self.start()
        producer = self.producer
        if producer is None:
            logger.warning(
                "Skipping Kafka lifecycle event %s for job %s while Kafka is unavailable",
                event.event_type,
                event.job_id,
            )
            return
        try:
            future = producer.send(
                self.config.topic,
                key=event.job_id,
                value=asdict(event),
            )
        except (KafkaError, OSError) as exc:
            logger.warning(
                "Unable to queue Kafka lifecycle event %s for job %s: %s",
                event.event_type,
                event.job_id,
                exc,
            )
            with self.lock:
                if self.producer is producer:
                    self.producer = None
                    self.next_start_attempt = (
                        time.monotonic() + self.config.reconnect_delay
                    )
                    producer.close(timeout=0)
            return
        future.add_errback(self._log_publish_error, event)

    @staticmethod
    def _log_publish_error(error: Exception, event: LifecycleEvent) -> None:
        logger.error(
            "Unable to publish Kafka lifecycle event %s for job %s: %s",
            event.event_type,
            event.job_id,
            error,
        )

    def close(self) -> None:
        with self.lock:
            producer = self.producer
            self.producer = None
        if producer is not None:
            producer.flush(timeout=10)
            producer.close(timeout=10)


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
            request_timeout_ms=5000,
            api_version_auto_timeout_ms=5000,
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
                            self.store.append(event)
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
    error: Optional[str] = None


class KafkaStatusProbe:
    """Read broker, topic, and projection consumer state."""

    def __init__(
        self,
        config: KafkaConfig,
        admin_factory=KafkaAdminClient,
        consumer_factory=KafkaConsumer,
    ):
        self.config = config
        self.admin_factory = admin_factory
        self.consumer_factory = consumer_factory

    def snapshot(self) -> KafkaStatus:
        if not self.config.enabled:
            return KafkaStatus(connected=False, error="Kafka is disabled")
        admin = None
        consumer = None
        try:
            admin = self.admin_factory(
                bootstrap_servers=list(self.config.bootstrap_servers),
                client_id=f"{self.config.client_id}-status",
                request_timeout_ms=3000,
                api_version_auto_timeout_ms=3000,
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
                    request_timeout_ms=3000,
                    api_version_auto_timeout_ms=3000,
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
            )
        except (KafkaError, OSError) as exc:
            logger.warning("Kafka status probe failed: %s", exc)
            return KafkaStatus(connected=False, error="Kafka is unavailable")
        finally:
            if consumer is not None:
                consumer.close()
            if admin is not None:
                admin.close()
