"""Tests for the S3 feed mirror."""

from datetime import date, datetime
from xml.etree import ElementTree as ET

import pytest

from podservice.cloudflare import CloudflareAnalyticsError, R2AccountUsage
from podservice.config import S3Config, StorageConfig, load_config
from podservice.feed import Episode, PodcastFeed
from podservice.s3 import BudgetExceeded, OperationLedger, S3Mirror

BASE_URL = "http://lan.example:8083"
PUBLIC_URL = "https://cast.example"


class FakePaginator:
    def __init__(self, client):
        self.client = client

    def paginate(self, Bucket, Prefix=""):
        self.client.calls.append(("list", Prefix))
        keys = [k for k in sorted(self.client.objects) if k.startswith(Prefix)]
        contents = [
            {"Key": k, "Size": len(self.client.objects[k].get("Body", b""))}
            for k in keys
        ]
        yield {"Contents": contents} if keys else {}


class FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.calls = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self)

    def upload_file(self, filename, bucket, key, ExtraArgs, Config=None):
        self.calls.append(("upload", key))
        with open(filename, "rb") as f:
            self.objects[key] = {"Body": f.read(), **ExtraArgs}

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl):
        self.calls.append(("put", Key))
        self.objects[Key] = {"Body": Body, "ContentType": ContentType}

    def delete_object(self, Bucket, Key):
        self.calls.append(("delete", Key))
        self.objects.pop(Key, None)


def make_config(**overrides):
    values = {
        "enabled": True,
        "endpoint_url": "https://account.r2.example",
        "bucket": "test-bucket",
        "public_base_url": PUBLIC_URL + "/",
        "prefix": "/secret/",
    }
    values.update(overrides)
    return S3Config(**values)


def make_feed():
    return PodcastFeed(
        title="Test", description="Test feed", author="Tester", base_url=BASE_URL
    )


def add_episode(feed, tmp_path, name, with_thumbnail=True, size=None, day=20):
    audio_dir = tmp_path / "audio"
    thumbs_dir = tmp_path / "thumbnails"
    audio_dir.mkdir(exist_ok=True)
    thumbs_dir.mkdir(exist_ok=True)
    audio = audio_dir / f"{name}.mp3"
    body = b"mp3-" + name.encode()
    audio.write_bytes(body if size is None else body.ljust(size, b"x"))
    image_url = ""
    if with_thumbnail:
        (thumbs_dir / f"{name}.jpg").write_bytes(b"jpg")
        image_url = f"{BASE_URL}/thumbnails/{name}.jpg"
    feed.add_episode(
        Episode(
            title=name,
            description="",
            audio_file=str(audio),
            audio_url=f"{BASE_URL}/audio/{name}.mp3",
            pub_date=datetime(2026, 10, day),
            file_size=audio.stat().st_size,
            source_url=f"https://example.com/{name}",
            image_url=image_url,
        )
    )


class FakeAnalytics:
    def __init__(self, usage=None, error=None):
        self._usage = usage
        self._error = error

    def usage(self):
        if self._error:
            raise CloudflareAnalyticsError(self._error)
        return self._usage


def make_mirror(feed, tmp_path, client, config=None, analytics=None):
    return S3Mirror(
        config or make_config(),
        feed,
        thumbnails_dir=str(tmp_path / "thumbnails"),
        client=client,
        ledger_path=tmp_path / "db" / "ops.json",
        analytics=analytics,
    )


def test_s3_disabled_by_default(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("storage:\n  data_dir: /tmp/x\n")

    assert load_config(str(config_path)).storage.s3.enabled is False
    assert StorageConfig().s3.enabled is False


def test_s3_config_parsed_from_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "storage:\n"
        "  s3:\n"
        "    enabled: true\n"
        "    endpoint_url: https://account.r2.example\n"
        "    bucket: test-bucket\n"
        "    public_base_url: https://cast.example/\n"
        "    prefix: abc\n"
        "    access_key_id_file: /run/id\n"
        "    secret_access_key_file: /run/secret\n"
    )

    s3 = load_config(str(config_path)).storage.s3

    assert s3.bucket == "test-bucket"
    assert s3.public_prefix_url == "https://cast.example/abc"
    assert s3.key_prefix == "abc/"


def test_enabled_s3_requires_bucket_and_urls():
    with pytest.raises(ValueError, match="bucket"):
        make_config(bucket=None)


