"""CLI entry point for podservice."""

import logging
import sys
from pathlib import Path

try:
    import click
except ImportError:
    # Fallback if click is not available
    click = None

from .config import ServiceConfig, get_default_config_path, load_config, save_config
from .daemon import run_service

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False):
    """Setup basic logging."""
    log_level = logging.DEBUG if verbose else logging.INFO
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logging.basicConfig(level=log_level, format=log_format)


def main_simple():
    """Simple main function without click (fallback)."""
    import argparse

    parser = argparse.ArgumentParser(description="Pod Service - Podcast Feed Service")
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        help="Path to config file",
        default=None,
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Serve command
    subparsers.add_parser("serve", help="Run the service")

    # Init command
    subparsers.add_parser("init", help="Initialize config file")

    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.command == "serve":
        run_service(config_path=args.config, foreground=True)
    elif args.command == "init":
        init_config()
    else:
        parser.print_help()


def init_config():
    """Initialize a default config file."""
    config_path = get_default_config_path()

    if config_path.exists():
        print(f"Config file already exists: {config_path}")
        response = input("Overwrite? (y/N): ")
        if response.lower() != "y":
            print("Aborted.")
            return

    # Create default config
    config = ServiceConfig()
    save_config(config)

    print(f"Created config file: {config_path}")
    print("\nExample configuration:")
    print(f"  Server: http://localhost:{config.server.port}")
    print(f"  Watch file: {config.watch.file}")
    print(f"  Audio directory: {config.storage.audio_dir}")
    print("\nEdit the config file to customize settings.")
    print(f"\nTo start the service, run: podservice serve")


# Click-based CLI (preferred)
if click:

    @click.group()
    @click.option("--verbose", "-v", is_flag=True, help="Verbose output")
    @click.pass_context
    def main(ctx, verbose):
        """Pod Service - Podcast Feed Service."""
        ctx.ensure_object(dict)
        ctx.obj["verbose"] = verbose
        setup_logging(verbose)

    @main.command()
    @click.option("--config", "-c", type=click.Path(), help="Path to config file")
    @click.pass_context
    def serve(ctx, config):
        """Run the service."""
        run_service(config_path=config, foreground=True)

    @main.command()
    @click.pass_context
    def init(ctx):
        """Initialize config file."""
        init_config()

    @main.command()
    @click.option("--config", "-c", type=click.Path(), help="Path to config file")
    @click.pass_context
    def info(ctx, config):
        """Show service information."""
        config_path = config

        try:
            config = load_config(config_path)
            effective_config_path = config_path or get_default_config_path()

            click.echo("Pod Service Information")
            click.echo("=" * 50)
            click.echo(f"Config file: {effective_config_path}")
            click.echo(f"Config exists: {Path(effective_config_path).exists()}")
            click.echo(f"\nServer Configuration:")
            click.echo(f"  URL: {config.server.base_url}")
            click.echo(f"  Port: {config.server.port}")
            click.echo(f"  Host: {config.server.host}")
            click.echo(f"\nPodcast Configuration:")
            click.echo(f"  Title: {config.podcast.title}")
            click.echo(f"  Author: {config.podcast.author}")
            click.echo(f"  Description: {config.podcast.description}")
            click.echo(f"\nStorage:")
            click.echo(f"  Data directory: {config.storage.data_dir}")
            click.echo(f"  Audio directory: {config.storage.audio_dir}")
            click.echo(f"\nFile Watching:")
            click.echo(f"  Enabled: {config.watch.enabled}")
            click.echo(f"  File: {config.watch.file}")
            click.echo(f"  File exists: {Path(config.watch.file).exists()}")

        except Exception as e:
            click.echo(f"Error loading config: {e}", err=True)
            sys.exit(1)

else:
    # Use simple main if click is not available
    main = main_simple


if __name__ == "__main__":
    main()
