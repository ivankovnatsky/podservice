# Pod Service

Podcast Feed Service - Convert media URLs to podcast episodes.

A lightweight Python service that watches a file for URLs (YouTube, Substack,
or any yt-dlp supported source), downloads them as audio, and serves them as
a podcast feed compatible with Apple Podcasts and other podcast players.

## Features

- 🎙️ **HTTP server** serving a podcast RSS feed with iTunes extensions
- 👀 **File watching** for automatic URL processing
- 📥 **Automatic download** using yt-dlp (YouTube, Substack, and many other sources)
- 🔄 **Real-time updates** - new episodes appear immediately
- 📱 **Apple Podcast compatible** feed
- 🚀 **NixOS/nix-darwin** service module for easy deployment
- 🔒 **Lightweight** - simple Python service with minimal dependencies

## Installation

```bash
# Clone the repo
git clone https://github.com/ivankovnatsky/podservice
cd podservice

# Using Nix (recommended)
make dev

# Or with poetry
poetry install
```

## Quick Start (Local Development)

The fastest way to try it out:

```bash
# Start the service (automatically creates ./data directory)
make serve
```

**Add URLs:**

Option 1 - Web interface (easiest):
- Open http://localhost:8083 in your browser
- Paste any URL and click "Add to Podcast"

Option 2 - Command line:
```bash
echo "https://www.youtube.com/watch?v=dQw4w9WgXcQ" >> data/urls.txt
# Or Substack articles with audio
echo "https://snyder.substack.com/p/how-wars-are-won" >> data/urls.txt
```

**View feed:**
- Feed XML: http://localhost:8083/feed.xml
- Audio files: http://localhost:8083/audio

The service automatically downloads videos as audio and updates the feed in real-time.

## Production Setup

1. **Initialize configuration:**
   ```bash
   podservice init
   ```

2. **Edit the config file:**
   - macOS: `~/Library/Application Support/podservice/config.yaml`
   - Linux: `~/.config/podservice/config.yaml`

3. **Start the service:**
   ```bash
   podservice serve
   ```

4. **Subscribe in Apple Podcasts:**
   - File → Add a Show by URL
   - Enter: `http://your-server:8083/feed.xml`

## Development Commands

```bash
# Quick commands (using Makefile)
make serve         # Start dev service
make clean         # Clean temp files
make info          # Show config
make test          # Run tests
make help          # Show all commands

# Or use CLI directly
podservice serve  # Start service
podservice init   # Initialize config
podservice info   # Show info

# With tmuxinator (full dev environment)
tmuxinator start podservice
```

## Configuration

Configuration file is located at:
- macOS: `~/Library/Application Support/podservice/config.yaml`
- Linux: `~/.config/podservice/config.yaml`

Example configuration:

```yaml
server:
  port: 8083
  host: "0.0.0.0"

podcast:
  title: "My Podcast"
  description: "Audio podcast episodes"
  author: "Pod Service"

storage:
  data_dir: "/path/to/storage"
  audio_dir: "/path/to/storage/audio"

watch:
  file: "/path/to/urls.txt"
```

## Deployment

For production deployment on NixOS or nix-darwin, see [DEPLOYMENT.md](DEPLOYMENT.md).

## How It Works

1. Service watches a text file for URLs
2. When URLs are detected, yt-dlp downloads the audio as MP3
3. Episode metadata is extracted and saved (gracefully handles sources with limited metadata)
4. The podcast feed XML is updated automatically
5. Audio files are served via HTTP
6. Successfully processed URLs are removed from the watch file

## Project Structure

```
podservice/
├── __init__.py       # Package initialization
├── __main__.py       # Module entry point
├── cli.py            # CLI interface
├── config.py         # Configuration management
├── daemon.py         # Main service daemon
├── downloader.py     # Media downloader (yt-dlp)
├── feed.py           # Podcast RSS feed generator
├── server.py         # HTTP server (Flask)
└── watcher.py        # File watching (watchdog)
```

## Similar Projects

This service is inspired by:

- [podsync](https://github.com/mxpv/podsync) - Full-featured YouTube/Vimeo to podcast converter (Go)

Podservice is simpler and more focused: URLs to podcast episodes via yt-dlp.

## Requirements

- Python 3.8+
- ffmpeg (for audio conversion)
- yt-dlp

## License

MIT
