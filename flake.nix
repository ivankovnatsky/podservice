{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/6bd7ba77ef6015853d67a89bd59f01b2880e9050";
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
            watchdog
            yt-dlp
            pyyaml
            requests
            click
            # Development & Testing
            pytest
          ];

        # Python environment with all dependencies
        pythonEnv = pkgs.python312.withPackages pythonPackages;

      in
      {
        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            pythonEnv
            ffmpeg # Required by yt-dlp for audio conversion

            # Formatting tools
            treefmt
            nixfmt-rfc-style
            ruff
          ];

          shellHook = ''
            echo "Pod Service development environment"
            echo "Python: $(python --version)"
            echo "FFmpeg: $(ffmpeg -version | head -n1)"
            echo ""
            echo "Available commands:"
            echo "  python -m pod_service serve  - Run the service"
            echo "  python -m pod_service init   - Initialize config"
            echo "  python -m pod_service info   - Show service info"
            echo ""
          '';
        };

        # Package the service
        packages.default = pkgs.python312Packages.buildPythonApplication {
          pname = "podservice";
          version = "0.1.0";

          src = ./.;

          propagatedBuildInputs = pythonPackages pkgs.python312Packages;

          meta = with pkgs.lib; {
            description = "YouTube to Podcast Feed Service";
            homepage = "https://github.com/ivankovnatsky/podservice";
            license = licenses.mit;
          };
        };
      }
    );
}
