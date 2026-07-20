# Example configuration for podservice
# Add this to your NixOS configuration

{ inputs, ... }:

{
  # Import the NixOS module. It supplies the package by default.
  imports = [
    inputs.podservice.nixosModules.default
  ];

  services.podservice = {
    enable = true;
    # Server configuration
    port = 8083;
    host = "0.0.0.0";
    baseUrl = "http://192.168.50.4:8083"; # Update to your server's IP/domain

    # Storage paths
    dataDir = "/var/lib/podservice";
    audioDir = "/var/lib/podservice/audio";

    # Podcast metadata
    podcast = {
      title = "My Podcast";
      description = "Audio podcast episodes";
      author = "PodService";
      language = "en-us";
      category = "Technology";
      # imageUrl = "https://example.com/cover.jpg"; # Optional
    };

    rabbitmq = {
      host = "127.0.0.1";
      port = 5672;
      username = "guest";
      retryDelays = [
        30
        300
        1800
      ];
    };

    # Logging
    logLevel = "INFO";
  };

  # Optional: Open firewall port (NixOS only)
  # networking.firewall.allowedTCPPorts = [ 8083 ];
}
