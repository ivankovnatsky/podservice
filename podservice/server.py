"""HTTP server for serving podcast feed."""

import logging
import os
import threading
from pathlib import Path

from flask import Flask, Response, redirect, render_template_string, request, send_from_directory

from .config import ServiceConfig
from .feed import PodcastFeed

logger = logging.getLogger(__name__)


class PodcastServer:
    """HTTP server for podcast feed and audio files."""

    def __init__(self, config: ServiceConfig, feed: PodcastFeed):
        self.config = config
        self.feed = feed
        self.app = Flask(__name__)
        self._setup_routes()
        self.server_thread = None

    def _setup_routes(self):
        """Setup Flask routes."""

        @self.app.route("/", methods=["GET"])
        def index():
            """Root endpoint."""
            success = request.args.get("success")
            error = request.args.get("error")

            message = ""
            if success:
                message = f'<div style="padding: 10px; background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 4px; margin-bottom: 20px;">✓ URL added successfully! Processing will start automatically.</div>'
            elif error:
                message = f'<div style="padding: 10px; background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 4px; margin-bottom: 20px;">✗ Error: {error}</div>'

            return f"""
            <html>
            <head>
                <title>Podservice</title>
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <style>
                    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; }}
                    h1 {{ color: #333; }}
                    .links {{ margin: 30px 0; }}
                    .links ul {{ list-style: none; padding: 0; }}
                    .links li {{ margin: 15px 0; }}
                    .links a {{ color: #007bff; text-decoration: none; font-size: 18px; display: inline-block; padding: 5px 0; }}
                    .links a:hover {{ text-decoration: underline; }}
                    .form-group {{ margin: 40px 0 20px 0; padding-top: 30px; border-top: 1px solid #eee; }}
                    .input-wrapper {{ position: relative; display: flex; gap: 10px; }}
                    input[type="text"] {{ flex: 1; padding: 12px; font-size: 16px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }}
                    button {{ background-color: #007bff; color: white; padding: 12px 24px; font-size: 16px; border: none; border-radius: 4px; cursor: pointer; white-space: nowrap; }}
                    button:hover {{ background-color: #0056b3; }}

                    /* Mobile styles */
                    @media (max-width: 768px) {{
                        body {{ margin: 20px auto; padding: 15px; }}
                        h1 {{ font-size: 24px; }}
                        h2 {{ font-size: 20px; }}
                        .input-wrapper {{ flex-direction: column; gap: 10px; }}
                        input[type="text"] {{ width: 100%; padding: 14px; font-size: 16px; }}
                        button {{ width: 100%; padding: 14px; font-size: 16px; }}
                        .links a {{ font-size: 16px; padding: 8px 0; }}
                        .links li {{ margin: 12px 0; }}
                    }}
                </style>
            </head>
            <body>
                <h1>Podservice</h1>
                <p>YouTube to Podcast Feed Service</p>

                {message}

                <div class="links">
                    <h2>Links</h2>
                    <ul>
                        <li><a href="/feed.xml">📡 Podcast Feed</a></li>
                        <li><a href="/audio">🎵 Audio Files</a></li>
                    </ul>
                </div>

                <div class="form-group">
                    <h2>Add YouTube Video</h2>
                    <form method="POST" action="/add-url">
                        <div class="input-wrapper">
                            <input type="text" name="url" placeholder="Paste YouTube URL here..." required>
                            <button type="submit">Add to Podcast</button>
                        </div>
                    </form>
                </div>
            </body>
            </html>
            """

        @self.app.route("/add-url", methods=["POST"])
        def add_url():
            """Add a YouTube URL to the watch file."""
            try:
                url = request.form.get("url", "").strip()

                if not url:
                    return redirect("/?error=URL is required")

                # Basic YouTube URL validation
                if "youtube.com" not in url and "youtu.be" not in url:
                    return redirect("/?error=Invalid YouTube URL")

                # Append URL to watch file
                watch_file = self.config.watch.file
                with open(watch_file, "a") as f:
                    f.write(f"{url}\n")

                logger.info(f"URL added via web interface: {url}")
                return redirect("/?success=1")

            except Exception as e:
                logger.error(f"Error adding URL: {e}", exc_info=True)
                return redirect(f"/?error={str(e)}")

        @self.app.route("/feed.xml")
        def feed_xml():
            """Serve podcast RSS feed."""
            try:
                xml_content = self.feed.generate_xml()
                return Response(xml_content, mimetype="application/xml")
            except Exception as e:
                logger.error(f"Error generating feed: {e}", exc_info=True)
                return Response(
                    "Error generating feed", status=500, mimetype="text/plain"
                )

        @self.app.route("/audio/<path:filename>")
        def audio_file(filename):
            """Serve audio files."""
            try:
                audio_dir = self.config.storage.audio_dir
                if not os.path.exists(audio_dir):
                    return Response("Audio directory not found", status=404)

                return send_from_directory(audio_dir, filename)
            except Exception as e:
                logger.error(f"Error serving audio file {filename}: {e}", exc_info=True)
                return Response("File not found", status=404)

        @self.app.route("/audio")
        def audio_list():
            """List available audio files."""
            try:
                audio_dir = Path(self.config.storage.audio_dir)
                if not audio_dir.exists():
                    return "<html><body><h1>Audio Files</h1><p>No audio files yet.</p></body></html>"

                files = []
                for file in sorted(audio_dir.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
                    if file.is_file() and file.suffix in [".mp3", ".m4a", ".wav"]:
                        size_mb = file.stat().st_size / (1024 * 1024)
                        files.append(
                            f'<li><a href="/audio/{file.name}">{file.name}</a> ({size_mb:.1f} MB)</li>'
                        )

                if not files:
                    return "<html><body><h1>Audio Files</h1><p>No audio files yet.</p></body></html>"

                files_html = "\n".join(files)
                return f"""
                <html>
                <head><title>Audio Files</title></head>
                <body>
                    <h1>Audio Files</h1>
                    <p><a href="/">&larr; Back</a></p>
                    <ul>
                    {files_html}
                    </ul>
                </body>
                </html>
                """
            except Exception as e:
                logger.error(f"Error listing audio files: {e}", exc_info=True)
                return Response("Error listing files", status=500)

    def start(self):
        """Start the server in a separate thread."""
        logger.info(
            f"Starting HTTP server on {self.config.server.host}:{self.config.server.port}"
        )

        # Disable Flask's default logging
        log = logging.getLogger("werkzeug")
        log.setLevel(logging.WARNING)

        def run_server():
            self.app.run(
                host=self.config.server.host,
                port=self.config.server.port,
                debug=False,
                use_reloader=False,
                threaded=True,
            )

        self.server_thread = threading.Thread(target=run_server, daemon=True)
        self.server_thread.start()

        logger.info(
            f"Server started: {self.config.server.base_url}/feed.xml"
        )

    def stop(self):
        """Stop the server."""
        logger.info("Stopping HTTP server...")
        # Flask doesn't have a clean way to stop from another thread
        # The daemon thread will be terminated when the main program exits
