"""Tests for the Cloudflare R2 analytics probe."""

from datetime import datetime, timezone

import pytest
import requests

from podservice.cloudflare import (
    GRAPHQL_URL,
    CloudflareAnalyticsError,
    CloudflareR2Analytics,
    parse_usage,
)


def payload(operations, storage):
    return {
        "data": {
            "viewer": {"accounts": [{"operations": operations, "storage": storage}]}
        }
    }


def test_parse_usage_classifies_operations_and_sums_latest_storage():
    usage = parse_usage(
        payload(
            [
                {"sum": {"requests": 10}, "dimensions": {"actionType": "PutObject"}},
                {"sum": {"requests": 5}, "dimensions": {"actionType": "ListObjects"}},
                {"sum": {"requests": 70}, "dimensions": {"actionType": "GetObject"}},
                {"sum": {"requests": 9}, "dimensions": {"actionType": "DeleteObject"}},
                {"sum": {"requests": 1}, "dimensions": {"actionType": "SomethingNew"}},
            ],
            [
                {
                    "max": {"payloadSize": 500, "metadataSize": 10},
                    "dimensions": {
                        "bucketName": "cast",
                        "datetime": "2026-10-22T00:00:00Z",
                    },
                },
                {
                    "max": {"payloadSize": 900, "metadataSize": 10},
                    "dimensions": {
                        "bucketName": "cast",
                        "datetime": "2026-10-21T00:00:00Z",
                    },
                },
                {
                    "max": {"payloadSize": 200, "metadataSize": 0},
                    "dimensions": {
                        "bucketName": "other",
                        "datetime": "2026-10-22T00:00:00Z",
                    },
                },
            ],
        )
    )

    assert usage.class_a_operations == 16
    assert usage.class_b_operations == 70
    assert usage.storage_bytes == 710
    assert usage.buckets == ("cast", "other")
    assert usage.storage_outside("cast") == 200
    assert usage.storage_outside("unknown") is None


def test_parse_usage_rejects_missing_account():
    with pytest.raises(CloudflareAnalyticsError, match="no account"):
        parse_usage({"data": {"viewer": {"accounts": []}}})


class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"status {self.status}")

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def post(self, url, json, headers, timeout):
        self.requests.append((url, json, headers))
        if self.error:
            raise self.error
        return self.response


def test_usage_queries_last_31_days_with_bearer_token(tmp_path):
    token = tmp_path / "token"
    token.write_text("secret-token\n")
    session = FakeSession(FakeResponse(payload([], [])))
    probe = CloudflareR2Analytics("acct", str(token), session=session)

    usage = probe.usage(now=datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc))

    assert usage.storage_bytes == 0
    url, body, headers = session.requests[0]
    assert url == GRAPHQL_URL
    assert headers["Authorization"] == "Bearer secret-token"
    assert body["variables"] == {
        "account": "acct",
        "since": "2026-09-21T12:00:00Z",
        "until": "2026-10-22T12:00:00Z",
    }


def test_usage_wraps_transport_and_graphql_errors(tmp_path):
    token = tmp_path / "token"
    token.write_text("t")

    probe = CloudflareR2Analytics(
        "acct", str(token), session=FakeSession(error=requests.ConnectionError("down"))
    )
    with pytest.raises(CloudflareAnalyticsError, match="request failed"):
        probe.usage()

    probe = CloudflareR2Analytics(
        "acct",
        str(token),
        session=FakeSession(FakeResponse({"errors": [{"message": "bad token"}]})),
    )
    with pytest.raises(CloudflareAnalyticsError, match="bad token"):
        probe.usage()
