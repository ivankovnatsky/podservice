"""Episode application services."""

import logging

from .downloader import MediaDownloader
from .feed import PodcastFeed
from .messaging import DownloadJob

logger = logging.getLogger(__name__)


class EpisodeService:
    """Coordinate downloads and podcast feed updates."""

    def __init__(self, downloader: MediaDownloader, feed: PodcastFeed):
        self.downloader = downloader
        self.feed = feed

    def process_download(self, job: DownloadJob) -> bool:
        """Download a job and add the resulting episode to the feed."""
        logger.info("Processing download job %s: %s", job.job_id, job.url)
        episode = self.downloader.download(job.url)
        if episode is None:
            logger.error("Download job %s failed: %s", job.job_id, job.url)
            return False

        self.feed.add_episode(episode)
        logger.info("Download job %s added episode: %s", job.job_id, episode.title)
        return True
