"""RabbitMQ download job transport."""

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4

import pika

from .config import RabbitMQConfig

logger = logging.getLogger(__name__)


class MessagePublishError(RuntimeError):
    """Raised when a job cannot be confirmed by RabbitMQ."""


@dataclass(frozen=True)
class DownloadJob:
    """A request to download one media URL."""

    job_id: str
    url: str
    submitted_at: str
    attempt: int = 0
    last_error: Optional[str] = None

    @classmethod
    def create(cls, url: str) -> "DownloadJob":
        return cls(
            job_id=str(uuid4()),
            url=url,
            submitted_at=datetime.now(timezone.utc).isoformat(),
        )

    @classmethod
    def from_bytes(cls, body: bytes) -> "DownloadJob":
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("Download job must be a JSON object")
            job = cls(**data)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid download job") from exc

        if (
            not isinstance(job.job_id, str)
            or not job.job_id
            or not isinstance(job.url, str)
            or not job.url
            or not isinstance(job.submitted_at, str)
            or not job.submitted_at
            or not isinstance(job.attempt, int)
            or isinstance(job.attempt, bool)
            or job.attempt < 0
        ):
            raise ValueError("Invalid download job")
        return job

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True).encode()


class PartialPublishError(MessagePublishError):
    """Raised when only part of a URL batch was confirmed."""

    def __init__(
        self,
        accepted_jobs: list[DownloadJob],
        unaccepted_jobs: list[DownloadJob],
    ):
        super().__init__("Only part of the download job batch was published")
        self.accepted_jobs = accepted_jobs
        self.unaccepted_jobs = unaccepted_jobs


class RabbitMQTopology:
    """Declare the exchanges and queues used for download jobs."""

    def __init__(self, config: RabbitMQConfig):
        self.config = config
        self.retry_exchange = f"{config.exchange}.retry"
        self.dead_exchange = f"{config.exchange}.dead"
        self.dead_queue = f"{config.queue}.dead"

    def retry_queue(self, attempt: int) -> str:
        delay = self._retry_delay(attempt)
        return f"{self.config.queue}.retry.{attempt}.{delay}s"

    def retry_routing_key(self, attempt: int) -> str:
        delay = self._retry_delay(attempt)
        return f"retry.{attempt}.{delay}s"

    def _retry_delay(self, attempt: int) -> int:
        if attempt < 1:
            raise ValueError(f"Invalid retry attempt {attempt}")
        try:
            return self.config.retry_delays[attempt - 1]
        except IndexError as exc:
            raise ValueError(f"No retry delay for attempt {attempt}") from exc

    def declare(self, channel) -> None:
        channel.exchange_declare(
            exchange=self.config.exchange,
            exchange_type="direct",
            durable=True,
        )
        channel.exchange_declare(
            exchange=self.retry_exchange,
            exchange_type="direct",
            durable=True,
        )
        channel.exchange_declare(
            exchange=self.dead_exchange,
            exchange_type="direct",
            durable=True,
        )

        channel.queue_declare(
            queue=self.config.queue,
            durable=True,
            arguments={"x-queue-type": "quorum"},
        )
        channel.queue_bind(
            queue=self.config.queue,
            exchange=self.config.exchange,
            routing_key=self.config.routing_key,
        )

        for attempt, delay in enumerate(self.config.retry_delays, start=1):
            queue = self.retry_queue(attempt)
            channel.queue_declare(
                queue=queue,
                durable=True,
                arguments={
                    "x-queue-type": "classic",
                    "x-message-ttl": delay * 1000,
                    "x-dead-letter-exchange": self.config.exchange,
                    "x-dead-letter-routing-key": self.config.routing_key,
                },
            )
            channel.queue_bind(
                queue=queue,
                exchange=self.retry_exchange,
                routing_key=self.retry_routing_key(attempt),
            )

        channel.queue_declare(
            queue=self.dead_queue,
            durable=True,
            arguments={"x-queue-type": "quorum"},
        )
        channel.queue_bind(
            queue=self.dead_queue,
            exchange=self.dead_exchange,
            routing_key=self.config.routing_key,
        )


def _connection_parameters(config: RabbitMQConfig) -> pika.ConnectionParameters:
    password = (
        Path(config.password_file).read_text().strip()
        if config.password_file
        else "guest"
    )
    credentials = pika.PlainCredentials(config.username, password)
    return pika.ConnectionParameters(
        host=config.host,
        port=config.port,
        virtual_host=config.virtual_host,
        credentials=credentials,
        heartbeat=60,
        blocked_connection_timeout=30,
    )


