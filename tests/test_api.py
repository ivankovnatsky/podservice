"""Tests for the API endpoints."""

import io
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from werkzeug.datastructures import FileStorage, MultiDict

from podservice.config import PodcastConfig, ServerConfig, ServiceConfig, StorageConfig
from podservice.events import DatabaseStatus, KafkaStatus, LifecycleEvent
from podservice.feed import PodcastFeed, save_episode_metadata
from podservice.messaging import DownloadJob, MessagePublishError, PartialPublishError
from podservice.server import PodcastServer
from podservice.status import RabbitMQQueueStatus, RabbitMQStatus


@pytest.fixture
def temp_data_dir():
    """Create a temporary directory for test data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def test_config(temp_data_dir):
    """Create a test configuration."""
    return ServiceConfig(
        server=ServerConfig(
            port=8083,
            host="127.0.0.1",
            base_url="http://localhost:8083",
        ),
        podcast=PodcastConfig(
            title="Test Podcast",
            description="Test Description",
            author="Test Author",
        ),
        storage=StorageConfig(
            data_dir=temp_data_dir,
        ),
    )


@pytest.fixture
def test_feed(test_config):
    """Create a test feed."""
    return PodcastFeed(
        title=test_config.podcast.title,
        description=test_config.podcast.description,
        author=test_config.podcast.author,
        base_url=test_config.server.base_url,
    )


@pytest.fixture
def submit_urls():
    """Create a download job submission mock."""

    def create_jobs(urls):
        return [
            DownloadJob(
                job_id=f"job-{index}",
                url=url,
                submitted_at="2026-07-20T00:00:00+00:00",
            )
            for index, url in enumerate(urls, start=1)
        ]

    return Mock(side_effect=create_jobs)


@pytest.fixture
def test_server(test_config, test_feed, submit_urls):
    """Create a test server."""
    return PodcastServer(test_config, test_feed, submit_urls=submit_urls)


@pytest.fixture
def client(test_server):
    """Create a test client."""
    test_server.app.config["TESTING"] = True
    with test_server.app.test_client() as client:
        yield client


class TestCreateEpisodeAPI:
    """Tests for POST /api/episodes endpoint."""

    def test_create_episode_success(self, client, temp_data_dir):
        """Test successful episode creation."""
        # Create a minimal MP3 file (just bytes, not real audio)
        audio_data = b"fake mp3 data" * 100

        response = client.post(
            "/api/episodes",
            data={
                "audio": (io.BytesIO(audio_data), "test_episode.mp3"),
                "title": "Test Episode Title",
                "description": "This is a test description",
                "source_url": "https://example.com/article",
            },
            content_type="multipart/form-data",
        )

        assert response.status_code == 201
        data = json.loads(response.data)
        assert data["success"] is True
        assert data["episode"]["title"] == "Test Episode Title"
        assert "audio_url" in data["episode"]
        assert data["episode"]["guid"] == "https://example.com/article"

        # Verify file was saved
        audio_dir = Path(temp_data_dir) / "audio"
        audio_files = list(audio_dir.glob("*.mp3"))
        assert len(audio_files) == 1

        # Verify metadata was saved
        metadata_dir = Path(temp_data_dir) / "metadata"
        metadata_files = list(metadata_dir.glob("*.json"))
        assert len(metadata_files) == 1

    def test_create_episode_missing_audio(self, client):
        """Test error when audio file is missing."""
        response = client.post(
            "/api/episodes",
            data={"title": "Test Episode"},
            content_type="multipart/form-data",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["success"] is False
        assert "audio" in data["error"].lower()

    def test_create_episode_missing_title(self, client):
        """Test error when title is missing."""
        audio_data = b"fake mp3 data"

        response = client.post(
            "/api/episodes",
            data={
                "audio": (io.BytesIO(audio_data), "test.mp3"),
            },
            content_type="multipart/form-data",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["success"] is False
        assert "title" in data["error"].lower()

    def test_create_episode_with_pub_date(self, client, temp_data_dir):
        """Test episode creation with custom pub_date."""
        audio_data = b"fake mp3 data" * 100
        pub_date = "2025-01-15T10:30:00"

        response = client.post(
            "/api/episodes",
            data={
                "audio": (io.BytesIO(audio_data), "test.mp3"),
                "title": "Episode with Date",
                "pub_date": pub_date,
            },
            content_type="multipart/form-data",
        )

        assert response.status_code == 201
        data = json.loads(response.data)
        assert data["success"] is True
        assert "2025-01-15" in data["episode"]["pub_date"]

    def test_create_episode_invalid_pub_date(self, client):
        """Test error with invalid pub_date format."""
        audio_data = b"fake mp3 data"

        response = client.post(
            "/api/episodes",
            data={
                "audio": (io.BytesIO(audio_data), "test.mp3"),
                "title": "Test Episode",
                "pub_date": "not-a-date",
            },
            content_type="multipart/form-data",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["success"] is False
        assert "pub_date" in data["error"].lower()

    def test_create_episode_duplicate_source_url(self, client, temp_data_dir):
        """Test duplicate detection via source_url."""
        audio_data = b"fake mp3 data" * 100
        source_url = "https://example.com/duplicate-article"

        # Create first episode
        response1 = client.post(
            "/api/episodes",
            data={
                "audio": (io.BytesIO(audio_data), "test1.mp3"),
                "title": "First Episode",
                "source_url": source_url,
            },
            content_type="multipart/form-data",
        )
        assert response1.status_code == 201

        # Try to create duplicate
        response2 = client.post(
            "/api/episodes",
            data={
                "audio": (io.BytesIO(audio_data), "test2.mp3"),
                "title": "Duplicate Episode",
                "source_url": source_url,
            },
            content_type="multipart/form-data",
        )

        assert response2.status_code == 409
        data = json.loads(response2.data)
        assert data["success"] is True  # 409 is treated as success
        assert "already exists" in data.get("message", "").lower()

    def test_create_episode_without_source_url(self, client, temp_data_dir):
        """Test episode creation without source_url uses audio_url as GUID."""
        audio_data = b"fake mp3 data" * 100

        response = client.post(
            "/api/episodes",
            data={
                "audio": (io.BytesIO(audio_data), "test.mp3"),
                "title": "No Source URL Episode",
            },
            content_type="multipart/form-data",
        )

        assert response.status_code == 201
        data = json.loads(response.data)
        assert data["success"] is True
        # GUID should be the audio_url when no source_url provided
        assert data["episode"]["guid"] == data["episode"]["audio_url"]

    def test_create_episode_filename_collision(self, client, temp_data_dir):
        """Test handling of filename collisions."""
        audio_data = b"fake mp3 data" * 100

        # Create first episode
        response1 = client.post(
            "/api/episodes",
            data={
                "audio": (io.BytesIO(audio_data), "test.mp3"),
                "title": "Same Title",
                "source_url": "https://example.com/article1",
            },
            content_type="multipart/form-data",
        )
        assert response1.status_code == 201

        # Create second episode with same title but different source_url
        response2 = client.post(
            "/api/episodes",
            data={
                "audio": (io.BytesIO(audio_data), "test.mp3"),
                "title": "Same Title",
                "source_url": "https://example.com/article2",
            },
            content_type="multipart/form-data",
        )
        assert response2.status_code == 201

        # Both files should exist with different names
        audio_dir = Path(temp_data_dir) / "audio"
        audio_files = list(audio_dir.glob("*.mp3"))
        assert len(audio_files) == 2

    def test_create_episode_various_audio_formats(self, client, temp_data_dir):
        """Test support for various audio file formats."""
        audio_data = b"fake audio data" * 100

        for ext in [".mp3", ".m4a", ".opus", ".wav"]:
            response = client.post(
                "/api/episodes",
                data={
                    "audio": (io.BytesIO(audio_data), f"test{ext}"),
                    "title": f"Test {ext} Episode",
                    "source_url": f"https://example.com/article{ext}",
                },
                content_type="multipart/form-data",
            )
            assert response.status_code == 201, f"Failed for format {ext}"

    @patch("podservice.server.download_image")
    def test_create_episode_with_image_url(self, mock_download, client, temp_data_dir):
        """Test episode creation with image_url download."""
        audio_data = b"fake mp3 data" * 100

        # Mock the download_image function to return a fake path
        thumbnails_dir = Path(temp_data_dir) / "thumbnails"
        thumbnails_dir.mkdir(parents=True, exist_ok=True)
        fake_thumbnail = thumbnails_dir / "test-episode.jpg"
        fake_thumbnail.write_bytes(b"fake image data")
        mock_download.return_value = fake_thumbnail

        response = client.post(
            "/api/episodes",
            data={
                "audio": (io.BytesIO(audio_data), "test.mp3"),
                "title": "Test Episode",
                "image_url": "https://example.com/image.png",
            },
            content_type="multipart/form-data",
        )

        assert response.status_code == 201
        data = json.loads(response.data)
        assert data["success"] is True
        assert data["episode"]["image_url"] != ""

        # Verify download_image was called
        mock_download.assert_called_once()


class TestQueueURLAPI:
    """Tests for URL job submission."""

    def test_queue_single_url(self, client, submit_urls):
        response = client.post(
            "/api/urls",
            json={"url": "https://example.com/episode"},
        )

        assert response.status_code == 202
        assert response.json["jobs"] == [
            {
                "job_id": "job-1",
                "url": "https://example.com/episode",
            }
        ]
        submit_urls.assert_called_once_with(["https://example.com/episode"])

    def test_queue_multiple_urls(self, client, submit_urls):
        urls = ["https://example.com/one", "https://example.com/two"]
        response = client.post("/api/urls", json={"urls": urls})

        assert response.status_code == 202
        assert response.json["count"] == 2
        assert [job["job_id"] for job in response.json["jobs"]] == [
            "job-1",
            "job-2",
        ]
        submit_urls.assert_called_once_with(urls)

    def test_reject_invalid_url(self, client, submit_urls):
        response = client.post("/api/urls", json={"url": "not-a-url"})

        assert response.status_code == 400
        submit_urls.assert_not_called()

    def test_reject_non_string_url(self, client, submit_urls):
        response = client.post("/api/urls", json={"url": 42})

        assert response.status_code == 400
        submit_urls.assert_not_called()

    @pytest.mark.parametrize(
        ("body", "content_type"),
        [
            ("{invalid", "application/json"),
            ("[]", "application/json"),
            ("plain text", "text/plain"),
        ],
    )
    def test_reject_invalid_json_body(self, client, submit_urls, body, content_type):
        response = client.post(
            "/api/urls",
            data=body,
            content_type=content_type,
        )

        assert response.status_code == 400
        assert response.json["error"] == "Request body must be a JSON object"
        submit_urls.assert_not_called()

    def test_report_queue_unavailable(self, client, submit_urls):
        submit_urls.side_effect = MessagePublishError("unavailable")

        response = client.post(
            "/api/urls",
            json={"url": "https://example.com/episode"},
        )

        assert response.status_code == 503
        assert response.json == {
            "success": False,
            "error": "Download queue is unavailable",
        }

    def test_report_partially_accepted_batch(self, client, submit_urls):
        submit_urls.side_effect = PartialPublishError(
            [
                DownloadJob(
                    job_id="accepted-1",
                    url="https://example.com/one",
                    submitted_at="2026-07-20T00:00:00+00:00",
                )
            ],
            [
                DownloadJob(
                    job_id="unaccepted-2",
                    url="https://example.com/two",
                    submitted_at="2026-07-20T00:00:00+00:00",
                )
            ],
        )

        response = client.post(
            "/api/urls",
            json={
                "urls": [
                    "https://example.com/one",
                    "https://example.com/two",
                ]
            },
        )

        assert response.status_code == 503
        assert response.json["accepted_jobs"] == [
            {"job_id": "accepted-1", "url": "https://example.com/one"}
        ]
        assert response.json["unaccepted_urls"] == ["https://example.com/two"]


class TestIndexEpisodes:
    def test_index_shows_episode_row_and_view_all(self, client, test_config):
        audio_dir = Path(test_config.storage.audio_dir)
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / "episode.mp3").write_bytes(b"audio")
        metadata_dir = Path(test_config.storage.metadata_dir)
        metadata_dir.mkdir(parents=True, exist_ok=True)
        (metadata_dir / "episode.json").write_text(json.dumps({"title": "First Show"}))

        response = client.get("/")

        assert b"First Show" in response.data
        assert b'class="view-all" href="/episodes"' in response.data
        assert b'href="/audio/episode.mp3"' in response.data

    def test_index_without_episodes(self, client):
        response = client.get("/")

        assert b"No episodes yet." in response.data

    def test_index_escapes_episode_titles(self, client, test_config):
        audio_dir = Path(test_config.storage.audio_dir)
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / "episode.mp3").write_bytes(b"audio")
        metadata_dir = Path(test_config.storage.metadata_dir)
        metadata_dir.mkdir(parents=True, exist_ok=True)
        (metadata_dir / "episode.json").write_text(
            json.dumps({"title": "<script>alert(1)</script>"})
        )

        response = client.get("/")

        assert b"<script>alert(1)</script>" not in response.data
        assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in response.data


class TestHtmlEscaping:
    def test_root_escapes_query_messages(self, client):
        response = client.get("/", query_string={"error": "<script>alert(1)</script>"})

        assert b"<script>alert(1)</script>" not in response.data
        assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in response.data

    def test_episode_list_escapes_query_messages(self, client, test_config):
        audio_dir = Path(test_config.storage.audio_dir)
        audio_dir.mkdir(parents=True, exist_ok=True)
        filename = 'episode"><img src=x onerror=alert(2)>.mp3'
        (audio_dir / filename).write_bytes(b"audio")
        metadata_dir = Path(test_config.storage.metadata_dir)
        metadata_dir.mkdir(parents=True, exist_ok=True)
        (metadata_dir / f"{Path(filename).stem}.json").write_text(
            json.dumps({"title": "<script>alert(1)</script>"})
        )

        response = client.get(
            "/episodes",
            query_string={"error": "<script>alert(1)</script>"},
        )

        assert b"<script>alert(1)</script>" not in response.data
        assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in response.data
        assert b"<img src=x onerror=alert(2)>" not in response.data

    def test_delete_all_requires_csrf_token(self, client, test_config):
        audio_dir = Path(test_config.storage.audio_dir)
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_file = audio_dir / "episode.mp3"
        audio_file.write_bytes(b"audio")

        response = client.post("/delete-all-episodes")

        assert response.status_code == 403
        assert audio_file.exists()

    def test_delete_all_accepts_csrf_token(self, client, test_config, test_server):
        audio_dir = Path(test_config.storage.audio_dir)
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_file = audio_dir / "episode.mp3"
        audio_file.write_bytes(b"audio")

        response = client.post(
            "/delete-all-episodes",
            data={"csrf_token": test_server.csrf_token},
        )

        assert response.status_code == 302
        assert not audio_file.exists()


class TestAudioUpload:
    def _upload(self, server, files):
        server.app.config["TESTING"] = True
        with server.app.test_client() as client:
            return client.post(
                "/upload-audio",
                data={
                    "csrf_token": server.csrf_token,
                    "audio": files,
                },
                content_type="multipart/form-data",
            )

    def test_uploads_emit_file_scoped_events_sharing_a_batch(
        self, test_config, test_feed
    ):
        emit = Mock()
        server = PodcastServer(test_config, test_feed, emit_upload_event=emit)

        response = self._upload(
            server,
            [
                (io.BytesIO(b"audio-one"), "first.mp3"),
                (io.BytesIO(b"audio-two"), "second.mp3"),
            ],
        )

        assert response.status_code == 302
        stored = [
            call.kwargs
            for call in emit.call_args_list
            if call.kwargs["event_type"] == "upload.stored"
        ]
        assert [call["filename"] for call in stored] == ["first.mp3", "second.mp3"]
        # One batch, distinct per-file jobs, so a partial batch stays legible.
        assert len({call["batch_id"] for call in stored}) == 1
        assert len({call["job_id"] for call in stored}) == 2

    def test_one_failing_file_does_not_abort_the_batch(self, test_config, test_feed):
        emit = Mock()
        server = PodcastServer(test_config, test_feed, emit_upload_event=emit)
        real_save = save_episode_metadata

        def fail_second(episode, path):
            if "second" in str(path):
                raise OSError("disk full")
            return real_save(episode, path)

        with patch("podservice.server.save_episode_metadata", side_effect=fail_second):
            response = self._upload(
                server,
                [
                    (io.BytesIO(b"audio-one"), "first.mp3"),
                    (io.BytesIO(b"audio-two"), "second.mp3"),
                    (io.BytesIO(b"audio-three"), "third.mp3"),
                ],
            )

        assert response.status_code == 302
        by_type = {}
        for call in emit.call_args_list:
            by_type.setdefault(call.kwargs["event_type"], []).append(
                call.kwargs["filename"]
            )
        assert by_type["upload.stored"] == ["first.mp3", "third.mp3"]
        assert by_type["upload.failed"] == ["second.mp3"]

        failed = next(
            call.kwargs
            for call in emit.call_args_list
            if call.kwargs["event_type"] == "upload.failed"
        )
        assert "disk full" in failed["detail"]

    def test_interrupted_upload_leaves_no_partial_file(self, test_config, test_feed):
        server = PodcastServer(test_config, test_feed, emit_upload_event=Mock())

        with patch(
            "podservice.server.save_episode_metadata", side_effect=OSError("boom")
        ):
            self._upload(server, [(io.BytesIO(b"audio"), "broken.mp3")])

        audio_dir = Path(test_config.storage.audio_dir)
        assert not list(audio_dir.glob("*.partial"))

    def test_failure_after_rename_leaves_no_orphaned_audio(
        self, test_config, test_feed
    ):
        server = PodcastServer(test_config, test_feed, emit_upload_event=Mock())

        # Fails after the audio file is already renamed into place.
        with patch(
            "podservice.server.save_episode_metadata", side_effect=OSError("boom")
        ):
            self._upload(server, [(io.BytesIO(b"audio"), "orphan.mp3")])

        audio_dir = Path(test_config.storage.audio_dir)
        assert not list(audio_dir.glob("*.mp3"))
        assert not list(Path(test_config.storage.metadata_dir).glob("*.json"))

    def test_failure_in_feed_removes_written_metadata(self, test_config, test_feed):
        server = PodcastServer(test_config, test_feed, emit_upload_event=Mock())

        with patch.object(test_feed, "add_episode", side_effect=RuntimeError("nope")):
            self._upload(server, [(io.BytesIO(b"audio"), "late.mp3")])

        assert not list(Path(test_config.storage.audio_dir).glob("*.mp3"))
        assert not list(Path(test_config.storage.metadata_dir).glob("*.json"))

    def test_events_record_the_filename_as_submitted(self, test_config, test_feed):
        emit = Mock()
        server = PodcastServer(test_config, test_feed, emit_upload_event=emit)
        # Sanitizing this name changes it, so the event must not carry that form.
        submitted = "Rooftop Laundry, Malta 28sqm.mp3"

        self._upload(server, [(io.BytesIO(b"audio"), submitted)])

        assert [call.kwargs["filename"] for call in emit.call_args_list] == [
            submitted,
            submitted,
        ]
        # The file on disk still uses the sanitized name.
        stored = list(Path(test_config.storage.audio_dir).glob("*.mp3"))
        assert len(stored) == 1
        assert stored[0].name != submitted

    def test_none_filename_is_skipped_without_aborting_the_batch(
        self, test_config, test_feed
    ):
        emit = Mock()
        server = PodcastServer(test_config, test_feed, emit_upload_event=emit)
        server.app.config["TESTING"] = True

        # Werkzeug yields filename=None for a part sent without one, which the
        # test client will not reproduce from a tuple, so inject it directly.
        parts = [
            FileStorage(io.BytesIO(b"one"), filename="first.mp3"),
            FileStorage(io.BytesIO(b"two"), filename=None),
            FileStorage(io.BytesIO(b"three"), filename="third.mp3"),
        ]

        with server.app.test_client() as client:
            with patch.object(MultiDict, "getlist", return_value=parts):
                response = client.post(
                    "/upload-audio",
                    data={"csrf_token": server.csrf_token},
                    content_type="multipart/form-data",
                )

        assert response.status_code == 302
        stored = [
            call.kwargs["filename"]
            for call in emit.call_args_list
            if call.kwargs["event_type"] == "upload.stored"
        ]
        assert stored == ["first.mp3", "third.mp3"]

    def test_upload_needs_no_csrf_token(self, test_config, test_feed):
        emit = Mock()
        server = PodcastServer(test_config, test_feed, emit_upload_event=emit)
        server.app.config["TESTING"] = True

        # Matches /api/episodes, which accepts the same upload unauthenticated.
        with server.app.test_client() as client:
            response = client.post(
                "/upload-audio",
                data={"audio": (io.BytesIO(b"audio"), "tokenless.mp3")},
                content_type="multipart/form-data",
            )

        assert response.status_code == 302
        assert list(Path(test_config.storage.audio_dir).glob("*.mp3"))

    def test_partial_batch_redirect_is_url_encoded(self, test_config, test_feed):
        server = PodcastServer(test_config, test_feed, emit_upload_event=Mock())

        def fail_second(episode, path):
            if "second" in str(path):
                raise OSError("disk full")
            return save_episode_metadata(episode, path)

        with patch("podservice.server.save_episode_metadata", side_effect=fail_second):
            response = self._upload(
                server,
                [
                    (io.BytesIO(b"one"), "first.mp3"),
                    (io.BytesIO(b"two"), "second.mp3"),
                ],
            )

        assert " " not in response.headers["Location"]

    def test_upload_works_without_an_event_emitter(self, test_config, test_feed):
        server = PodcastServer(test_config, test_feed)

        response = self._upload(server, [(io.BytesIO(b"audio"), "solo.mp3")])

        assert response.status_code == 302
        assert list(Path(test_config.storage.audio_dir).glob("*.mp3"))


class TestMessagingStatus:
    def test_dashboard_and_json_report_broker_state(self, test_config, test_feed):
        rabbitmq = RabbitMQStatus(
            connected=True,
            version="4.2.5",
            queues=(
                RabbitMQQueueStatus(
                    name="podservice.downloads",
                    role="Downloads",
                    ready=2,
                    unacknowledged=1,
                    consumers=1,
                    state="running",
                ),
            ),
        )
        kafka = KafkaStatus(
            connected=True,
            broker_count=1,
            topic_exists=True,
            partition_count=1,
            consumer_lag=3,
        )
        event = LifecycleEvent(
            event_id="event-1",
            event_type="download.succeeded",
            occurred_at="2026-07-20T12:00:00+00:00",
            job_id="job-1",
            source="https://example.com/episode?a=1&b=2",
            attempt=0,
        )
        server = PodcastServer(
            test_config,
            test_feed,
            rabbitmq_status=Mock(return_value=rabbitmq),
            kafka_status=Mock(return_value=kafka),
            database_status=Mock(
                return_value=DatabaseStatus(
                    connected=True,
                    path="/data/db/podservice.sqlite3",
                    size_bytes=4096,
                    event_count=1,
                    outbox_pending=0,
                    last_event_at=event.occurred_at,
                )
            ),
            recent_events=Mock(return_value=[event]),
        )
        server.app.config["TESTING"] = True

        with server.app.test_client() as status_client:
            dashboard = status_client.get("/status")
            api = status_client.get("/api/status")

        assert dashboard.status_code == 200
        assert b"Data Status" in dashboard.data
        assert dashboard.data.index(b'<a href="/">') < dashboard.data.index(
            b"Data Status</h1>"
        )
        assert b"SQLite" in dashboard.data
        assert b"RabbitMQ queues" in dashboard.data
        assert b"Recent Kafka lifecycle events" in dashboard.data
        assert b"https://example.com/episode?a=1&amp;b=2" in dashboard.data
        assert api.json["rabbitmq"]["ready"] == 2
        assert api.json["kafka"]["consumer_lag"] == 3
        assert api.json["database"]["event_count"] == 1
        assert api.json["events"][0]["event_id"] == "event-1"

    def test_headphones_favicon(self, client):
        response = client.get("/favicon.svg")

        assert response.status_code == 200
        assert response.mimetype == "image/svg+xml"
        assert b'<path d="M14 35v-5' in response.data


class TestUtilsFunctions:
    """Tests for utility functions."""

    def test_sanitize_filename(self):
        """Test filename sanitization."""
        from podservice.utils import sanitize_filename

        assert sanitize_filename("Normal Title") == "Normal Title"
        assert sanitize_filename('Title: With "Quotes"') == "Title With Quotes"
        assert sanitize_filename("Path/To\\File") == "PathToFile"
        assert sanitize_filename("Multiple   Spaces") == "Multiple Spaces"
        assert sanitize_filename("  Leading and Trailing  ") == "Leading and Trailing"

        # Test length limiting
        long_title = "A" * 300
        assert len(sanitize_filename(long_title)) == 200