def test_credential_files_must_be_paired():
    with pytest.raises(ValueError, match="together"):
        make_config(access_key_id_file="/run/id")


def test_sync_uploads_media_and_public_feed(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "one")
    client = FakeS3()

    make_mirror(feed, tmp_path, client).sync()

    assert client.objects["secret/audio/one.mp3"]["ContentType"] == "audio/mpeg"
    assert client.objects["secret/thumbnails/one.jpg"]["ContentType"] == "image/jpeg"
    # Feed is written after its media.
    assert client.calls[-1] == ("put", "secret/feed.xml")

    xml = client.objects["secret/feed.xml"]["Body"].decode()
    item = ET.fromstring(xml).find("channel/item")
    assert item.find("enclosure").get("url") == f"{PUBLIC_URL}/secret/audio/one.mp3"
    itunes = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
    assert (
        item.find(f"{itunes}image").get("href")
        == f"{PUBLIC_URL}/secret/thumbnails/one.jpg"
    )
    assert BASE_URL not in xml


def test_sync_skips_existing_objects(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "one", with_thumbnail=False)
    client = FakeS3({"secret/audio/one.mp3": {}})

    make_mirror(feed, tmp_path, client).sync()

    assert ("upload", "secret/audio/one.mp3") not in client.calls


def test_sync_deletes_orphaned_media_only(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "one", with_thumbnail=False)
    client = FakeS3(
        {
            "secret/audio/gone.mp3": {},
            "secret/thumbnails/gone.jpg": {},
            "secret/notes.txt": {},
            "other/audio/keep.mp3": {},
        }
    )

    make_mirror(feed, tmp_path, client).sync()

    assert "secret/audio/gone.mp3" not in client.objects
    assert "secret/thumbnails/gone.jpg" not in client.objects
    assert "secret/notes.txt" in client.objects
    assert "other/audio/keep.mp3" in client.objects
    assert client.calls.index(("put", "secret/feed.xml")) < client.calls.index(
        ("delete", "secret/audio/gone.mp3")
    )


def test_empty_local_feed_never_wipes_remote_media(tmp_path):
    client = FakeS3({"secret/audio/one.mp3": {}, "secret/feed.xml": {}})

    make_mirror(make_feed(), tmp_path, client).sync()

    assert client.calls == [("list", "")]
    assert "secret/audio/one.mp3" in client.objects


def test_empty_prefix_accepted():
    config = make_config(prefix=None)

    assert config.key_prefix == ""
    assert config.public_prefix_url == PUBLIC_URL


def test_feed_changes_request_sync(tmp_path):
    feed = make_feed()
    mirror = make_mirror(feed, tmp_path, FakeS3())
    feed.add_listener(mirror.request_sync)

    add_episode(feed, tmp_path, "one")
    assert mirror._pending.is_set()

    mirror._pending.clear()
    feed.clear_episodes()
    assert mirror._pending.is_set()


def test_local_feed_keeps_server_urls(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "one")

    xml = feed.generate_xml()

    assert f"{BASE_URL}/audio/one.mp3" in xml
    assert PUBLIC_URL not in xml


def test_public_feed_omits_episodes_without_audio(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "one", with_thumbnail=False)
    add_episode(feed, tmp_path, "gone", with_thumbnail=False)
    (tmp_path / "audio" / "gone.mp3").unlink()
    client = FakeS3()

    make_mirror(feed, tmp_path, client).sync()

    xml = client.objects["secret/feed.xml"]["Body"].decode()
    assert "one.mp3" in xml
    assert "gone.mp3" not in xml


def test_storage_limit_keeps_newest_episodes(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "old", with_thumbnail=False, size=400, day=1)
    add_episode(feed, tmp_path, "mid", with_thumbnail=False, size=400, day=2)
    add_episode(feed, tmp_path, "new", with_thumbnail=False, size=400, day=3)
    client = FakeS3({"secret/audio/old.mp3": {"Body": b"x" * 400}})
    mirror = make_mirror(feed, tmp_path, client, make_config(max_storage_bytes=900, max_cleanup_percent=50)
    )

    mirror.sync()

    assert set(k for k in client.objects if k.startswith("secret/audio/")) == {
        "secret/audio/new.mp3",
        "secret/audio/mid.mp3",
    }
    xml = client.objects["secret/feed.xml"]["Body"].decode()
    assert "old.mp3" not in xml
    # Old plus new media would not fit, so the eviction happens before uploads.
    assert client.calls.index(("delete", "secret/audio/old.mp3")) < client.calls.index(
        ("upload", "secret/audio/mid.mp3")
    )
    status = mirror.status()
    assert status.mirrored_episodes == 2
    assert status.evicted_titles == ["old"]
    assert status.paused is None


