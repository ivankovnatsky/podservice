"""Configuration management for podservice."""

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class ServerConfig:
    """HTTP server configuration."""

    port: int = 8083
    host: str = "0.0.0.0"
    base_url: str = "http://localhost:8083"


@dataclass
class PodcastConfig:
    """Podcast feed metadata."""

    title: str = "My Podcast"
    description: str = "Audio podcast episodes"
    author: str = "Pod Service"
    language: str = "en-us"
    category: str = "Technology"
    image_url: Optional[str] = None


@dataclass
class StorageConfig:
    """Storage paths configuration."""

    data_dir: str = "/tmp/podservice"
    audio_dir: Optional[str] = None
    metadata_dir: Optional[str] = None
    thumbnails_dir: Optional[str] = None

    def __post_init__(self):
        """Set audio_dir, metadata_dir, and thumbnails_dir if not provided and resolve to absolute paths."""
        # Convert to absolute paths
        self.data_dir = os.path.abspath(os.path.expanduser(self.data_dir))

        if self.audio_dir is None:
            self.audio_dir = os.path.join(self.data_dir, "audio")
        else:
            self.audio_dir = os.path.abspath(os.path.expanduser(self.audio_dir))

        if self.metadata_dir is None:
            self.metadata_dir = os.path.join(self.data_dir, "metadata")
        else:
            self.metadata_dir = os.path.abspath(os.path.expanduser(self.metadata_dir))

        if self.thumbnails_dir is None:
            self.thumbnails_dir = os.path.join(self.data_dir, "thumbnails")
        else:
            self.thumbnails_dir = os.path.abspath(os.path.expanduser(self.thumbnails_dir))


@dataclass
class WatchConfig:
    """File watching configuration."""

    file: str = "/tmp/podservice/urls.txt"
    enabled: bool = True

    def __post_init__(self):
        """Resolve to absolute path."""
        self.file = os.path.abspath(os.path.expanduser(self.file))


@dataclass
class ServiceConfig:
    """Main service configuration."""

    server: ServerConfig = field(default_factory=ServerConfig)
    podcast: PodcastConfig = field(default_factory=PodcastConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    log_level: str = "INFO"


def get_default_config_path() -> Path:
    """Get the default configuration file path."""
    home = Path.home()

    if platform.system() == "Darwin":
        # macOS
        return home / "Library" / "Application Support" / "podservice" / "config.yaml"
    else:
        # Linux and other Unix-like systems
        return home / ".config" / "podservice" / "config.yaml"


def load_config(config_path: Optional[str] = None) -> ServiceConfig:
    """Load service configuration from YAML file."""
    if config_path is None:
        config_path = get_default_config_path()
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        # Return default configuration
        return ServiceConfig()

    try:
        with open(config_path, "r") as f:
            data = yaml.safe_load(f) or {}

        # Parse server config
        server_data = data.get("server", {})
        server = ServerConfig(**server_data)

        # Parse podcast config
        podcast_data = data.get("podcast", {})
        podcast = PodcastConfig(**podcast_data)

        # Parse storage config
        storage_data = data.get("storage", {})
        storage = StorageConfig(**storage_data)

        # Parse watch config
        watch_data = data.get("watch", {})
        watch = WatchConfig(**watch_data)

        # Create main config
        config = ServiceConfig(
            server=server,
            podcast=podcast,
            storage=storage,
            watch=watch,
            log_level=data.get("log_level", "INFO"),
        )

        return config

    except Exception as e:
        raise Exception(f"Failed to load configuration from {config_path}: {e}")


def save_config(config: ServiceConfig, config_path: Optional[str] = None) -> None:
    """Save service configuration to YAML file."""
    if config_path is None:
        config_path = get_default_config_path()
    else:
        config_path = Path(config_path)

    # Ensure directory exists
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Convert to dict
    data = {
        "server": {
            "port": config.server.port,
            "host": config.server.host,
            "base_url": config.server.base_url,
        },
        "podcast": {
            "title": config.podcast.title,
            "description": config.podcast.description,
            "author": config.podcast.author,
            "language": config.podcast.language,
            "category": config.podcast.category,
            "image_url": config.podcast.image_url,
        },
        "storage": {
            "data_dir": config.storage.data_dir,
            "audio_dir": config.storage.audio_dir,
            "metadata_dir": config.storage.metadata_dir,
            "thumbnails_dir": config.storage.thumbnails_dir,
        },
        "watch": {
            "file": config.watch.file,
            "enabled": config.watch.enabled,
        },
        "log_level": config.log_level,
    }

    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, indent=2)
