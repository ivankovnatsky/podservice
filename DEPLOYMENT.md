# Deployment Guide

Podservice requires RabbitMQ, ffmpeg, persistent storage, and access to the
submitted media sources. Keep RabbitMQ on the private network unless its
authentication and TLS are configured for remote access.

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
      username = "guest";
      virtualHost = "/";
      retryDelays = [ 30 300 1800 ];
    };
  };
}
```

The module configures podservice itself. RabbitMQ must also be enabled on the
host or supplied by another machine. When both run under systemd, order
podservice after `rabbitmq.service` in the machine configuration.

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

## Service management

```bash
systemctl status podservice rabbitmq
journalctl -u podservice -u rabbitmq -f
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
├── metadata/    # Episode metadata
└── thumbnails/  # Episode artwork
```

RabbitMQ stores queued jobs separately in its own data directory. Include both
the podservice and RabbitMQ data in the host's backup policy.

On Darwin, the equivalent podservice layout is rooted at
`/Volumes/Storage/Data/.podservice`.

## Troubleshooting

- A `503` response from `/api/urls` means RabbitMQ did not confirm the job.
- Check the podservice logs for consumer reconnects and download errors.
- Inspect `podservice.downloads.dead` for jobs that exhausted all retries.
- Verify the configured base URL is reachable from the podcast player.
- Use a reverse proxy for HTTPS and add authentication before exposing the
  service outside a trusted network.
