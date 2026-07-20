"""Tests for Kafka lifecycle events and their SQLite projection."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

from kafka import TopicPartition
from kafka.errors import NoBrokersAvailable

from podservice.config import KafkaConfig
from podservice.events import (
    KafkaLifecyclePublisher,
    KafkaProjectionConsumer,
    KafkaStatusProbe,
    KafkaTopicManager,
    LifecycleEvent,
    LifecycleEventStore,
)


def kafka_config(**overrides):
    values = {"enabled": True, "topic": "lifecycle"}
    values.update(overrides)
    return KafkaConfig(**values)


def make_event():
    return LifecycleEvent(
        event_id="event-1",
        event_type="download.succeeded",
        occurred_at="2026-07-20T12:00:00+00:00",
        job_id="job-1",
        url="https://example.com/episode",
        attempt=0,
    )


def test_event_store_projects_events_idempotently(tmp_path):
    store = LifecycleEventStore(tmp_path / "events.sqlite3")
    event = make_event()

    store.append(event)
    store.append(event)

    assert store.recent() == [event]


def test_topic_manager_creates_lifecycle_topic_once():
    admin = Mock()
    admin.list_topics.return_value = set()
    manager = KafkaTopicManager(
        kafka_config(topic_partitions=2),
        admin_factory=Mock(return_value=admin),
    )

    manager.ensure_topic()
    manager.ensure_topic()

    admin.create_topics.assert_called_once()
    topic = admin.create_topics.call_args.args[0][0]
    assert topic.name == "lifecycle"
    assert topic.num_partitions == 2
    admin.close.assert_called_once()


def test_publisher_sends_keyed_lifecycle_event():
    manager = Mock()
    producer = Mock()
    future = Mock()
    producer.send.return_value = future
    publisher = KafkaLifecyclePublisher(
        kafka_config(),
        manager,
        producer_factory=Mock(return_value=producer),
    )

    publisher.start()
    publisher.publish(make_event())

    manager.ensure_topic.assert_called_once()
    sent = producer.send.call_args
    assert sent.args == ("lifecycle",)
    assert sent.kwargs["key"] == "job-1"
    assert sent.kwargs["value"]["event_id"] == "event-1"
    future.add_errback.assert_called_once()


def test_publisher_degrades_when_kafka_is_unavailable():
    manager = Mock()
    manager.ensure_topic.side_effect = NoBrokersAvailable()
    publisher = KafkaLifecyclePublisher(kafka_config(), manager)

    publisher.start()
    publisher.publish(make_event())

    assert publisher.producer is None


def test_kafka_status_reports_topic_partitions_and_consumer_lag():
    admin = Mock()
    admin.describe_cluster.return_value = {"brokers": [{"node_id": 1}]}
    admin.list_topics.return_value = {"lifecycle"}
    consumer = Mock()
    consumer.partitions_for_topic.return_value = {0, 1}
    consumer.end_offsets.return_value = {
        TopicPartition("lifecycle", 0): 12,
        TopicPartition("lifecycle", 1): 5,
    }
    consumer.committed.side_effect = [10, 3]
    probe = KafkaStatusProbe(
        kafka_config(),
        admin_factory=Mock(return_value=admin),
        consumer_factory=Mock(return_value=consumer),
    )

    status = probe.snapshot()

    assert status.connected is True
    assert status.broker_count == 1
    assert status.topic_exists is True
    assert status.partition_count == 2
    assert status.consumer_lag == 4
    consumer.close.assert_called_once()
    admin.close.assert_called_once()


def test_kafka_status_tolerates_partition_metadata_changing():
    admin = Mock()
    admin.describe_cluster.return_value = {"brokers": [{"node_id": 1}]}
    admin.list_topics.return_value = {"lifecycle"}
    consumer = Mock()
    consumer.partitions_for_topic.return_value = {0, 1}
    consumer.end_offsets.return_value = {TopicPartition("lifecycle", 0): 12}
    consumer.committed.return_value = 10
    probe = KafkaStatusProbe(
        kafka_config(),
        admin_factory=Mock(return_value=admin),
        consumer_factory=Mock(return_value=consumer),
    )

    status = probe.snapshot()

    assert status.connected is True
    assert status.consumer_lag == 2


def test_projection_commits_past_malformed_json(tmp_path):
    consumer = Mock()
    projection = KafkaProjectionConsumer(
        kafka_config(),
        Mock(),
        LifecycleEventStore(tmp_path / "events.sqlite3"),
        consumer_factory=Mock(return_value=consumer),
    )
    invalid = SimpleNamespace(
        topic="lifecycle",
        partition=0,
        offset=1,
        value=b"{invalid",
    )
    valid = SimpleNamespace(
        topic="lifecycle",
        partition=0,
        offset=2,
        value=json.dumps(make_event().__dict__).encode(),
    )

    def poll(**kwargs):
        projection.stop_event.set()
        return {TopicPartition("lifecycle", 0): [invalid, valid]}

    consumer.poll.side_effect = poll

    projection._consume_once()

    consumer.commit.assert_called_once()
    assert projection.store.recent() == [make_event()]