def _properties(job: DownloadJob, reason: Optional[str] = None) -> pika.BasicProperties:
    headers = {"attempt": job.attempt}
    if reason:
        headers["reason"] = reason
    return pika.BasicProperties(
        content_type="application/json",
        delivery_mode=2,
        message_id=job.job_id,
        timestamp=int(time.time()),
        headers=headers,
    )


class RabbitMQPublisher:
    """Publish confirmed download jobs."""

    def __init__(self, config: RabbitMQConfig, connection_factory=None):
        self.config = config
        self.topology = RabbitMQTopology(config)
        self.connection_factory = connection_factory or pika.BlockingConnection
        self.connection = None
        self.channel = None
        self.lock = threading.Lock()

    def publish_urls(self, urls: list[str]) -> list[DownloadJob]:
        jobs = [DownloadJob.create(url) for url in urls]
        accepted_jobs = []
        for index, job in enumerate(jobs):
            try:
                self.publish(job)
            except MessagePublishError as exc:
                if not accepted_jobs:
                    raise MessagePublishError(
                        "Unable to publish any download jobs"
                    ) from exc
                raise PartialPublishError(accepted_jobs, jobs[index:]) from exc
            accepted_jobs.append(job)
        return jobs

    def publish(self, job: DownloadJob) -> None:
        with self.lock:
            for publish_attempt in range(2):
                try:
                    channel = self._get_channel()
                    channel.basic_publish(
                        exchange=self.config.exchange,
                        routing_key=self.config.routing_key,
                        body=job.to_bytes(),
                        properties=_properties(job),
                        mandatory=True,
                    )
                    return
                except (
                    OSError,
                    pika.exceptions.AMQPError,
                    MessagePublishError,
                ) as exc:
                    self._close_unlocked()
                    if publish_attempt == 1:
                        raise MessagePublishError(
                            f"Unable to publish download job {job.job_id}"
                        ) from exc

    def _get_channel(self):
        if self.connection is None or self.connection.is_closed:
            self.connection = self.connection_factory(
                _connection_parameters(self.config)
            )

        if self.channel is None or self.channel.is_closed:
            self.channel = self.connection.channel()
            self.topology.declare(self.channel)
            self.channel.confirm_delivery()
        return self.channel

    def close(self) -> None:
        with self.lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        connection = self.connection
        self.connection = None
        self.channel = None
        if connection is not None and connection.is_open:
            try:
                connection.close()
            except Exception:
                logger.warning(
                    "RabbitMQ publisher connection close failed", exc_info=True
                )


