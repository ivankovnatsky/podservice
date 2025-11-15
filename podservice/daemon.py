"""Main daemon service."""

import logging
import signal
import sys
import time
from pathlib import Path

from .config import ServiceConfig, load_config
from .downloader import YouTubeDownloader
from .feed import PodcastFeed
from .server import PodcastServer
from .watcher import URLFileWatcher, read_urls_from_file, remove_url_from_file

logger = logging.getLogger(__name__)


class PodService:
    """Main pod service daemon."""

    def __init__(self, config: ServiceConfig):
        self.config = config
        self.running = False

        # Ensure directories exist
        Path(self.config.storage.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.config.storage.audio_dir).mkdir(parents=True, exist_ok=True)

        # Create metadata directory
        self.metadata_dir = Path(self.config.storage.data_dir) / "metadata"
        self.metadata_dir.mkdir(parents=True, exist_ok=True)

        # Ensure watch file exists
        if self.config.watch.enabled:
            watch_file = Path(self.config.watch.file)
            watch_file.parent.mkdir(parents=True, exist_ok=True)
            watch_file.touch(exist_ok=True)

        # Initialize components
        self.feed = PodcastFeed(
            title=self.config.podcast.title,
            description=self.config.podcast.description,
            author=self.config.podcast.author,
            base_url=self.config.server.base_url,
            language=self.config.podcast.language,
            category=self.config.podcast.category,
            image_url=self.config.podcast.image_url,
        )

        # Load existing episodes from metadata
        self.feed.load_episodes_from_metadata(str(self.metadata_dir))

        # Initialize downloader
        self.downloader = YouTubeDownloader(
            output_dir=self.config.storage.audio_dir,
            base_url=self.config.server.base_url,
            metadata_dir=str(self.metadata_dir),
            thumbnails_dir=self.config.storage.thumbnails_dir,
        )

        # Initialize server
        self.server = PodcastServer(self.config, self.feed)

        # Initialize file watcher
        self.watcher = None
        if self.config.watch.enabled:
            self.watcher = URLFileWatcher(
                file_path=self.config.watch.file,
                callback=self._process_url_file,
            )

        # Set up signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info(f"Received signal {signum}, shutting down...")
        self.stop()

    def _process_url_file(self, file_path: str):
        """Process URLs from the watched file."""
        logger.info(f"Processing URL file: {file_path}")

        urls = read_urls_from_file(file_path)

        if not urls:
            logger.debug("No URLs to process")
            return

        logger.info(f"Found {len(urls)} URL(s) to process")

        for url in urls:
            try:
                logger.info(f"Processing URL: {url}")

                # Download video as audio
                episode = self.downloader.download(url)

                if episode:
                    # Add to feed
                    self.feed.add_episode(episode)
                    logger.info(f"Added episode to feed: {episode.title}")

                    # Remove URL from file after successful processing
                    remove_url_from_file(file_path, url)
                    logger.info(f"Removed processed URL from file: {url}")
                else:
                    logger.error(f"Failed to download: {url}")

            except Exception as e:
                logger.error(f"Error processing URL {url}: {e}", exc_info=True)

        logger.info("Finished processing URLs")

    def start(self):
        """Start the service daemon."""
        logger.info("Starting Pod Service...")
        logger.info(f"Configuration:")
        logger.info(f"  Server: {self.config.server.base_url}")
        logger.info(f"  Port: {self.config.server.port}")
        logger.info(f"  Audio directory: {self.config.storage.audio_dir}")
        logger.info(f"  Watch file: {self.config.watch.file}")
        logger.info(f"  Podcast: {self.config.podcast.title}")

        self.running = True

        try:
            # Start HTTP server
            self.server.start()

            # Start file watcher
            if self.watcher:
                self.watcher.start()

                # Process existing URLs once at startup
                self._process_url_file(self.config.watch.file)

                # Main loop - just keep the service alive
                logger.info("Service running. Watching for URL changes...")
                while self.running:
                    time.sleep(1)
            else:
                logger.info("File watching disabled. Server running in standalone mode.")
                logger.info(f"Add episodes by placing URLs in {self.config.watch.file}")
                while self.running:
                    time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Service interrupted by user")
        except Exception as e:
            logger.error(f"Service error: {e}", exc_info=True)
        finally:
            self.cleanup()

    def stop(self):
        """Stop the service daemon."""
        self.running = False

    def cleanup(self):
        """Cleanup resources."""
        logger.info("Cleaning up...")

        if self.watcher:
            self.watcher.stop()

        self.server.stop()

        logger.info("Pod Service stopped")


def run_service(config_path: str = None, foreground: bool = True):
    """Run the pod service daemon."""
    try:
        config = load_config(config_path)
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        sys.exit(1)

    # Set up logging
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # Configure stdout handler for INFO and below
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.setFormatter(logging.Formatter(log_format))
    stdout_handler.addFilter(lambda record: record.levelno <= logging.INFO)

    # Configure stderr handler for WARNING and above
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(logging.Formatter(log_format))

    handlers = [stdout_handler, stderr_handler]

    logging.basicConfig(level=log_level, format=log_format, handlers=handlers)

    logger.info(
        f"Pod Service starting in {'foreground' if foreground else 'daemon'} mode"
    )

    # Create and start service
    service = PodService(config)
    service.start()
