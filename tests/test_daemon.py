"""Tests for service startup migrations."""

from types import SimpleNamespace
from unittest.mock import Mock

from podservice.daemon import PodService
from podservice.messaging import DownloadJob


def test_pending_legacy_urls_are_published_then_removed(tmp_path):
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(
        "https://example.com/one\ninvalid-value\nhttps://example.com/two\n"
    )
    service = PodService.__new__(PodService)
    service.config = SimpleNamespace(legacy_urls_file=str(urls_file))
    service.submit_urls = Mock(
        return_value=[
            DownloadJob.create("https://example.com/one"),
            DownloadJob.create("https://example.com/two"),
        ]
    )

    service._migrate_legacy_urls()

    service.submit_urls.assert_called_once_with(
        ["https://example.com/one", "https://example.com/two"]
    )
    assert urls_file.read_text() == "invalid-value\n"


def test_cleanup_stops_http_submissions_before_broker_clients():
    service = PodService.__new__(PodService)
    calls = []
    service.server = Mock()
    service.publisher = Mock()
    service.consumer = Mock()
    service.kafka_projection = Mock()
    service.kafka_publisher = Mock()
    service.server.stop.side_effect = lambda: calls.append("server")
    service.publisher.close.side_effect = lambda: calls.append("rabbit-publisher")
    service.consumer.stop.side_effect = lambda: calls.append("rabbit-consumer")
    service.kafka_projection.stop.side_effect = lambda: calls.append("kafka-consumer")
    service.kafka_publisher.close.side_effect = lambda: calls.append("kafka-publisher")

    service.cleanup()

    assert calls == [
        "server",
        "rabbit-publisher",
        "rabbit-consumer",
        "kafka-consumer",
        "kafka-publisher",
    ]


def test_lifecycle_failure_does_not_replace_confirmed_rabbitmq_result():
    service = PodService.__new__(PodService)
    service.kafka_publisher = Mock()
    service.kafka_publisher.publish.side_effect = RuntimeError("Kafka unavailable")
    job = DownloadJob.create("https://example.com/episode")

    service._emit_lifecycle("download.requested", job)
