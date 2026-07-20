# Deployment Guide

Podservice requires RabbitMQ, ffmpeg, persistent storage, and access to the
submitted media sources. Kafka is required when lifecycle events are enabled.
Keep both brokers on the private network unless authentication and TLS are
configured for remote access.

## Flake integration

Add podservice as an input:

```nix
{
  inputs.podservice = {
    url = "github:ivankovnatsky/podservice";
    inputs.nixpkgs.follows = "nixpkgs";
  };
}
```

Import the module into a NixOS configuration:

```nix
{ inputs, ... }:

{
  imports = [ inputs.podservice.nixosModules.default ];

  services.podservice = {
    enable = true;
    port = 8083;
    host = "0.0.0.0";
    baseUrl = "http://192.168.50.4:8083";
    dataDir = "/var/lib/podservice";
    audioDir = "/var/lib/podservice/audio";

    podcast = {
      title = "My Podcast";
      description = "Audio podcast episodes";
      author = "PodService";
      language = "en-us";
      category = "Technology";
    };

    rabbitmq = {
      host = "127.0.0.1";
      port = 5672;
      managementPort = 15672;
      username = "guest";
      virtualHost = "/";
      retryDelays = [ 30 300 1800 ];
    };

    kafka = {
      enable = true;
      bootstrapServers = [ "127.0.0.1:9092" ];
      topic = "podservice.lifecycle";
      consumerGroup = "podservice-dashboard";
    };
  };
}
```

The module configures podservice itself. RabbitMQ and, when enabled, Kafka must
also run on the host or be supplied by another machine. Order podservice after
their systemd units when they are managed on the same host.

For nix-darwin, import `darwinModules.default` and use macOS storage paths:

```nix
{ inputs, ... }:

{
  imports = [ inputs.podservice.darwinModules.default ];

  services.podservice = {
    enable = true;
    dataDir = "/Volumes/Storage/Data/.podservice";
    audioDir = "/Volumes/Storage/Data/.podservice/audio";
    rabbitmq.host = "127.0.0.1";
  };
}
```

The launchd daemon creates its storage directories before starting and writes
stdout and stderr logs below `dataDir`. A complete configuration is available in
[`nix/example-darwin-config.nix`](nix/example-darwin-config.nix).

## Usage

Submit a URL through the web interface or API:

```bash
curl -X POST http://192.168.50.4:8083/api/urls \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/episode"}'
```

The service returns `202 Accepted` after the message is confirmed by RabbitMQ.
The consumer downloads jobs one at a time, retries failures using delayed
queues, and moves exhausted jobs to `podservice.downloads.dead`.

Endpoints:

- Feed: `http://192.168.50.4:8083/feed.xml`
- Web interface: `http://192.168.50.4:8083/`
- Audio: `http://192.168.50.4:8083/audio`
- API documentation: `http://192.168.50.4:8083/apidocs/`
- Messaging status: `http://192.168.50.4:8083/status`

## Service management

```bash
systemctl status podservice rabbitmq apache-kafka
journalctl -u podservice -u rabbitmq -u apache-kafka -f
```

On nix-darwin:

```bash
sudo launchctl print system/org.nixos.podservice
tail -f /Volumes/Storage/Data/.podservice/podservice.out.log
```

## Persistent data

```text
/var/lib/podservice/
├── audio/       # Downloaded audio
├── db/          # Local database files
│   └── podservice.sqlite3
├── metadata/    # Episode metadata
└── thumbnails/  # Episode artwork
```

RabbitMQ stores queued jobs separately in its own data directory, while Kafka
stores the lifecycle event log in its broker data directory. Include all three
stores in the host's backup policy.

On Darwin, the equivalent podservice layout is rooted at
`/Volumes/Storage/Data/.podservice`.

## Troubleshooting

- A `503` response from `/api/urls` means RabbitMQ did not confirm the job.
- Use `/status` to check RabbitMQ consumers, queued jobs, Kafka partitions, and
  projection lag.
- Check the podservice logs for consumer reconnects and download errors.
- Inspect `podservice.downloads.dead` for jobs that exhausted all retries.
- Verify the configured base URL is reachable from the podcast player.
- Use a reverse proxy for HTTPS and add authentication before exposing the
  service outside a trusted network.