class RabbitMQConsumer:
    """Consume download jobs and route failures through retries."""

    def __init__(
        self,
        config: RabbitMQConfig,
        handler: Callable[[DownloadJob], bool],
        lifecycle_handler: Optional[
            Callable[[str, DownloadJob, Optional[str]], None]
        ] = None,
        connection_factory=None,
    ):
        self.config = config
        self.handler = handler
        self.lifecycle_handler = lifecycle_handler
        self.topology = RabbitMQTopology(config)
        self.connection_factory = connection_factory or pika.BlockingConnection
        self.stop_event = threading.Event()
        self.thread = None
        self.worker_thread = None
        self.connection = None
        self.channel = None

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name="rabbitmq-download-consumer",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._wait_for_worker()
        connection = self.connection
        if connection is not None and connection.is_open:
            try:
                connection.add_callback_threadsafe(self._stop_consuming)
            except pika.exceptions.AMQPError:
                logger.debug("RabbitMQ consumer connection already closing")
        if self.thread is not None:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                logger.warning("RabbitMQ consumer did not stop within 10 seconds")
        self._wait_for_worker()

    def _wait_for_worker(self) -> None:
        worker = self.worker_thread
        if worker is not None and worker.is_alive():
            worker.join()

    def _stop_consuming(self) -> None:
        if self.channel is not None and self.channel.is_open:
            self.channel.stop_consuming()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._consume_once()
            except pika.exceptions.AMQPError as exc:
                logger.error("RabbitMQ consumer connection failed: %s", exc)
            except Exception:
                logger.exception("RabbitMQ consumer stopped unexpectedly")

            self._wait_for_worker()
            if not self.stop_event.is_set():
                self.stop_event.wait(self.config.reconnect_delay)

    def _consume_once(self) -> None:
        try:
            self.connection = self.connection_factory(
                _connection_parameters(self.config)
            )
            self.channel = self.connection.channel()
            self.topology.declare(self.channel)
            self.channel.confirm_delivery()
            self.channel.basic_qos(prefetch_count=1)
            self.channel.basic_consume(
                queue=self.config.queue,
                on_message_callback=self._on_message,
                auto_ack=False,
            )
            logger.info("Consuming RabbitMQ queue %s", self.config.queue)
            self.channel.start_consuming()
        finally:
            if self.connection is not None and self.connection.is_open:
                try:
                    self.connection.close()
                except Exception:
                    logger.warning(
                        "RabbitMQ consumer connection close failed",
                        exc_info=True,
                    )
            self.connection = None
            self.channel = None

    def _on_message(self, channel, method, properties, body: bytes):
        if self.stop_event.is_set():
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            return None

        try:
            job = DownloadJob.from_bytes(body)
        except ValueError:
            logger.error("Dead-lettering invalid download job")
            self._publish_raw_dead(channel, body, "invalid_message")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        connection = self.connection
        self.worker_thread = threading.Thread(
            target=self._process_job,
            args=(connection, channel, method.delivery_tag, job),
            name=f"download-job-{job.job_id}",
            daemon=False,
        )
        self.worker_thread.start()
        return self.worker_thread

    def _process_job(self, connection, channel, delivery_tag: int, job: DownloadJob):
        self._emit_lifecycle("download.started", job)
        try:
            succeeded = self.handler(job)
        except Exception:
            logger.exception("Download job %s raised an exception", job.job_id)
            succeeded = False
        self._emit_lifecycle(
            "download.succeeded" if succeeded else "download.failed",
            job,
        )

        try:
            connection.add_callback_threadsafe(
                lambda: self._complete_job(channel, delivery_tag, job, succeeded)
            )
        except pika.exceptions.AMQPError:
            logger.warning(
                "RabbitMQ connection closed before job %s could be acknowledged",
                job.job_id,
            )

    def _complete_job(
        self,
        channel,
        delivery_tag: int,
        job: DownloadJob,
        succeeded: bool,
    ) -> None:
        if succeeded:
            channel.basic_ack(delivery_tag=delivery_tag)
            return

        next_attempt = job.attempt + 1
        failed_job = replace(
            job,
            attempt=next_attempt,
            last_error="download_failed",
        )
        if next_attempt <= len(self.config.retry_delays):
            self._publish_retry(channel, failed_job)
            self._emit_lifecycle(
                "download.retry_scheduled",
                failed_job,
                f"{self.config.retry_delays[next_attempt - 1]}s",
            )
            logger.warning(
                "Scheduled retry %s for download job %s",
                next_attempt,
                job.job_id,
            )
        else:
            self._publish_dead(channel, failed_job)
            self._emit_lifecycle("download.dead_lettered", failed_job)
            logger.error("Dead-lettered download job %s", job.job_id)

        channel.basic_ack(delivery_tag=delivery_tag)

    def _emit_lifecycle(
        self,
        event_type: str,
        job: DownloadJob,
        detail: Optional[str] = None,
    ) -> None:
        if self.lifecycle_handler is None:
            return
        try:
            self.lifecycle_handler(event_type, job, detail)
        except Exception:
            logger.exception(
                "Unable to emit lifecycle event %s for job %s",
                event_type,
                job.job_id,
            )

    def _publish_retry(self, channel, job: DownloadJob) -> None:
        channel.basic_publish(
            exchange=self.topology.retry_exchange,
            routing_key=self.topology.retry_routing_key(job.attempt),
            body=job.to_bytes(),
            properties=_properties(job, job.last_error),
            mandatory=True,
        )

    def _publish_dead(self, channel, job: DownloadJob) -> None:
        channel.basic_publish(
            exchange=self.topology.dead_exchange,
            routing_key=self.config.routing_key,
            body=job.to_bytes(),
            properties=_properties(job, job.last_error),
            mandatory=True,
        )

    def _publish_raw_dead(self, channel, body: bytes, reason: str) -> None:
        channel.basic_publish(
            exchange=self.topology.dead_exchange,
            routing_key=self.config.routing_key,
            body=body,
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,
                timestamp=int(time.time()),
                headers={"reason": reason},
            ),
            mandatory=True,
        )