def test_eviction_is_a_contiguous_window_of_newest_episodes(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "old-small", with_thumbnail=False, size=100, day=1)
    add_episode(feed, tmp_path, "mid-big", with_thumbnail=False, size=800, day=2)
    add_episode(feed, tmp_path, "new", with_thumbnail=False, size=400, day=3)
    client = FakeS3()
    mirror = make_mirror(feed, tmp_path, client, make_config(max_storage_bytes=1000))

    mirror.sync()

    assert [k for k in client.objects if k.startswith("secret/audio/")] == [
        "secret/audio/new.mp3"
    ]
    assert mirror.status().evicted_titles == ["mid-big", "old-small"]


def test_oversized_episode_does_not_empty_the_mirror(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "old", with_thumbnail=False, size=300, day=1)
    add_episode(feed, tmp_path, "huge", with_thumbnail=False, size=5000, day=2)
    client = FakeS3({"secret/audio/old.mp3": {"Body": b"x" * 300}})
    mirror = make_mirror(feed, tmp_path, client, make_config(max_storage_bytes=1000))

    mirror.sync()

    assert "secret/audio/old.mp3" in client.objects
    assert "secret/audio/huge.mp3" not in client.objects
    assert mirror.status().evicted_titles == ["huge"]


def test_bucket_without_analytics_sample_counts_whole_account_as_foreign(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "one", with_thumbnail=False, size=400)
    client = FakeS3()
    analytics = FakeAnalytics(R2AccountUsage(700, 0, 0, {"other": 700}))

    make_mirror(
        feed, tmp_path, client, make_config(max_storage_bytes=1000), analytics
    ).sync()

    assert "secret/audio/one.mp3" not in client.objects


def test_unreadable_ledger_blocks_writes(tmp_path):
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "ops.json").write_text("{not json")
    feed = make_feed()
    add_episode(feed, tmp_path, "one", with_thumbnail=False)
    client = FakeS3()

    with pytest.raises(BudgetExceeded, match="unreadable"):
        make_mirror(feed, tmp_path, client).sync()
    assert client.calls == []


def test_foreign_objects_count_against_storage_limit(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "one", with_thumbnail=False, size=400)
    client = FakeS3({"backups/db.sqlite": {"Body": b"x" * 700}})

    make_mirror(feed, tmp_path, client, make_config(max_storage_bytes=1000)).sync()

    assert "secret/audio/one.mp3" not in client.objects
    assert "backups/db.sqlite" in client.objects


def test_storage_fully_used_outside_mirror_pauses(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "one", with_thumbnail=False)
    client = FakeS3({"backups/db.sqlite": {"Body": b"x" * 1000}})

    with pytest.raises(BudgetExceeded, match="outside the mirror"):
        make_mirror(feed, tmp_path, client, make_config(max_storage_bytes=1000)).sync()
    assert not any(call[0] in ("upload", "put") for call in client.calls)


def test_deletes_proceed_when_operation_budget_runs_out_mid_sync(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "old", with_thumbnail=False, size=600, day=1)
    add_episode(feed, tmp_path, "new", with_thumbnail=False, size=600, day=2)
    client = FakeS3({"secret/audio/old.mp3": {"Body": b"x" * 600}})
    # Room for the listing only.
    mirror = make_mirror(
        feed,
        tmp_path,
        client,
        make_config(
            max_storage_bytes=1000, max_class_a_operations=1, max_cleanup_percent=60
        ),
    )

    with pytest.raises(BudgetExceeded, match="class A"):
        mirror.sync()

    assert "secret/audio/old.mp3" not in client.objects


def test_every_request_is_charged_to_the_ledger(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "one")
    client = FakeS3({"secret/audio/gone.mp3": {}})
    mirror = make_mirror(feed, tmp_path, client)

    mirror.sync()

    # list + audio + thumbnail + feed; deletes are free
    assert mirror.ledger.used() == 4
    assert ("delete", "secret/audio/gone.mp3") in client.calls
    assert (tmp_path / "db" / "ops.json").exists()


