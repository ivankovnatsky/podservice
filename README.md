# Pod Service

Podservice turns media URLs supported by yt-dlp into audio episodes and serves
them through a podcast feed compatible with Apple Podcasts and other players.

## Features

- Flask web interface and REST API
- Durable RabbitMQ-backed download jobs with confirmed publishing
- Delayed retries and a dead-letter queue for failed downloads
- Kafka download lifecycle events with a persistent dashboard projection
- RabbitMQ and Kafka status dashboard at `/status`
- Audio extraction through yt-dlp and ffmpeg
- RSS 2.0 feed with iTunes extensions
- Direct audio upload and episode management
- Swagger API documentation at `/apidocs/`
- Nix package plus NixOS and nix-darwin service modules

## Requirements

- Python 3.9 or newer
- RabbitMQ
- Kafka when lifecycle events are enabled
- ffmpeg
- yt-dlp

## Quick start

Start RabbitMQ, then run the service from its Nix development environment:

```bash
make serve
```

Open <http://localhost:8083>, submit a media URL, and subscribe to
<http://localhost:8083/feed.xml>.

## API

Queue one URL:

```bash
curl -X POST http://localhost:8083/api/urls \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/episode"}'
```

Queue several URLs:

```bash
curl -X POST http://localhost:8083/api/urls \
  -H 'Content-Type: application/json' \
  -d '{"urls":["https://example.com/one","https://example.com/two"]}'
```

The endpoint returns `202 Accepted` after RabbitMQ confirms the persistent
messages. Each accepted URL receives a job ID.

Upload an existing audio file:

```bash
curl -X POST http://localhost:8083/api/episodes \
  -F 'audio=@episode.mp3' \
  -F 'title=My Episode' \
  -F 'description=Episode description' \
  -F 'source_url=https://example.com/article'
```

Other endpoints:

- `GET /status` — RabbitMQ queues, Kafka health and lag, and recent events
- `GET /api/status` — JSON form of the messaging status
- `GET /feed.xml` — podcast feed
- `GET /audio/<filename>` — audio files
- `GET /thumbnails/<filename>` — thumbnails
- `GET /episodes` — episode management
- `GET /apidocs/` — Swagger documentation

## Configuration

The default configuration path is `~/.config/podservice/config.yaml` on Linux
and `~/Library/Application Support/podservice/config.yaml` on macOS.

```yaml
server:
  port: 8083
  host: "0.0.0.0"
  base_url: "http://localhost:8083"

podcast:
  title: "My Podcast"
  description: "Audio podcast episodes"
  author: "PodService"

storage:
  data_dir: "./data"
  audio_dir: "./data/audio"

rabbitmq:
  host: "127.0.0.1"
  port: 5672
  management_port: 15672
  username: "guest"
  password_file: null
  virtual_host: "/"
  exchange: "podservice.commands"
  queue: "podservice.downloads"
  routing_key: "download.requested"
  retry_delays: [30, 300, 1800]
  reconnect_delay: 5

kafka:
  enabled: false
  bootstrap_servers: ["127.0.0.1:9092"]
  topic: "podservice.lifecycle"
  consumer_group: "podservice-dashboard"
  client_id: "podservice"
  topic_partitions: 1
  topic_replication_factor: 1
  reconnect_delay: 5

log_level: "INFO"
```

## Processing flow

1. The web interface or API publishes a `DownloadJob` to RabbitMQ.
2. The consumer reserves one job at a time and downloads its URL with yt-dlp.
3. Episode metadata is saved as JSON and the audio is stored under `audio/`.
4. The episode is added to the in-memory feed and the message is acknowledged.
5. Failed jobs wait 30 seconds, 5 minutes, and 30 minutes between attempts, then
   move to `podservice.downloads.dead`.
6. When Kafka is enabled, requested, started, succeeded, failed, retry, and
   dead-letter lifecycle events are published to `podservice.lifecycle`.
7. A consumer projects events into `db/podservice.sqlite3` for the status
   dashboard; Kafka remains the replayable source of the event history.

Messages are persistent and publisher confirms are required before the API
reports acceptance. Consumers use manual acknowledgements so an interrupted
download remains available for another attempt.

Episode metadata uses `source_url` and remains compatible with legacy
`youtube_url` records:

```json
{
  "title": "Episode Title",
  "description": "...",
  "audio_file": "/path/to/audio.mp3",
  "audio_url": "http://localhost:8083/audio/file.mp3",
  "pub_date": "2025-12-21T10:00:00",
  "duration": 3600,
  "file_size": 12345678,
  "source_url": "https://original-source-url",
  "image_url": "http://localhost:8083/thumbnails/thumb.jpg"
}
```

## Project structure

```text
podservice/
├── cli.py          # Click CLI
├── config.py       # YAML configuration
├── daemon.py       # Process lifecycle
├── downloader.py   # yt-dlp integration
├── episodes.py     # Download/feed coordination
├── events.py       # Kafka lifecycle events and SQLite projection
├── feed.py         # RSS feed and episode model
├── messaging.py    # RabbitMQ jobs, topology, publisher, and consumer
├── server.py       # Flask web interface and API
├── status.py       # Read-only RabbitMQ status probe
└── utils.py        # Shared utilities
```

The main classes are `PodService`, `PodcastServer`, `EpisodeService`,
`MediaDownloader`, `PodcastFeed`, `RabbitMQPublisher`, and `RabbitMQConsumer`.

## Development

```bash
make serve   # Run with config.example.yaml
make info    # Show resolved configuration
make test    # Run the test suite
make format  # Format and lint Python files
make dev     # Enter the Nix development shell
```

Direct CLI equivalents:

```bash
podservice serve --config config.example.yaml
podservice init
podservice info --config config.example.yaml
```

## Deployment

The flake exports the podservice package through `packages`, the NixOS module
through `nixosModules.default`, and the nix-darwin module through
`darwinModules.default`. See [DEPLOYMENT.md](DEPLOYMENT.md) for configuration
and service-management examples.

## Troubleshooting

- Check the `/status` dashboard and podservice, RabbitMQ, and Kafka logs when
  jobs or lifecycle events are not being processed.
- Inspect `podservice.downloads.dead` for jobs that exhausted their retries.
- Check the `audio/` and `metadata/` directories when the feed is missing an
  episode.
- Run `ffmpeg -version` and `yt-dlp <url>` from the development shell when a
  source cannot be downloaded.

## License

MIT
