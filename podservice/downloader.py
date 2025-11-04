"""YouTube downloader using yt-dlp."""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import yt_dlp

from .feed import Episode, save_episode_metadata

logger = logging.getLogger(__name__)


def sanitize_filename(filename: str) -> str:
    """Sanitize filename to be filesystem-safe."""
    # Remove or replace problematic characters
    filename = re.sub(r'[<>:"/\\|?*]', "", filename)
    # Replace multiple spaces with single space
    filename = re.sub(r"\s+", " ", filename)
    # Trim and limit length
    filename = filename.strip()[:200]
    return filename


class YouTubeDownloader:
    """Download YouTube videos as audio using yt-dlp."""

    def __init__(self, output_dir: str, base_url: str, metadata_dir: str):
        self.output_dir = Path(output_dir)
        self.base_url = base_url
        self.metadata_dir = Path(metadata_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)

    def download(self, url: str) -> Optional[Episode]:
        """
        Download YouTube video as audio.

        Args:
            url: YouTube video URL

        Returns:
            Episode object if successful, None otherwise
        """
        try:
            logger.info(f"Downloading: {url}")

            # First, check if this video was already downloaded
            # by checking if metadata exists for this URL
            for metadata_file in self.metadata_dir.glob("*.json"):
                try:
                    with open(metadata_file, 'r') as f:
                        data = json.load(f)
                        if data.get('youtube_url') == url:
                            logger.info(f"Video already downloaded, loading existing episode: {data.get('title', 'Unknown')}")
                            # Return the existing episode so URL gets removed from file
                            audio_file = data.get('audio_file', '')
                            if audio_file and Path(audio_file).exists():
                                filename = Path(audio_file).name
                                audio_url = f"{self.base_url}/audio/{quote(filename)}"
                                return Episode(
                                    title=data.get('title', 'Untitled'),
                                    description=data.get('description', ''),
                                    audio_file=audio_file,
                                    audio_url=audio_url,
                                    pub_date=datetime.fromisoformat(data.get('pub_date', datetime.now().isoformat())),
                                    duration=data.get('duration', 0),
                                    file_size=data.get('file_size', 0),
                                    youtube_url=url,
                                )
                except Exception:
                    continue

            # Configure yt-dlp options
            ydl_opts = {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
                "outtmpl": {"default": str(self.output_dir / "%(title)s.%(ext)s")},
                "quiet": True,
                "no_warnings": True,
                "extract_flat": False,
                "ignoreerrors": False,
            }

            # Download and extract metadata
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Extract info first
                info = ydl.extract_info(url, download=False)

                if info is None:
                    logger.error(f"Could not extract info from {url}")
                    return None

                # Get video metadata
                title = info.get("title", "Untitled")
                description = info.get("description", "")
                duration = info.get("duration", 0)
                upload_date_str = info.get("upload_date")

                # Parse upload date
                if upload_date_str:
                    try:
                        pub_date = datetime.strptime(upload_date_str, "%Y%m%d")
                    except ValueError:
                        pub_date = datetime.now()
                else:
                    pub_date = datetime.now()

                # Sanitize title for filename
                safe_title = sanitize_filename(title)

                # Update output template with sanitized filename
                ydl_opts["outtmpl"] = {"default": str(self.output_dir / f"{safe_title}.%(ext)s")}

                # Download the audio
                logger.info(f"Downloading audio for: {title}")
                ydl.download([url])

                # Find the downloaded file (should be .mp3 after post-processing)
                audio_file = self.output_dir / f"{safe_title}.mp3"

                # Sometimes yt-dlp doesn't follow the exact template
                # Try to find the file with a similar name
                if not audio_file.exists():
                    logger.debug(f"Expected file not found: {audio_file}")
                    # Try to find by pattern
                    matches = list(self.output_dir.glob(f"{safe_title}*.mp3"))
                    if matches:
                        audio_file = matches[0]
                        logger.debug(f"Found alternative file: {audio_file}")
                    else:
                        logger.error(f"Could not find downloaded audio file for {title}")
                        return None

                if not audio_file.exists():
                    logger.error(f"Audio file not found after download: {audio_file}")
                    return None

                # Get file size
                file_size = audio_file.stat().st_size

                # Generate audio URL (properly URL-encoded)
                audio_url = f"{self.base_url}/audio/{quote(audio_file.name)}"

                # Create episode
                episode = Episode(
                    title=title,
                    description=description,
                    audio_file=str(audio_file),
                    audio_url=audio_url,
                    pub_date=pub_date,
                    duration=duration,
                    file_size=file_size,
                    youtube_url=url,
                )

                # Save metadata
                metadata_file = self.metadata_dir / f"{audio_file.stem}.json"
                save_episode_metadata(episode, str(metadata_file))

                logger.info(
                    f"Successfully downloaded: {title} ({file_size / (1024*1024):.1f} MB)"
                )

                return episode

        except Exception as e:
            logger.error(f"Error downloading {url}: {e}", exc_info=True)
            return None

    def get_video_info(self, url: str) -> Optional[dict]:
        """Get video information without downloading."""
        try:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": False,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info

        except Exception as e:
            logger.error(f"Error getting video info for {url}: {e}", exc_info=True)
            return None