def test_unchanged_feed_is_not_reuploaded(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "one", with_thumbnail=False)
    client = FakeS3()
    mirror = make_mirror(feed, tmp_path, client)

    mirror.sync()
    mirror.sync()

    assert client.calls.count(("put", "secret/feed.xml")) == 1
    assert mirror.ledger.used() == 4


def test_exhausted_operation_budget_blocks_all_requests(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "one", with_thumbnail=False)
    client = FakeS3()
    mirror = make_mirror(feed, tmp_path, client, make_config(max_class_a_operations=2))

    with pytest.raises(BudgetExceeded, match="class A"):
        mirror.sync()

    # The listing and the upload fit; the feed upload did not and was never sent.
    assert client.calls == [("list", ""), ("upload", "secret/audio/one.mp3")]
    assert mirror.ledger.used() == 2
    with pytest.raises(BudgetExceeded):
        mirror.sync()
    assert len(client.calls) == 2


def test_ledger_persists_and_expires(tmp_path):
    path = tmp_path / "ops.json"
    ledger = OperationLedger(path, limit=10)
    ledger.charge(3, today=date(2026, 9, 1))
    ledger.charge(2, today=date(2026, 10, 1))

    reloaded = OperationLedger(path, limit=10)

    assert reloaded.used(today=date(2026, 10, 1)) == 5
    assert reloaded.used(today=date(2026, 10, 3)) == 2
    with pytest.raises(BudgetExceeded):
        reloaded.charge(9, today=date(2026, 10, 3))
    assert reloaded.used(today=date(2026, 10, 3)) == 2


def test_account_storage_reserves_budget(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "one", with_thumbnail=False, size=400)
    client = FakeS3()
    analytics = FakeAnalytics(R2AccountUsage(700, 0, 0, {"other": 700}))

    mirror = make_mirror(
        feed, tmp_path, client, make_config(max_storage_bytes=1000), analytics
    )
    mirror.sync()

    assert "secret/audio/one.mp3" not in client.objects
    assert mirror.status().account_bytes == 700


def test_lagging_sample_of_own_bucket_does_not_shrink_budget(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "one", with_thumbnail=False, size=400)
    client = FakeS3()
    # Analytics still reports media this service already deleted from its bucket.
    analytics = FakeAnalytics(R2AccountUsage(900, 0, 0, {"test-bucket": 900}))

    make_mirror(
        feed, tmp_path, client, make_config(max_storage_bytes=1000), analytics
    ).sync()

    assert "secret/audio/one.mp3" in client.objects


def test_mirror_can_shrink_when_account_is_over_the_limit(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "old", with_thumbnail=False, size=600, day=1)
    add_episode(feed, tmp_path, "new", with_thumbnail=False, size=600, day=2)
    client = FakeS3(
        {
            "secret/audio/old.mp3": {"Body": b"x" * 600},
            "secret/audio/new.mp3": {"Body": b"x" * 600},
        }
    )
    analytics = FakeAnalytics(R2AccountUsage(1200, 0, 0, {"test-bucket": 1200}))

    make_mirror(
        feed,
        tmp_path,
        client,
        make_config(max_storage_bytes=1000, max_cleanup_percent=60),
        analytics,
    ).sync()

    assert "secret/audio/old.mp3" not in client.objects
    assert "secret/audio/new.mp3" in client.objects


@pytest.mark.parametrize(
    "usage, match",
    [
        (R2AccountUsage(0, 500, 0), "class A"),
        (R2AccountUsage(0, 0, 5000), "class B"),
    ],
)
def test_account_limits_pause_before_any_request(tmp_path, usage, match):
    feed = make_feed()
    add_episode(feed, tmp_path, "one", with_thumbnail=False)
    client = FakeS3()
    config = make_config(
        max_storage_bytes=1000,
        max_class_a_operations=500,
        max_class_b_operations=5000,
    )

    with pytest.raises(BudgetExceeded, match=match):
        make_mirror(feed, tmp_path, client, config, FakeAnalytics(usage)).sync()
    assert client.calls == []


def test_other_buckets_filling_the_account_blocks_writes(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "one", with_thumbnail=False)
    client = FakeS3()
    analytics = FakeAnalytics(R2AccountUsage(1000, 0, 0, {"other": 1000}))

    with pytest.raises(BudgetExceeded, match="outside the mirror"):
        make_mirror(
            feed, tmp_path, client, make_config(max_storage_bytes=1000), analytics
        ).sync()
    assert client.calls == [("list", "")]


