"""Tests for the S3 feed mirror."""

from datetime import datetime
from xml.etree import ElementTree as ET

import pytest

from podservice.config import S3Config, StorageConfig, load_config
from podservice.feed import Episode, PodcastFeed
from podservice.s3 import S3Mirror

BASE_URL = "http://lan.example:8083"
PUBLIC_URL = "https://cast.example"


class FakePaginator:
    def __init__(self, client):
        self.client = client

    def paginate(self, Bucket, Prefix):
        keys = [k for k in sorted(self.client.objects) if k.startswith(Prefix)]
        yield {"Contents": [{"Key": k} for k in keys]} if keys else {}


class FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.calls = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self)

    def upload_file(self, filename, bucket, key, ExtraArgs):
        self.calls.append(("upload", key))
        with open(filename, "rb") as f:
            self.objects[key] = {"Body": f.read(), **ExtraArgs}

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl):
        self.calls.append(("put", Key))
        self.objects[Key] = {"Body": Body, "ContentType": ContentType}

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.calls.append(("delete", item["Key"]))
            self.objects.pop(item["Key"], None)


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


def add_episode(feed, tmp_path, name, with_thumbnail=True):
    audio_dir = tmp_path / "audio"
    thumbs_dir = tmp_path / "thumbnails"
    audio_dir.mkdir(exist_ok=True)
    thumbs_dir.mkdir(exist_ok=True)
    audio = audio_dir / f"{name}.mp3"
    audio.write_bytes(b"mp3-" + name.encode())
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
            pub_date=datetime(2026, 10, 20),
            file_size=audio.stat().st_size,
            source_url=f"https://example.com/{name}",
            image_url=image_url,
        )
    )


def make_mirror(feed, tmp_path, client):
    return S3Mirror(
        make_config(), feed, thumbnails_dir=str(tmp_path / "thumbnails"), client=client
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

    assert client.calls == []
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
