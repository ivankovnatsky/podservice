# Example configuration for podservice
# Add this to your NixOS or nix-darwin configuration

{ config, pkgs, ... }:

{
  # Import the service module
  imports = [
    /path/to/podservice/nix/service.nix
  ];

  # Or if using flakes:
  # imports = [
  #   inputs.podservice.nixosModules.default
  # ];

  services.podservice = {
    enable = true;

    # Server configuration
    port = 8083;
    host = "0.0.0.0";
    baseUrl = "http://192.168.50.4:8083"; # Update to your server's IP/domain

    # Storage paths
    dataDir = "/Volumes/Storage/Data/Media/Podservice"; # macOS
    # dataDir = "/var/lib/podservice"; # Linux

    audioDir = "/Volumes/Storage/Data/Media/Podservice/audio"; # macOS
    # audioDir = "/var/lib/podservice/audio"; # Linux

    # Podcast metadata
    podcast = {
      title = "My YouTube Podcast";
      description = "Converted YouTube videos as podcast episodes";
      author = "Your Name";
      language = "en-us";
      category = "Technology";
      # imageUrl = "https://example.com/cover.jpg"; # Optional
    };

    # File watching
    watch = {
      enabled = true;
      file = "/Volumes/Storage/Data/Media/Podservice/urls.txt"; # macOS
      # file = "/var/lib/podservice/urls.txt"; # Linux
    };

    # Logging
    logLevel = "INFO";
  };

  # Optional: Open firewall port (NixOS only)
  # networking.firewall.allowedTCPPorts = [ 8083 ];
}
