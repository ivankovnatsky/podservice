{ inputs, ... }:

{
  imports = [ inputs.podservice.darwinModules.default ];

  services.podservice = {
    enable = true;
    port = 8083;
    host = "0.0.0.0";
    baseUrl = "http://localhost:8083";

    dataDir = "/Volumes/Storage/Data/.podservice";
    audioDir = "/Volumes/Storage/Data/.podservice/audio";

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
      retryDelays = [
        30
        300
        1800
      ];
    };

    kafka = {
      enable = true;
      bootstrapServers = [ "127.0.0.1:9092" ];
      topic = "podservice.lifecycle";
      consumerGroup = "podservice-dashboard";
    };

    logLevel = "INFO";
  };
}
