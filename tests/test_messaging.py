"""Tests for RabbitMQ download job transport."""

import threading
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pika
import pytest

from podservice.config import RabbitMQConfig, ServiceConfig, load_config, save_config
from podservice.messaging import (
    DownloadJob,
    MessagePublishError,
    PartialPublishError,
    RabbitMQConsumer,
    RabbitMQPublisher,
    RabbitMQTopology,
    _connection_parameters,
)


def make_config(**overrides) -> RabbitMQConfig:
    values = {
        "exchange": "commands",
        "queue": "downloads",
        "routing_key": "download",
        "retry_delays": (30, 300),
    }
    values.update(overrides)
    return RabbitMQConfig(**values)


def make_job(attempt: int = 0) -> DownloadJob:
    return DownloadJob(
        job_id="job-1",
        url="https://example.com/episode",
        submitted_at="2026-07-20T00:00:00+00:00",
        attempt=attempt,
    )


def test_topology_declares_retry_ttls_and_dead_letter_routes():
    channel = Mock()
    topology = RabbitMQTopology(make_config())

    topology.declare(channel)

    retry_declarations = [
        call.kwargs
        for call in channel.queue_declare.call_args_list
        if ".retry." in call.kwargs["queue"]
    ]
    assert [item["arguments"]["x-message-ttl"] for item in retry_declarations] == [
        30_000,
        300_000,
    ]
    assert all(
        item["arguments"]["x-dead-letter-exchange"] == "commands"
        for item in retry_declarations
    )
    assert [item["queue"] for item in retry_declarations] == [
        "downloads.retry.1.30s",
        "downloads.retry.2.300s",
    ]
    assert all(
        item["arguments"]["x-queue-type"] == "classic" for item in retry_declarations
    )


def test_publisher_confirms_persistent_job():
    channel = Mock()
    channel.is_closed = False
    channel.basic_publish.return_value = True
    connection = Mock()
    connection.is_closed = False
    connection.is_open = True
    connection.channel.return_value = channel
    publisher = RabbitMQPublisher(
        make_config(),
        connection_factory=Mock(return_value=connection),
    )

    publisher.publish(make_job())

    channel.confirm_delivery.assert_called_once_with()
    published = channel.basic_publish.call_args.kwargs
    assert published["exchange"] == "commands"
    assert published["routing_key"] == "download"
    assert published["properties"].delivery_mode == 2
    assert DownloadJob.from_bytes(published["body"]).job_id == "job-1"


def test_download_job_rejects_non_object_payload():
    with pytest.raises(ValueError):
        DownloadJob.from_bytes(b"[]")


def test_download_job_rejects_invalid_attempt_type():
    body = make_job().to_bytes().replace(b'"attempt":0', b'"attempt":"zero"')

    with pytest.raises(ValueError):
        DownloadJob.from_bytes(body)


def test_connection_parameters_read_password_file(tmp_path):
    password_file = tmp_path / "rabbitmq-password"
    password_file.write_text("file-secret\n")

    parameters = _connection_parameters(make_config(password_file=str(password_file)))

    assert parameters.credentials.password == "file-secret"


def test_saved_config_uses_password_file(tmp_path):
    config_path = tmp_path / "config.yaml"
    password_file = tmp_path / "rabbitmq-password"
    config = ServiceConfig(rabbitmq=make_config(password_file=str(password_file)))

    save_config(config, str(config_path))

    saved = config_path.read_text()
    assert "\n  password:" not in saved
    assert f"password_file: {password_file}" in saved


