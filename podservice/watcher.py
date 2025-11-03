"""File watcher for monitoring URL files."""

import logging
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


class URLFileHandler(FileSystemEventHandler):
    """Handler for URL file changes."""

    def __init__(self, file_path: Path, callback: Callable[[str], None]):
        self.file_path = file_path.resolve()
        self.callback = callback
        self.last_processed = 0

    def on_modified(self, event):
        """Handle file modification events."""
        if event.is_directory:
            return

        # Check if the modified file is our target file
        if Path(event.src_path).resolve() == self.file_path:
            # Debounce: don't process too frequently
            current_time = time.time()
            if current_time - self.last_processed < 1:
                logger.debug("Ignoring rapid file change (debouncing)")
                return

            self.last_processed = current_time
            logger.info(f"URL file changed: {event.src_path}")
            self.callback(str(self.file_path))


class URLFileWatcher:
    """Watch URL file for changes."""

    def __init__(self, file_path: str, callback: Callable[[str], None]):
        self.file_path = Path(file_path)
        self.callback = callback
        self.observer = None

        # Ensure parent directory exists
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

        # Create file if it doesn't exist
        if not self.file_path.exists():
            self.file_path.touch()
            logger.info(f"Created URL file: {self.file_path}")

    def start(self):
        """Start watching the file."""
        if not self.file_path.parent.exists():
            logger.error(f"Parent directory does not exist: {self.file_path.parent}")
            return

        logger.info(f"Starting file watcher for: {self.file_path}")

        handler = URLFileHandler(self.file_path, self.callback)
        self.observer = Observer()
        self.observer.schedule(handler, str(self.file_path.parent), recursive=False)
        self.observer.start()

        logger.info(f"File watcher started for: {self.file_path}")

    def stop(self):
        """Stop watching the file."""
        if self.observer:
            logger.info("Stopping file watcher...")
            self.observer.stop()
            self.observer.join()
            logger.info("File watcher stopped")

    def is_alive(self) -> bool:
        """Check if watcher is running."""
        return self.observer is not None and self.observer.is_alive()


def read_urls_from_file(file_path: str) -> list[str]:
    """
    Read URLs from file.

    Args:
        file_path: Path to the URL file

    Returns:
        List of URLs (non-empty lines, excluding comments)
    """
    urls = []

    try:
        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                # Skip empty lines and comments
                if line and not line.startswith("#"):
                    urls.append(line)

        logger.debug(f"Read {len(urls)} URLs from {file_path}")
        return urls

    except FileNotFoundError:
        logger.warning(f"URL file not found: {file_path}")
        return []
    except Exception as e:
        logger.error(f"Error reading URL file {file_path}: {e}", exc_info=True)
        return []


def remove_url_from_file(file_path: str, url: str):
    """
    Remove a URL from the file after successful processing.

    Args:
        file_path: Path to the URL file
        url: URL to remove
    """
    try:
        with open(file_path, "r") as f:
            lines = f.readlines()

        with open(file_path, "w") as f:
            for line in lines:
                if line.strip() != url:
                    f.write(line)

        logger.debug(f"Removed URL from file: {url}")

    except Exception as e:
        logger.error(f"Error removing URL from file {file_path}: {e}", exc_info=True)
