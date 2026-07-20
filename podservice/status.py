"""Read-only broker status probes for the web dashboard."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

from .config import RabbitMQConfig
from .messaging import RabbitMQTopology

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RabbitMQQueueStatus:
    name: str
    role: str
    ready: int
    unacknowledged: int
    consumers: int
    state: str


@dataclass(frozen=True)
class RabbitMQStatus:
    connected: bool
    version: Optional[str] = None
    queues: tuple[RabbitMQQueueStatus, ...] = ()
    error: Optional[str] = None

    @property
    def ready(self) -> int:
        return sum(queue.ready for queue in self.queues)

    @property
    def unacknowledged(self) -> int:
        return sum(queue.unacknowledged for queue in self.queues)

    @property
    def consumers(self) -> int:
        return sum(queue.consumers for queue in self.queues)


class RabbitMQStatusProbe:
    """Read RabbitMQ management data without consuming queue messages."""

    def __init__(self, config: RabbitMQConfig, session=requests):
        self.config = config
        self.session = session
        self.topology = RabbitMQTopology(config)

    def snapshot(self) -> RabbitMQStatus:
        password = (
            Path(self.config.password_file).read_text().strip()
            if self.config.password_file
            else "guest"
        )
        base_url = f"http://{self.config.host}:{self.config.management_port}/api"
        auth = (self.config.username, password)
        try:
            overview_response = self.session.get(
                f"{base_url}/overview",
                auth=auth,
                timeout=3,
            )
            overview_response.raise_for_status()
            queues_response = self.session.get(
                f"{base_url}/queues/{quote(self.config.virtual_host, safe='')}",
                auth=auth,
                timeout=3,
            )
            queues_response.raise_for_status()
            overview = overview_response.json()
            queue_data = {queue["name"]: queue for queue in queues_response.json()}
            expected = [
                (self.config.queue, "Downloads"),
                *[
                    (
                        self.topology.retry_queue(attempt),
                        f"Retry {attempt} ({delay}s)",
                    )
                    for attempt, delay in enumerate(self.config.retry_delays, start=1)
                ],
                (self.topology.dead_queue, "Dead letter"),
            ]
            queues = tuple(
                self._queue_snapshot(name, role, queue_data.get(name))
                for name, role in expected
            )
            return RabbitMQStatus(
                connected=True,
                version=overview.get("rabbitmq_version"),
                queues=queues,
            )
        except (OSError, requests.RequestException, ValueError, KeyError) as exc:
            logger.warning("RabbitMQ status probe failed: %s", exc)
            return RabbitMQStatus(
                connected=False,
                error="RabbitMQ management API is unavailable",
            )

    @staticmethod
    def _queue_snapshot(
        name: str,
        role: str,
        queue: Optional[dict],
    ) -> RabbitMQQueueStatus:
        if queue is None:
            return RabbitMQQueueStatus(
                name=name,
                role=role,
                ready=0,
                unacknowledged=0,
                consumers=0,
                state="missing",
            )
        return RabbitMQQueueStatus(
            name=name,
            role=role,
            ready=int(queue.get("messages_ready", 0)),
            unacknowledged=int(queue.get("messages_unacknowledged", 0)),
            consumers=int(queue.get("consumers", 0)),
            state=str(queue.get("state", "unknown")),
        )