def test_missing_token_file_pauses_writes(tmp_path):
    from podservice.cloudflare import CloudflareR2Analytics

    feed = make_feed()
    add_episode(feed, tmp_path, "one", with_thumbnail=False)
    client = FakeS3()
    analytics = CloudflareR2Analytics("acct", str(tmp_path / "missing-token"))

    with pytest.raises(BudgetExceeded, match="usage unknown"):
        make_mirror(feed, tmp_path, client, analytics=analytics).sync()
    assert client.calls == []


def test_unreachable_analytics_pauses_writes(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "one", with_thumbnail=False)
    client = FakeS3()

    with pytest.raises(BudgetExceeded, match="usage unknown"):
        make_mirror(
            feed, tmp_path, client, analytics=FakeAnalytics(error="timeout")
        ).sync()
    assert client.calls == []


def test_cleanup_over_the_cap_pauses_without_deleting(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "old", with_thumbnail=False, size=200, day=1)
    add_episode(feed, tmp_path, "new", with_thumbnail=False, size=900, day=2)
    client = FakeS3({"secret/audio/old.mp3": {"Body": b"x" * 200}})
    mirror = make_mirror(feed, tmp_path, client, make_config(max_storage_bytes=1000))

    with pytest.raises(BudgetExceeded, match="more than 10%"):
        mirror.sync()

    assert "secret/audio/old.mp3" in client.objects
    assert not any(call[0] in ("upload", "put", "delete") for call in client.calls)


def test_cleanup_within_the_cap_proceeds(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "old", with_thumbnail=False, size=100, day=1)
    add_episode(feed, tmp_path, "new", with_thumbnail=False, size=950, day=2)
    client = FakeS3({"secret/audio/old.mp3": {"Body": b"x" * 100}})

    make_mirror(feed, tmp_path, client, make_config(max_storage_bytes=1000)).sync()

    assert "secret/audio/old.mp3" not in client.objects
    assert "secret/audio/new.mp3" in client.objects


def test_cap_ignores_unmirrored_and_locally_deleted_episodes(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "old", with_thumbnail=False, size=900, day=1)
    add_episode(feed, tmp_path, "new", with_thumbnail=False, size=900, day=2)
    # "gone" was removed from the local library; "old" was never uploaded.
    client = FakeS3({"secret/audio/gone.mp3": {"Body": b"x" * 500}})

    make_mirror(feed, tmp_path, client, make_config(max_storage_bytes=1000)).sync()

    assert set(k for k in client.objects if k.startswith("secret/audio/")) == {
        "secret/audio/new.mp3"
    }


def test_cap_ignores_media_shared_with_kept_episodes(tmp_path):
    feed = make_feed()
    add_episode(feed, tmp_path, "old", with_thumbnail=False, size=50, day=1)
    add_episode(feed, tmp_path, "new", with_thumbnail=False, size=900, day=2)
    for episode in feed.episodes:
        episode.image_url = f"{BASE_URL}/thumbnails/shared.jpg"
    (tmp_path / "thumbnails" / "shared.jpg").write_bytes(b"x" * 80)
    client = FakeS3(
        {
            "secret/audio/old.mp3": {"Body": b"x" * 50},
            "secret/thumbnails/shared.jpg": {"Body": b"x" * 80},
        }
    )

    make_mirror(feed, tmp_path, client, make_config(max_storage_bytes=1000)).sync()

    assert "secret/audio/old.mp3" not in client.objects
    assert "secret/thumbnails/shared.jpg" in client.objects


def test_cleanup_percent_must_be_in_range():
    with pytest.raises(ValueError, match="max_cleanup_percent"):
        make_config(max_cleanup_percent=0)
    with pytest.raises(ValueError, match="max_cleanup_percent"):
        make_config(max_cleanup_percent=101)


def test_limits_must_be_positive_and_analytics_paired():
    with pytest.raises(ValueError, match="max_storage_bytes"):
        make_config(max_storage_bytes=0)
    with pytest.raises(ValueError, match="together"):
        make_config(cloudflare_account_id="abc")
    assert make_config().analytics_enabled is False
    assert make_config(
        cloudflare_account_id="abc", cloudflare_api_token_file="/run/token"
    ).analytics_enabled
