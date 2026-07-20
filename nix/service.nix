{
  config,
  lib,
  options,
  pkgs,
  ...
}:

with lib;

let
  cfg = config.services.podservice;

  # Build the config file
  configFile = pkgs.writeText "podservice-config.yaml" (
    builtins.toJSON {
      server = {
        port = cfg.port;
        host = cfg.host;
        base_url = cfg.baseUrl;
      };
      podcast = {
        title = cfg.podcast.title;
        description = cfg.podcast.description;
        author = cfg.podcast.author;
        language = cfg.podcast.language;
        category = cfg.podcast.category;
        image_url = cfg.podcast.imageUrl;
      };
      storage = {
        data_dir = cfg.dataDir;
        audio_dir = cfg.audioDir;
      };
      rabbitmq = {
        host = cfg.rabbitmq.host;
        port = cfg.rabbitmq.port;
        username = cfg.rabbitmq.username;
        password_file = cfg.rabbitmq.passwordFile;
        virtual_host = cfg.rabbitmq.virtualHost;
        exchange = cfg.rabbitmq.exchange;
        queue = cfg.rabbitmq.queue;
        routing_key = cfg.rabbitmq.routingKey;
        retry_delays = cfg.rabbitmq.retryDelays;
        reconnect_delay = cfg.rabbitmq.reconnectDelay;
      };
      log_level = cfg.logLevel;
    }
  );

  darwinStart = pkgs.writeShellScript "podservice-start" ''
    ${pkgs.coreutils}/bin/mkdir -p ${
      lib.escapeShellArgs [
        cfg.dataDir
        cfg.audioDir
        "${cfg.dataDir}/metadata"
        "${cfg.dataDir}/thumbnails"
      ]
    }
    export PATH=${lib.makeBinPath [ pkgs.ffmpeg ]}
    cd ${lib.escapeShellArg cfg.dataDir}
    exec ${cfg.package}/bin/podservice serve --config ${configFile} \
      >> ${lib.escapeShellArg "${cfg.dataDir}/podservice.out.log"} \
      2>> ${lib.escapeShellArg "${cfg.dataDir}/podservice.error.log"}
  '';

in
{
  options.services.podservice = {
    enable = mkEnableOption "Pod Service - Podcast Feed Service";

    package = mkOption {
      type = types.package;
      description = "Podservice package to run";
    };

    port = mkOption {
      type = types.int;
      default = 8083;
      description = "Port to listen on";
    };

    host = mkOption {
      type = types.str;
      default = "0.0.0.0";
      description = "Host to bind to";
    };

    baseUrl = mkOption {
      type = types.str;
      default = "http://localhost:8083";
      description = "Base URL for the service (used in podcast feed)";
    };

    dataDir = mkOption {
      type = types.str;
      default = "/var/lib/podservice";
      description = "Base data directory";
    };

    audioDir = mkOption {
      type = types.str;
      default = "/var/lib/podservice/audio";
      description = "Audio files directory";
    };

    podcast = {
      title = mkOption {
        type = types.str;
        default = "My Podcast";
        description = "Podcast title";
      };

      description = mkOption {
        type = types.str;
        default = "Audio podcast episodes";
        description = "Podcast description";
      };

      author = mkOption {
        type = types.str;
        default = "PodService";
        description = "Podcast author";
      };

      language = mkOption {
        type = types.str;
        default = "en-us";
        description = "Podcast language";
      };

      category = mkOption {
        type = types.str;
        default = "Technology";
        description = "Podcast category";
      };

      imageUrl = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "URL to podcast cover image";
      };
    };

    rabbitmq = {
      host = mkOption {
        type = types.str;
        default = "127.0.0.1";
        description = "RabbitMQ host";
      };

      port = mkOption {
        type = types.port;
        default = 5672;
        description = "RabbitMQ AMQP port";
      };

      username = mkOption {
        type = types.str;
        default = "guest";
        description = "RabbitMQ username";
      };

      passwordFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Runtime path to the RabbitMQ password file";
      };

      virtualHost = mkOption {
        type = types.str;
        default = "/";
        description = "RabbitMQ virtual host";
      };

      exchange = mkOption {
        type = types.str;
        default = "podservice.commands";
        description = "Download command exchange";
      };

      queue = mkOption {
        type = types.str;
        default = "podservice.downloads";
        description = "Download job queue";
      };

      routingKey = mkOption {
        type = types.str;
        default = "download.requested";
        description = "Download command routing key";
      };

      retryDelays = mkOption {
        type = types.listOf types.ints.positive;
        default = [
          30
          300
          1800
        ];
        description = "Retry delays in seconds";
      };

      reconnectDelay = mkOption {
        type = types.ints.positive;
        default = 5;
        description = "Consumer reconnect delay in seconds";
      };
    };

    logLevel = mkOption {
      type = types.str;
      default = "INFO";
      description = "Logging level (DEBUG, INFO, WARNING, ERROR)";
    };

    user = mkOption {
      type = types.str;
      default = "podservice";
      description = "User to run the service as";
    };

    group = mkOption {
      type = types.str;
      default = "podservice";
      description = "Group to run the service as";
    };
  };

  config = mkIf cfg.enable (mkMerge [
    {
      assertions = [
        {
          assertion = cfg.rabbitmq.username == "guest" || cfg.rabbitmq.passwordFile != null;
          message = "services.podservice.rabbitmq.passwordFile is required for non-guest users";
        }
      ];
    }

    (optionalAttrs (options ? systemd) {
      users.users.${cfg.user} = {
        isSystemUser = true;
        group = cfg.group;
        home = cfg.dataDir;
        createHome = true;
        description = "Pod Service user";
      };

      users.groups.${cfg.group} = { };

      systemd.tmpfiles.rules = [
        "d ${cfg.dataDir} 0750 ${cfg.user} ${cfg.group} -"
        "d ${cfg.audioDir} 0750 ${cfg.user} ${cfg.group} -"
        "d ${cfg.dataDir}/metadata 0750 ${cfg.user} ${cfg.group} -"
      ];

      systemd.services.podservice = {
        description = "Pod Service - Podcast Feed Service";
        wantedBy = [ "multi-user.target" ];
        after = [ "network.target" ];
        path = [ pkgs.ffmpeg ];

        serviceConfig = {
          Type = "simple";
          User = cfg.user;
          Group = cfg.group;
          WorkingDirectory = cfg.dataDir;
          ExecStart = "${cfg.package}/bin/podservice serve --config ${configFile}";
          Restart = "on-failure";
          RestartSec = "10s";
          TimeoutStopSec = "infinity";

          NoNewPrivileges = true;
          PrivateTmp = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          ReadWritePaths = [
            cfg.dataDir
            cfg.audioDir
          ];
        };
      };
    })

    (optionalAttrs (options ? launchd) {
      launchd.daemons.podservice.serviceConfig = {
        ProgramArguments = [ (toString darwinStart) ];
        KeepAlive = true;
        RunAtLoad = true;
      };
    })
  ]);
}
