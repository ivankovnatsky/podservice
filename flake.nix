{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/ee0632b43f6f098575479910fca05c24b0ada9dd";
    flake-utils.url = "github:numtide/flake-utils/11707dc2f618dd54ca8739b309ec4fc024de578b";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs {
          inherit system;
          overlays = [ ];
        };

        # Common Python packages
        # NOTE: Keep in sync with pyproject.toml [project.dependencies]
        pythonPackages =
          ps: with ps; [
            # Core Dependencies
            flask
            flasgger
            pika
            kafka-python-ng
            yt-dlp
            pyyaml
            requests
            click
            pillow
            # Development & Testing
            pytest
          ];

        # Python environment with all dependencies
        pythonEnv = pkgs.python312.withPackages pythonPackages;

        # Package the service
        podservicePackage = pkgs.python312Packages.buildPythonApplication {
          pname = "podservice";
          version = "0.1.0";
          pyproject = true;

          src = ./.;

          build-system = with pkgs.python312Packages; [
            poetry-core
          ];

          dependencies = pythonPackages pkgs.python312Packages;

          meta = with pkgs.lib; {
            description = "Podcast Feed Service";
            homepage = "https://github.com/ivankovnatsky/podservice";
            license = licenses.mit;
          };
        };
      in
      {
        packages = {
          podservice = podservicePackage;
          default = podservicePackage;
        };

        devShells.default = pkgs.mkShell {
          buildInputs =
            (with pkgs; [
              pythonEnv
              ffmpeg # Required by yt-dlp for audio conversion

              # Formatting tools
              treefmt
              nixfmt-rfc-style
              prettier
              ruff
            ])
            ++ pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.mermaid-cli ];

          shellHook = ''
            echo "Pod Service development environment"
            echo "Python: $(python --version)"
            echo "FFmpeg: $(ffmpeg -version | head -n1)"
            echo ""
            echo "Available commands:"
            echo "  python -m podservice serve  - Run the service"
            echo "  python -m podservice init   - Initialize config"
            echo "  python -m podservice info   - Show service info"
            echo ""
          '';
        };
      }
    )
    // {
      nixosModules.default =
        { lib, pkgs, ... }:
        {
          imports = [ ./nix/service.nix ];
          services.podservice.package =
            lib.mkDefault
              self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        };

      darwinModules.default =
        { lib, pkgs, ... }:
        {
          imports = [ ./nix/service.nix ];
          services.podservice.package =
            lib.mkDefault
              self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        };
    };
}
