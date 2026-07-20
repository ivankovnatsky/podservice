"""Tests for episode application services."""

from datetime import datetime
from unittest.mock import Mock

from podservice.episodes import EpisodeService
from podservice.feed import Episode
from podservice.messaging import DownloadJob


def make_job() -> DownloadJob:
    return DownloadJob(
        job_id="job-1",
        url="https://example.com/episode",
        submitted_at="2026-07-20T00:00:00+00:00",
    )


def test_process_download_adds_episode_to_feed():
    episode = Episode(
        title="Episode",
        description="",
        audio_file="/tmp/episode.mp3",
        audio_url="http://localhost/audio/episode.mp3",
        pub_date=datetime(2026, 7, 20),
    )
    downloader = Mock()
    downloader.download.return_value = episode
    feed = Mock()

    service = EpisodeService(downloader, feed)

    assert service.process_download(make_job()) is True
    downloader.download.assert_called_once_with("https://example.com/episode")
    feed.add_episode.assert_called_once_with(episode)


def test_process_download_reports_failure():
    downloader = Mock()
    downloader.download.return_value = None
    feed = Mock()

    service = EpisodeService(downloader, feed)

    assert service.process_download(make_job()) is False
    feed.add_episode.assert_not_called()
