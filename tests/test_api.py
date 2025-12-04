"""Tests for the API endpoints."""

import io
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from podservice.config import ServiceConfig, ServerConfig, PodcastConfig, StorageConfig, WatchConfig
from podservice.feed import PodcastFeed
from podservice.server import PodcastServer


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
        watch=WatchConfig(
            file=os.path.join(temp_data_dir, "urls.txt"),
            enabled=False,
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
def test_server(test_config, test_feed):
    """Create a test server."""
    return PodcastServer(test_config, test_feed)


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
