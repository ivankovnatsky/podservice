"""Tests for read-only broker status probes."""

from unittest.mock import Mock

from podservice.config import RabbitMQConfig
from podservice.status import RabbitMQStatusProbe


def response(payload):
    result = Mock()
    result.json.return_value = payload
    return result


def test_rabbitmq_status_reports_main_retry_and_dead_queues():
    session = Mock()
    session.get.side_effect = [
        response({"rabbitmq_version": "4.2.5"}),
        response(
            [
                {
                    "name": "downloads",
                    "messages_ready": 2,
                    "messages_unacknowledged": 1,
                    "consumers": 1,
                    "state": "running",
                },
                {
                    "name": "downloads.retry.1.30s",
                    "messages_ready": 3,
                    "consumers": 0,
                    "state": "running",
                },
                {
                    "name": "downloads.dead",
                    "messages_ready": 4,
                    "consumers": 0,
                    "state": "running",
                },
            ]
        ),
    ]
    probe = RabbitMQStatusProbe(
        RabbitMQConfig(
            queue="downloads",
            exchange="commands",
            retry_delays=(30,),
        ),
        session=session,
    )

    status = probe.snapshot()

    assert status.connected is True
    assert status.version == "4.2.5"
    assert status.ready == 9
    assert status.unacknowledged == 1
    assert status.consumers == 1
    assert [queue.role for queue in status.queues] == [
        "Downloads",
        "Retry 1 (30s)",
        "Dead letter",
    ]
    assert session.get.call_args_list[1].args[0].endswith("/api/queues/%2F")