def test_inline_password_config_is_rejected(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("rabbitmq:\n  password: inline-secret\n")

    with pytest.raises(Exception, match="use rabbitmq.password_file"):
        load_config(str(config_path))


def test_non_guest_user_requires_password_file():
    with pytest.raises(ValueError, match="password_file is required"):
        make_config(username="podservice")


def test_batch_publish_reports_accepted_and_unaccepted_jobs():
    publisher = RabbitMQPublisher(make_config())
    publisher.publish = Mock(side_effect=[None, MessagePublishError("unavailable")])

    with pytest.raises(PartialPublishError) as error:
        publisher.publish_urls(
            [
                "https://example.com/one",
                "https://example.com/two",
                "https://example.com/three",
            ]
        )

    assert [job.url for job in error.value.accepted_jobs] == ["https://example.com/one"]
    assert [job.url for job in error.value.unaccepted_jobs] == [
        "https://example.com/two",
        "https://example.com/three",
    ]


def test_batch_publish_reports_total_outage_when_no_jobs_are_accepted():
    publisher = RabbitMQPublisher(make_config())
    publisher.publish = Mock(side_effect=MessagePublishError("unavailable"))

    with pytest.raises(MessagePublishError) as error:
        publisher.publish_urls(["https://example.com/one"])

    assert type(error.value) is MessagePublishError


def test_publisher_wraps_publish_failure_when_connection_close_also_fails():
    channel = Mock()
    channel.is_closed = False
    channel.basic_publish.side_effect = pika.exceptions.AMQPConnectionError(
        "publish failed"
    )
    connection = Mock()
    connection.is_closed = False
    connection.is_open = True
    connection.close.side_effect = pika.exceptions.AMQPConnectionError("close failed")
    connection.channel.return_value = channel
    publisher = RabbitMQPublisher(
        make_config(),
        connection_factory=Mock(return_value=connection),
    )

    with pytest.raises(MessagePublishError):
        publisher.publish(make_job())


def test_consumer_acknowledges_successful_job():
    channel = Mock()
    consumer = RabbitMQConsumer(make_config(), handler=Mock(return_value=True))

    consumer._complete_job(channel, 7, make_job(), succeeded=True)

    channel.basic_ack.assert_called_once_with(delivery_tag=7)
    channel.basic_publish.assert_not_called()


def test_consumer_schedules_confirmed_retry_before_acknowledging():
    channel = Mock()
    channel.basic_publish.return_value = True
    consumer = RabbitMQConsumer(make_config(), handler=Mock(return_value=False))

    consumer._complete_job(channel, 8, make_job(), succeeded=False)

    published = channel.basic_publish.call_args.kwargs
    assert published["exchange"] == "commands.retry"
    assert published["routing_key"] == "retry.1.30s"
    assert DownloadJob.from_bytes(published["body"]).attempt == 1
    channel.basic_ack.assert_called_once_with(delivery_tag=8)


def test_consumer_dead_letters_after_last_retry():
    channel = Mock()
    channel.basic_publish.return_value = True
    consumer = RabbitMQConsumer(make_config(), handler=Mock(return_value=False))

    consumer._complete_job(channel, 9, make_job(attempt=2), succeeded=False)

    published = channel.basic_publish.call_args.kwargs
    assert published["exchange"] == "commands.dead"
    assert DownloadJob.from_bytes(published["body"]).attempt == 3
    channel.basic_ack.assert_called_once_with(delivery_tag=9)


def test_consumer_does_not_ack_when_retry_publish_is_unconfirmed():
    channel = Mock()
    channel.basic_publish.side_effect = pika.exceptions.NackError([])
    consumer = RabbitMQConsumer(make_config(), handler=Mock(return_value=False))

    with pytest.raises(pika.exceptions.NackError):
        consumer._complete_job(channel, 10, make_job(), succeeded=False)

    channel.basic_ack.assert_not_called()


def test_consumer_keeps_connection_thread_free_during_download():
    handler_started = Event()
    release_handler = Event()

    def handler(job):
        handler_started.set()
        release_handler.wait(timeout=2)
        return True

    connection = Mock()
    channel = Mock()
    consumer = RabbitMQConsumer(make_config(), handler=handler)
    consumer.connection = connection

    worker = consumer._on_message(
        channel,
        SimpleNamespace(delivery_tag=11),
        None,
        make_job().to_bytes(),
    )

    assert handler_started.wait(timeout=1)
    assert worker.is_alive()
    assert not worker.daemon
    connection.add_callback_threadsafe.assert_not_called()

    release_handler.set()
    worker.join(timeout=1)
    callback = connection.add_callback_threadsafe.call_args.args[0]
    callback()
    channel.basic_ack.assert_called_once_with(delivery_tag=11)


def test_consumer_stop_tolerates_closing_connection():
    connection = Mock()
    connection.is_open = True
    connection.add_callback_threadsafe.side_effect = (
        pika.exceptions.AMQPConnectionError("closing")
    )
    consumer = RabbitMQConsumer(make_config(), handler=Mock())
    consumer.connection = connection

    consumer.stop()


def test_consumer_waits_for_worker_before_reconnecting():
    release_worker = Event()
    worker = threading.Thread(target=release_worker.wait)
    worker.start()
    consumer = RabbitMQConsumer(make_config(reconnect_delay=1), handler=Mock())
    consumer.worker_thread = worker
    consume_calls = 0
    reconnected = Event()

    def consume_once():
        nonlocal consume_calls
        consume_calls += 1
        if consume_calls == 1:
            raise pika.exceptions.AMQPConnectionError("lost")
        reconnected.set()
        consumer.stop_event.set()

    consumer._consume_once = consume_once
    consumer_thread = threading.Thread(target=consumer._run)
    consumer_thread.start()

    assert not reconnected.wait(timeout=0.1)
    release_worker.set()
    assert reconnected.wait(timeout=2)
    consumer_thread.join(timeout=1)


def test_consumer_stop_waits_for_active_worker():
    release_worker = Event()
    worker = threading.Thread(target=release_worker.wait)
    worker.start()
    consumer = RabbitMQConsumer(make_config(), handler=Mock())
    consumer.worker_thread = worker
    stop_thread = threading.Thread(target=consumer.stop)

    stop_thread.start()
    stop_thread.join(timeout=0.1)
    assert stop_thread.is_alive()

    release_worker.set()
    stop_thread.join(timeout=1)
    assert not stop_thread.is_alive()
