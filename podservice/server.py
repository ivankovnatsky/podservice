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
                    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; background-color: #fff; color: #333; }}
                    h1 {{ color: #333; }}
                    .links {{ margin: 30px 0; }}
                    .links ul {{ list-style: none; padding: 0; }}
                    .links li {{ margin: 15px 0; }}
                    .links a {{ color: #007bff; text-decoration: none; font-size: 18px; display: inline-block; padding: 5px 0; }}
                    .links a:hover {{ text-decoration: underline; }}
                    .form-group {{ margin: 40px 0 20px 0; padding-top: 30px; border-top: 1px solid #eee; }}
                    .input-wrapper {{ position: relative; display: flex; gap: 10px; }}
                    input[type="text"] {{ flex: 1; padding: 12px; font-size: 16px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; background-color: #fff; color: #333; }}
                    button {{ background-color: #007bff; color: white; padding: 12px 24px; font-size: 16px; border: none; border-radius: 4px; cursor: pointer; white-space: nowrap; }}
                    button:hover {{ background-color: #0056b3; }}

                    /* Dark mode */
                    @media (prefers-color-scheme: dark) {{
                        body {{ background-color: #1a1a1a; color: #e0e0e0; }}
                        h1, h2 {{ color: #e0e0e0; }}
                        .links a {{ color: #4a9eff; }}
                        .form-group {{ border-top-color: #333; }}
                        input[type="text"] {{ background-color: #2a2a2a; color: #e0e0e0; border-color: #444; }}
                        button {{ background-color: #0d6efd; }}
                        button:hover {{ background-color: #0b5ed7; }}
                    }}

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
                    <h2>Internal</h2>
                    <ul>
                        <li><a href="/feed.xml">📡 Podcast Feed</a></li>
                        <li><a href="/episodes">🎵 Episodes</a></li>
                    </ul>

                    <h2>External</h2>
                    <ul>
                        <li><a href="https://www.youtube.com" target="_blank" rel="noopener noreferrer">YouTube</a></li>
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

                # Check if file exists before trying to serve it
                file_path = os.path.join(audio_dir, filename)
                if not os.path.exists(file_path):
                    logger.warning(
                        f"Audio file not found (may have been deleted or cached in client): {filename}"
                    )
                    return Response(
                        "Episode no longer available",
                        status=410,  # 410 Gone = permanently removed
                        mimetype="text/plain"
                    )

                return send_from_directory(audio_dir, filename)
            except Exception as e:
                logger.error(f"Error serving audio file {filename}: {e}", exc_info=True)
                return Response("File not found", status=404)

        @self.app.route("/thumbnails/<path:filename>")
        def thumbnail_file(filename):
            """Serve thumbnail images."""
            try:
                thumbnails_dir = self.config.storage.thumbnails_dir
                if not os.path.exists(thumbnails_dir):
                    return Response("Thumbnails directory not found", status=404)

                # Check if file exists before trying to serve it
                file_path = os.path.join(thumbnails_dir, filename)
                if not os.path.exists(file_path):
                    logger.warning(f"Thumbnail not found: {filename}")
                    return Response("Thumbnail not found", status=404, mimetype="text/plain")

                return send_from_directory(thumbnails_dir, filename)
            except Exception as e:
                logger.error(f"Error serving thumbnail {filename}: {e}", exc_info=True)
                return Response("File not found", status=404)

        @self.app.route("/episodes")
        def episodes_list():
            """List available episodes."""
            try:
                audio_dir = Path(self.config.storage.audio_dir)
                if not audio_dir.exists():
                    return """
                    <html>
                    <head>
                        <title>Episodes</title>
                        <meta name="viewport" content="width=device-width, initial-scale=1.0">
                        <style>
                            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 50px auto; padding: 20px; background-color: #fff; color: #333; }
                            h1 { color: #333; }
                            @media (prefers-color-scheme: dark) {
                                body { background-color: #1a1a1a; color: #e0e0e0; }
                                h1 { color: #e0e0e0; }
                            }
                        </style>
                    </head>
                    <body><h1>Episodes</h1><p>No episodes yet.</p></body>
                    </html>
                    """

                success = request.args.get("success")
                error = request.args.get("error")

                message = ""
                if success:
                    message = '<div style="padding: 10px; background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 4px; margin: 15px 0;">✓ Episode deleted successfully</div>'
                elif error:
                    message = f'<div style="padding: 10px; background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 4px; margin: 15px 0;">✗ Error: {error}</div>'

                metadata_dir = Path(self.config.storage.data_dir) / "metadata"
                thumbnails_dir = Path(self.config.storage.thumbnails_dir)

                files = []
                for file in sorted(audio_dir.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
                    if file.is_file() and file.suffix in [".mp3", ".m4a", ".wav"]:
                        size_mb = file.stat().st_size / (1024 * 1024)

                        # Try to find thumbnail (prefer JPEG first for compatibility)
                        thumbnail_html = ""
                        for ext in ['.jpg', '.jpeg', '.webp', '.png']:
                            thumb_file = thumbnails_dir / f"{file.stem}{ext}"
                            if thumb_file.exists():
                                thumbnail_html = f'<img src="/thumbnails/{thumb_file.name}" alt="" style="width: 60px; height: 60px; object-fit: cover; border-radius: 4px;">'
                                break

                        # Fallback placeholder if no thumbnail
                        if not thumbnail_html:
                            thumbnail_html = '<div class="thumbnail-placeholder" style="width: 60px; height: 60px; background-color: #ddd; border-radius: 4px; display: flex; align-items: center; justify-content: center; color: #999; font-size: 24px;">🎵</div>'

                        files.append(
                            f'''<li style="margin: 15px 0; display: flex; align-items: center; gap: 12px;">
                                {thumbnail_html}
                                <a href="/audio/{file.name}" style="flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">{file.name}</a>
                                <span style="color: #666; white-space: nowrap;">({size_mb:.1f} MB)</span>
                                <form method="POST" action="/delete-episode" style="margin: 0;" onsubmit="return confirm('Delete this episode? This cannot be undone.');">
                                    <input type="hidden" name="filename" value="{file.name}">
                                    <button type="submit" style="background-color: #dc3545; color: white; padding: 5px 12px; border: none; border-radius: 4px; cursor: pointer; font-size: 14px;">Delete</button>
                                </form>
                            </li>'''
                        )

                if not files:
                    return f"""
                    <html>
                    <head>
                        <title>Episodes</title>
                        <meta name="viewport" content="width=device-width, initial-scale=1.0">
                        <style>
                            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 50px auto; padding: 20px; background-color: #fff; color: #333; }}
                            h1 {{ color: #333; }}
                            a {{ color: #007bff; text-decoration: none; }}
                            a:hover {{ text-decoration: underline; }}

                            @media (prefers-color-scheme: dark) {{
                                body {{ background-color: #1a1a1a; color: #e0e0e0; }}
                                h1 {{ color: #e0e0e0; }}
                                a {{ color: #4a9eff; }}
                            }}
                        </style>
                    </head>
                    <body>
                        <h1>Episodes</h1>
                        <p><a href="/">&larr; Back</a></p>
                        {message}
                        <p>No episodes yet.</p>
                    </body>
                    </html>
                    """

                files_html = "\n".join(files)
                return f"""
                <html>
                <head>
                    <title>Episodes</title>
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <style>
                        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 50px auto; padding: 20px; background-color: #fff; color: #333; }}
                        h1 {{ color: #333; }}
                        ul {{ list-style: none; padding: 0; }}
                        a {{ color: #007bff; text-decoration: none; }}
                        a:hover {{ text-decoration: underline; }}
                        button:hover {{ background-color: #c82333 !important; }}
                        img {{ border: 1px solid #e0e0e0; }}
                        .header-controls {{ display: flex; justify-content: space-between; align-items: center; margin: 15px 0; }}
                        .delete-all-btn {{ background-color: #dc3545; color: white; padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }}
                        .delete-all-btn:hover {{ background-color: #c82333; }}

                        /* Dark mode */
                        @media (prefers-color-scheme: dark) {{
                            body {{ background-color: #1a1a1a; color: #e0e0e0; }}
                            h1 {{ color: #e0e0e0; }}
                            a {{ color: #4a9eff; }}
                            li span {{ color: #999 !important; }}
                            img {{ border-color: #333; }}
                            .thumbnail-placeholder {{ background-color: #333 !important; }}
                        }}

                        @media (max-width: 768px) {{
                            body {{ margin: 20px auto; padding: 15px; }}
                            li {{ flex-wrap: wrap !important; }}
                            li img, li .thumbnail-placeholder {{ width: 50px !important; height: 50px !important; }}
                            .header-controls {{ flex-direction: column; gap: 10px; align-items: flex-start; }}
                            .delete-all-btn {{ width: 100%; }}
                        }}
                    </style>
                </head>
                <body>
                    <h1>Episodes</h1>
                    <div class="header-controls">
                        <p style="margin: 0;"><a href="/">&larr; Back</a></p>
                        <form method="POST" action="/delete-all-episodes" style="margin: 0;" onsubmit="return confirm('Delete ALL episodes? This cannot be undone!');">
                            <button type="submit" class="delete-all-btn">Delete All Episodes</button>
                        </form>
                    </div>
                    {message}
                    <ul>
                    {files_html}
                    </ul>
                </body>
                </html>
                """
            except Exception as e:
                logger.error(f"Error listing episodes: {e}", exc_info=True)
                return Response("Error listing files", status=500)

        @self.app.route("/delete-episode", methods=["POST"])
        def delete_episode():
            """Delete an episode (audio file and metadata)."""
            try:
                filename = request.form.get("filename", "").strip()

                if not filename:
                    return redirect("/episodes?error=No filename provided")

                # Security: prevent path traversal by ensuring no directory separators
                # and that the filename is just a basename (no path components)
                if "/" in filename or "\\" in filename or filename != os.path.basename(filename):
                    return redirect("/episodes?error=Invalid filename")

                audio_dir = Path(self.config.storage.audio_dir)
                metadata_dir = Path(self.config.storage.data_dir) / "metadata"
                thumbnails_dir = Path(self.config.storage.thumbnails_dir)

                # Delete audio file
                audio_file = audio_dir / filename
                if audio_file.exists():
                    audio_file.unlink()
                    logger.info(f"Deleted audio file: {filename}")

                # Delete corresponding metadata file
                metadata_file = metadata_dir / f"{audio_file.stem}.json"
                if metadata_file.exists():
                    metadata_file.unlink()
                    logger.info(f"Deleted metadata file: {metadata_file.name}")

                # Delete corresponding thumbnail (check for common extensions)
                for ext in ['.jpg', '.jpeg', '.png', '.webp']:
                    thumbnail_file = thumbnails_dir / f"{audio_file.stem}{ext}"
                    if thumbnail_file.exists():
                        thumbnail_file.unlink()
                        logger.info(f"Deleted thumbnail file: {thumbnail_file.name}")
                        break

                # Reload episodes from metadata to update the feed
                self.feed.episodes.clear()
                self.feed.load_episodes_from_metadata(str(metadata_dir))

                return redirect("/episodes?success=1")

            except Exception as e:
                logger.error(f"Error deleting episode: {e}", exc_info=True)
                return redirect(f"/episodes?error={str(e)}")

        @self.app.route("/delete-all-episodes", methods=["POST"])
        def delete_all_episodes():
            """Delete all episodes (audio files, metadata, and thumbnails)."""
            try:
                audio_dir = Path(self.config.storage.audio_dir)
                metadata_dir = Path(self.config.storage.data_dir) / "metadata"
                thumbnails_dir = Path(self.config.storage.thumbnails_dir)

                deleted_count = 0

                # Delete all audio files
                if audio_dir.exists():
                    for audio_file in audio_dir.glob("*"):
                        if audio_file.is_file() and audio_file.suffix in [".mp3", ".m4a", ".wav"]:
                            audio_file.unlink()
                            logger.info(f"Deleted audio file: {audio_file.name}")
                            deleted_count += 1

                # Delete all metadata files
                if metadata_dir.exists():
                    for metadata_file in metadata_dir.glob("*.json"):
                        if metadata_file.is_file():
                            metadata_file.unlink()
                            logger.info(f"Deleted metadata file: {metadata_file.name}")

                # Delete all thumbnail files
                if thumbnails_dir.exists():
                    for thumbnail_file in thumbnails_dir.glob("*"):
                        if thumbnail_file.is_file() and thumbnail_file.suffix in [".jpg", ".jpeg", ".png", ".webp"]:
                            thumbnail_file.unlink()
                            logger.info(f"Deleted thumbnail file: {thumbnail_file.name}")

                # Clear all episodes from the feed
                self.feed.episodes.clear()

                logger.info(f"Deleted all episodes (total: {deleted_count})")
                return redirect("/episodes?success=1")

            except Exception as e:
                logger.error(f"Error deleting all episodes: {e}", exc_info=True)
                return redirect(f"/episodes?error={str(e)}")

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
