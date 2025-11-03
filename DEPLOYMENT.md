# Deployment Guide

This guide covers deploying podservice on your infrastructure.

## Network Configuration

Based on your setup:
- **Bee (192.168.50.3)**: Linux NixOS server
- **Mini (192.168.50.4)**: macOS with nix-darwin

Podservice uses **port 8083** (sequential to podsync's 8082).

## Deployment Options

### Option 1: Using Nix Flake (Recommended)

1. **Add podservice to your flake inputs:**

```nix
# In your nixos-config/flake.nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    # ... other inputs

    podservice = {
      url = "github:ivankovnatsky/podservice";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, podservice, ... }: {
    # For NixOS (bee)
    nixosConfigurations.bee = nixpkgs.lib.nixosSystem {
      # ...
      modules = [
        podservice.nixosModules.default
        ./machines/bee/configuration.nix
      ];
    };

    # For nix-darwin (mini)
    darwinConfigurations.mini = darwin.lib.darwinSystem {
      # ...
      modules = [
        podservice.darwinModules.default
        ./machines/mini/configuration.nix
      ];
    };
  };
}
```

2. **Configure the service:**

Create `machines/mini/darwin/server/podservice/default.nix`:

```nix
{ config, pkgs, ... }:

{
  services.podservice = {
    enable = true;
    port = 8083;
    host = "0.0.0.0";
    baseUrl = "http://192.168.50.4:8083";

    dataDir = "/Volumes/Storage/Data/podservice";
    audioDir = "/Volumes/Storage/Data/podservice/audio";

    podcast = {
      title = "My YouTube Podcast";
      description = "YouTube videos as podcast episodes";
      author = "Ivan Kovnatsky";
      language = "en-us";
      category = "Technology";
    };

    watch = {
      enabled = true;
      file = "/Volumes/Storage/Data/podservice/urls.txt";
    };

    logLevel = "INFO";
  };
}
```

3. **Import in your machine configuration:**

```nix
# machines/mini/darwin/configuration.nix
{
  imports = [
    ./server/podservice
    # ... other imports
  ];
}
```

### Option 2: Local Development Path

If not using a flake input, you can import directly:

```nix
{
  imports = [
    /Users/ivan/Sources/github.com/ivankovnatsky/podservice/nix/service.nix
  ];
}
```

## Usage

### Adding YouTube Videos

Simply add YouTube URLs to the watched file, one per line:

```bash
# macOS
echo "https://www.youtube.com/watch?v=VIDEO_ID" >> /Volumes/Storage/Data/Media/Podservice/urls.txt

# Linux
echo "https://www.youtube.com/watch?v=VIDEO_ID" >> /var/lib/podservice/urls.txt
```

The service will:
1. Detect the file change
2. Download the video as MP3
3. Add it to the podcast feed
4. Remove the URL from the file after successful processing

### Accessing the Podcast

- **Feed URL**: `http://192.168.50.4:8083/feed.xml`
- **Web Interface**: `http://192.168.50.4:8083/`
- **Audio Files**: `http://192.168.50.4:8083/audio`

### Adding to Apple Podcasts

1. Open Apple Podcasts
2. File → Add a Show by URL
3. Enter: `http://192.168.50.4:8083/feed.xml`

## Service Management

### NixOS (bee)

```bash
# Check status
sudo systemctl status podservice

# View logs
sudo journalctl -u podservice -f

# Restart service
sudo systemctl restart podservice
```

### nix-darwin (mini)

```bash
# Check status
sudo launchctl list | grep podservice

# View logs
tail -f /Volumes/Storage/Data/podservice/podservice.out.log

# Restart service
sudo launchctl kickstart -k system/podservice
```

## Directory Structure

```
/Volumes/Storage/Data/Media/Podservice/  # or /var/lib/podservice/
├── audio/                               # Downloaded MP3 files
│   └── *.mp3
├── metadata/                            # Episode metadata JSON files
│   └── *.json
├── urls.txt                             # URL queue (watched file)
└── podservice.*.log                     # Service logs (macOS)
```

## Troubleshooting

### Check if service is running

```bash
# Test the endpoint
curl http://192.168.50.4:8083/feed.xml

# Check if port is listening
netstat -an | grep 8083
```

### Manually process a URL

```bash
# Enter the nix shell
nix develop /Users/ivan/Sources/github.com/ivankovnatsky/podservice

# Run directly
python -m pod_service info
```

### View detailed logs

Set `logLevel = "DEBUG"` in the service configuration and rebuild.

## Security Considerations

- The service runs as a dedicated user/group (`podservice`)
- Protected system directories (on NixOS)
- Consider adding authentication if exposed publicly
- Currently HTTP only - use reverse proxy (nginx/caddy) for HTTPS

## Next Steps

1. Configure reverse proxy with HTTPS
2. Add custom podcast artwork
3. Set up automated backups of audio files
4. Monitor disk usage in audio directory
