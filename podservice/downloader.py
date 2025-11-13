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

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    logger.warning("Pillow not installed, thumbnail conversion disabled")

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

    def __init__(self, output_dir: str, base_url: str, metadata_dir: str, thumbnails_dir: str):
        self.output_dir = Path(output_dir)
        self.base_url = base_url
        self.metadata_dir = Path(metadata_dir)
        self.thumbnails_dir = Path(thumbnails_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.thumbnails_dir.mkdir(parents=True, exist_ok=True)

    def _convert_thumbnail_to_jpeg(self, thumbnail_path: Path) -> Optional[Path]:
        """
        Convert thumbnail to JPEG format for better compatibility.

        Args:
            thumbnail_path: Path to the thumbnail file

        Returns:
            Path to JPEG file, or original path if conversion fails/not needed
        """
        if not HAS_PIL:
            return thumbnail_path

        try:
            # Only convert if not already JPEG
            if thumbnail_path.suffix.lower() in ['.jpg', '.jpeg']:
                return thumbnail_path

            jpeg_path = thumbnail_path.with_suffix('.jpg')

            # Open and convert image
            with Image.open(thumbnail_path) as img:
                # Convert to RGB if necessary (removes alpha channel)
                if img.mode in ('RGBA', 'LA', 'P'):
                    rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    rgb_img.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                    img = rgb_img
                elif img.mode != 'RGB':
                    img = img.convert('RGB')

                # Save as JPEG with high quality
                img.save(jpeg_path, 'JPEG', quality=95, optimize=True)

            # Delete original file
            thumbnail_path.unlink()
            logger.debug(f"Converted thumbnail to JPEG: {jpeg_path.name}")
            return jpeg_path

        except Exception as e:
            logger.warning(f"Failed to convert thumbnail to JPEG: {e}")
            return thumbnail_path

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
                                # Regenerate image URL from current base_url if thumbnail exists
                                image_url = data.get('image_url', '')
                                if image_url:
                                    # Extract just the filename and regenerate URL
                                    image_filename = Path(image_url.split('/')[-1]).name
                                    image_url = f"{self.base_url}/thumbnails/{quote(image_filename)}"
                                return Episode(
                                    title=data.get('title', 'Untitled'),
                                    description=data.get('description', ''),
                                    audio_file=audio_file,
                                    audio_url=audio_url,
                                    pub_date=datetime.fromisoformat(data.get('pub_date', datetime.now().isoformat())),
                                    duration=data.get('duration', 0),
                                    file_size=data.get('file_size', 0),
                                    youtube_url=url,
                                    image_url=image_url,
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
                "writethumbnail": True,  # Download thumbnail
                "noplaylist": True,  # Only download single video, not entire playlist
                "quiet": False,  # Show output for debugging
                "no_warnings": False,  # Show warnings for debugging
                "extract_flat": False,
                "ignoreerrors": False,
            }

            # First pass: Extract info without downloading
            logger.debug(f"Extracting info from URL: {url}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            logger.debug(f"Info extracted successfully")

            if info is None:
                logger.error(f"Could not extract info from {url}")
                return None

            # Get video metadata
            title = info.get("title", "Untitled")
            logger.debug(f"Video title: {title}")
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
            # Set different paths for audio and thumbnail
            ydl_opts["outtmpl"] = {
                "default": str(self.output_dir / f"{safe_title}.%(ext)s"),
                "thumbnail": str(self.thumbnails_dir / f"{safe_title}.%(ext)s"),
            }

            # Second pass: Download with proper filenames
            logger.info(f"Downloading audio for: {title}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            logger.debug(f"Download completed")

            # Find the downloaded audio file (should be .mp3 after post-processing)
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

            # Find the downloaded thumbnail (yt-dlp downloads various formats)
            thumbnail_file = None
            for ext in ['.jpg', '.jpeg', '.png', '.webp']:
                thumb_path = self.thumbnails_dir / f"{safe_title}{ext}"
                if thumb_path.exists():
                    thumbnail_file = thumb_path
                    logger.debug(f"Found thumbnail: {thumb_path.name}")
                    break

            # Convert thumbnail to JPEG for better compatibility
            if thumbnail_file:
                thumbnail_file = self._convert_thumbnail_to_jpeg(thumbnail_file)
            else:
                logger.debug(f"No thumbnail found for: {title}")

            # Get file size
            file_size = audio_file.stat().st_size

            # Generate audio URL (properly URL-encoded)
            audio_url = f"{self.base_url}/audio/{quote(audio_file.name)}"

            # Generate thumbnail URL if thumbnail was downloaded
            image_url = ""
            if thumbnail_file:
                image_url = f"{self.base_url}/thumbnails/{quote(thumbnail_file.name)}"

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
                image_url=image_url,
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
