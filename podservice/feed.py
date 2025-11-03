"""Podcast feed generation."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List
from urllib.parse import quote
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


class Episode:
    """Podcast episode metadata."""

    def __init__(
        self,
        title: str,
        description: str,
        audio_file: str,
        audio_url: str,
        pub_date: datetime,
        duration: int = 0,
        file_size: int = 0,
        youtube_url: str = "",
    ):
        self.title = title
        self.description = description
        self.audio_file = audio_file
        self.audio_url = audio_url
        self.pub_date = pub_date
        self.duration = duration
        self.file_size = file_size
        self.youtube_url = youtube_url
        self.guid = youtube_url or audio_url


class PodcastFeed:
    """Generate and manage podcast RSS feed."""

    def __init__(
        self,
        title: str,
        description: str,
        author: str,
        base_url: str,
        language: str = "en-us",
        category: str = "Technology",
        image_url: str = None,
    ):
        self.title = title
        self.description = description
        self.author = author
        self.base_url = base_url
        self.language = language
        self.category = category
        self.image_url = image_url
        self.episodes: List[Episode] = []

    def add_episode(self, episode: Episode):
        """Add an episode to the feed."""
        # Check if episode already exists (by GUID)
        existing_guids = {ep.guid for ep in self.episodes}
        if episode.guid not in existing_guids:
            self.episodes.append(episode)
            # Sort by pub_date, newest first
            self.episodes.sort(key=lambda x: x.pub_date, reverse=True)
            logger.info(f"Added episode: {episode.title}")
        else:
            logger.debug(f"Episode already exists: {episode.title}")

    def generate_xml(self) -> str:
        """Generate RSS 2.0 XML feed with iTunes extensions."""
        # Create RSS root
        rss = ET.Element("rss")
        rss.set("version", "2.0")
        rss.set("xmlns:itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
        rss.set("xmlns:content", "http://purl.org/rss/1.0/modules/content/")

        # Create channel
        channel = ET.SubElement(rss, "channel")

        # Add channel metadata
        ET.SubElement(channel, "title").text = self.title
        ET.SubElement(channel, "description").text = self.description
        ET.SubElement(channel, "link").text = self.base_url
        ET.SubElement(channel, "language").text = self.language

        # iTunes tags
        ET.SubElement(channel, "itunes:author").text = self.author
        ET.SubElement(channel, "itunes:summary").text = self.description
        ET.SubElement(channel, "itunes:explicit").text = "no"

        # Category
        category_elem = ET.SubElement(channel, "itunes:category")
        category_elem.set("text", self.category)

        # Image
        if self.image_url:
            image = ET.SubElement(channel, "itunes:image")
            image.set("href", self.image_url)

        # Add episodes
        for episode in self.episodes:
            item = ET.SubElement(channel, "item")

            ET.SubElement(item, "title").text = episode.title
            ET.SubElement(item, "description").text = episode.description
            ET.SubElement(item, "itunes:summary").text = episode.description

            # Enclosure (audio file)
            enclosure = ET.SubElement(item, "enclosure")
            enclosure.set("url", episode.audio_url)
            enclosure.set("type", "audio/mpeg")
            enclosure.set("length", str(episode.file_size))

            # Pub date (RFC 822 format)
            pub_date_str = episode.pub_date.strftime("%a, %d %b %Y %H:%M:%S +0000")
            ET.SubElement(item, "pubDate").text = pub_date_str

            # GUID
            guid = ET.SubElement(item, "guid")
            guid.set("isPermaLink", "false")
            guid.text = episode.guid

            # Duration (iTunes format: HH:MM:SS or seconds)
            if episode.duration:
                hours = episode.duration // 3600
                minutes = (episode.duration % 3600) // 60
                seconds = episode.duration % 60
                duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                ET.SubElement(item, "itunes:duration").text = duration_str

            # Link (to original source if available)
            if episode.youtube_url:
                ET.SubElement(item, "link").text = episode.youtube_url

        # Convert to string with proper formatting
        ET.indent(rss, space="  ")
        xml_str = ET.tostring(rss, encoding="unicode", method="xml")

        # Add XML declaration
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_str

    def save_to_file(self, file_path: str):
        """Save feed to file."""
        xml_content = self.generate_xml()
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(xml_content)
        logger.info(f"Saved feed to {file_path}")

    def load_episodes_from_metadata(self, metadata_dir: str):
        """Load episodes from metadata files in directory."""
        metadata_path = Path(metadata_dir)
        if not metadata_path.exists():
            logger.debug(f"Metadata directory does not exist: {metadata_dir}")
            return

        # Find all .json metadata files
        for json_file in metadata_path.glob("*.json"):
            try:
                with open(json_file, "r") as f:
                    data = json.load(f)

                # Get audio URL - fix if it's not properly encoded
                audio_url = data.get("audio_url", "")
                audio_file = data.get("audio_file", "")

                # Convert audio_file to absolute path if relative
                if audio_file:
                    audio_file = os.path.abspath(audio_file)

                # Always regenerate URL with current base_url from config
                # This allows changing base_url without re-downloading
                if audio_file and os.path.exists(audio_file):
                    filename = os.path.basename(audio_file)
                    audio_url = f"{self.base_url}/audio/{quote(filename)}"

                # Create episode from metadata
                episode = Episode(
                    title=data.get("title", "Untitled"),
                    description=data.get("description", ""),
                    audio_file=audio_file,
                    audio_url=audio_url,
                    pub_date=datetime.fromisoformat(data.get("pub_date")),
                    duration=data.get("duration", 0),
                    file_size=data.get("file_size", 0),
                    youtube_url=data.get("youtube_url", ""),
                )

                self.add_episode(episode)

            except Exception as e:
                logger.error(f"Error loading metadata from {json_file}: {e}")

        logger.info(f"Loaded {len(self.episodes)} episodes from metadata")


def save_episode_metadata(episode: Episode, metadata_file: str):
    """Save episode metadata to JSON file."""
    metadata = {
        "title": episode.title,
        "description": episode.description,
        "audio_file": episode.audio_file,
        "audio_url": episode.audio_url,
        "pub_date": episode.pub_date.isoformat(),
        "duration": episode.duration,
        "file_size": episode.file_size,
        "youtube_url": episode.youtube_url,
    }

    os.makedirs(os.path.dirname(metadata_file), exist_ok=True)
    with open(metadata_file, "w") as f:
        json.dump(metadata, f, indent=2)

    logger.debug(f"Saved episode metadata to {metadata_file}")
