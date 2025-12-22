"""HTTP server for serving podcast feed."""

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from flasgger import Swagger
from flask import Flask, Response, jsonify, redirect, render_template_string, request, send_from_directory
from werkzeug.utils import secure_filename

from .config import ServiceConfig
from .feed import Episode, PodcastFeed, save_episode_metadata
from .utils import download_image, sanitize_filename

logger = logging.getLogger(__name__)

# Swagger configuration
SWAGGER_CONFIG = {
    "headers": [],
    "specs": [
        {
            "endpoint": "apispec",
            "route": "/apispec.json",
            "rule_filter": lambda rule: True,
            "model_filter": lambda tag: True,
        }
    ],
    "static_url_path": "/flasgger_static",
    "swagger_ui": True,
    "specs_route": "/apidocs/",
}

SWAGGER_TEMPLATE = {
    "info": {
        "title": "Podservice API",
        "description": "API for managing podcast episodes",
        "version": "0.1.0",
    },
    "basePath": "/",
}


class PodcastServer:
    """HTTP server for podcast feed and audio files."""

    def __init__(self, config: ServiceConfig, feed: PodcastFeed):
        self.config = config
        self.feed = feed
        self.app = Flask(__name__)
        self.swagger = Swagger(self.app, config=SWAGGER_CONFIG, template=SWAGGER_TEMPLATE)
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
                if success == "1":
                    message = '<div style="padding: 10px; background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 4px; margin-bottom: 20px;">✓ Added successfully!</div>'
                else:
                    message = f'<div style="padding: 10px; background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 4px; margin-bottom: 20px;">✓ {success} files uploaded successfully!</div>'
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
                    input[type="text"], textarea {{ flex: 1; padding: 12px; font-size: 16px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; background-color: #fff; color: #333; width: 100%; }}
                    button {{ background-color: #007bff; color: white; padding: 12px 24px; font-size: 16px; border: none; border-radius: 4px; cursor: pointer; white-space: nowrap; }}
                    button:hover {{ background-color: #0056b3; }}

                    /* Dark mode */
                    @media (prefers-color-scheme: dark) {{
                        body {{ background-color: #1a1a1a; color: #e0e0e0; }}
                        h1, h2 {{ color: #e0e0e0; }}
                        .links a {{ color: #4a9eff; }}
                        .form-group {{ border-top-color: #333; }}
                        input[type="text"], input[type="file"], textarea {{ background-color: #2a2a2a; color: #e0e0e0; border-color: #444; }}
                        button {{ background-color: #0d6efd; }}
                        button:hover {{ background-color: #0b5ed7; }}
                        label {{ color: #e0e0e0; }}
                        #drop-zone {{ background-color: #2a2a2a !important; border-color: #444 !important; }}
                        #drop-zone span {{ color: #e0e0e0; }}
                        #drop-zone strong {{ color: #4a9eff !important; }}
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
                <p>Podcast Feed Service</p>

                {message}

                <div class="links">
                    <h2>Internal</h2>
                    <ul>
                        <li><a href="/feed.xml">📡 Podcast Feed</a></li>
                        <li><a href="/episodes">🎵 Episodes</a></li>
                        <li><a href="/apidocs/">📚 API Docs</a></li>
                    </ul>

                </div>

                <div class="form-group">
                    <h2>Add from URL</h2>
                    <form method="POST" action="/add-url">
                        <div class="input-wrapper">
                            <input type="text" name="url" placeholder="Paste URL here..." required>
                            <button type="submit">Add to Podcast</button>
                        </div>
                    </form>
                </div>

                <div class="form-group">
                    <h2>Upload Audio Files</h2>
                    <form id="upload-form" method="POST" action="/upload-audio" enctype="multipart/form-data">
                        <div style="margin-bottom: 15px;">
                            <label for="audio" style="display: block; margin-bottom: 5px; font-weight: 500;">Audio Files *</label>
                            <div id="drop-zone" style="width: 100%; padding: 30px 10px; border: 2px dashed #ddd; border-radius: 4px; box-sizing: border-box; background-color: #fafafa; text-align: center; cursor: pointer; transition: all 0.2s ease;">
                                <input type="file" name="audio" id="audio" accept="audio/*" required multiple style="display: none;">
                                <div id="drop-text">
                                    <span style="font-size: 32px; display: block; margin-bottom: 8px;">🎵</span>
                                    <span>Drag & drop audio files here or <strong style="color: #007bff;">browse</strong></span>
                                </div>
                                <div id="file-selected" style="display: none;">
                                    <span style="font-size: 32px; display: block; margin-bottom: 8px;">✓</span>
                                    <span id="file-name" style="word-break: break-all;"></span>
                                </div>
                            </div>
                        </div>
                        <div style="margin-bottom: 15px;">
                            <label for="description" style="display: block; margin-bottom: 5px; font-weight: 500;">Description (optional, shared for all files)</label>
                            <textarea name="description" id="description" placeholder="Episode description..." rows="3" style="resize: vertical;"></textarea>
                        </div>
                        <button type="submit" style="background-color: #007bff; color: white; padding: 12px 24px; font-size: 16px; border: none; border-radius: 4px; cursor: pointer; width: 100%;">Upload to Podcast</button>
                    </form>
                </div>

                <script>
                    (function() {{
                        const dropZone = document.getElementById('drop-zone');
                        const fileInput = document.getElementById('audio');
                        const dropText = document.getElementById('drop-text');
                        const fileSelected = document.getElementById('file-selected');
                        const fileName = document.getElementById('file-name');

                        const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
                        const colors = {{
                            border: isDark ? '#444' : '#ddd',
                            bg: isDark ? '#2a2a2a' : '#fafafa',
                            dragBorder: '#007bff',
                            dragBg: isDark ? '#1a3a5c' : '#e8f4ff',
                            successBorder: '#28a745',
                            successBg: isDark ? '#1a3d1a' : '#e8f5e9'
                        }};

                        // Click to browse
                        dropZone.addEventListener('click', () => fileInput.click());

                        // Drag events
                        dropZone.addEventListener('dragover', (e) => {{
                            e.preventDefault();
                            dropZone.style.borderColor = colors.dragBorder;
                            dropZone.style.backgroundColor = colors.dragBg;
                        }});

                        dropZone.addEventListener('dragleave', (e) => {{
                            e.preventDefault();
                            dropZone.style.borderColor = colors.border;
                            dropZone.style.backgroundColor = colors.bg;
                        }});

                        dropZone.addEventListener('drop', (e) => {{
                            e.preventDefault();
                            const files = e.dataTransfer.files;
                            const audioFiles = Array.from(files).filter(f => f.type.startsWith('audio/'));
                            if (audioFiles.length > 0) {{
                                fileInput.files = files;
                                showFilesSelected(audioFiles);
                            }} else {{
                                alert('Please drop audio files.');
                                resetDropZone();
                            }}
                        }});

                        // File input change
                        fileInput.addEventListener('change', () => {{
                            if (fileInput.files.length > 0) {{
                                showFilesSelected(Array.from(fileInput.files));
                            }}
                        }});

                        function showFilesSelected(files) {{
                            dropZone.style.borderColor = colors.successBorder;
                            dropZone.style.backgroundColor = colors.successBg;
                            dropText.style.display = 'none';
                            fileSelected.style.display = 'block';

                            if (files.length === 1) {{
                                fileName.textContent = files[0].name + ' (' + (files[0].size / 1024 / 1024).toFixed(1) + ' MB)';
                            }} else {{
                                const totalSize = files.reduce((sum, f) => sum + f.size, 0);
                                fileName.innerHTML = files.length + ' files selected (' + (totalSize / 1024 / 1024).toFixed(1) + ' MB total)<br><small style="color: #888;">' + files.map(f => f.name).join(', ') + '</small>';
                            }}
                        }}

                        function resetDropZone() {{
                            dropZone.style.borderColor = colors.border;
                            dropZone.style.backgroundColor = colors.bg;
                            dropText.style.display = 'block';
                            fileSelected.style.display = 'none';
                        }}
                    }})();
                </script>
            </body>
            </html>
            """

        @self.app.route("/add-url", methods=["POST"])
        def add_url():
            """Add a URL to the watch file."""
            try:
                url = request.form.get("url", "").strip()

                if not url:
                    return redirect("/?error=URL is required")

                # Basic URL validation - must be http or https
                if not url.startswith("http://") and not url.startswith("https://"):
                    return redirect("/?error=Invalid URL (must start with http:// or https://)")

                # Append URL to watch file
                watch_file = self.config.watch.file
                with open(watch_file, "a") as f:
                    f.write(f"{url}\n")

                logger.info(f"URL added via web interface: {url}")
                return redirect("/?success=1")

            except Exception as e:
                logger.error(f"Error adding URL: {e}", exc_info=True)
                return redirect(f"/?error={str(e)}")

        @self.app.route("/upload-audio", methods=["POST"])
        def upload_audio():
            """Upload audio files via web form."""
            try:
                # Get all uploaded audio files
                audio_files = request.files.getlist("audio")
                if not audio_files or all(f.filename == "" for f in audio_files):
                    return redirect("/?error=No audio files selected")

                description = request.form.get("description", "").strip()

                # Ensure directories exist
                audio_dir = Path(self.config.storage.audio_dir)
                metadata_dir = Path(self.config.storage.metadata_dir)
                audio_dir.mkdir(parents=True, exist_ok=True)
                metadata_dir.mkdir(parents=True, exist_ok=True)

                uploaded_count = 0
                for audio_file in audio_files:
                    if audio_file.filename == "":
                        continue

                    # Get file info
                    original_filename = secure_filename(audio_file.filename)
                    file_stem = Path(original_filename).stem
                    file_ext = Path(original_filename).suffix.lower()

                    # Derive title from filename: remove extension, replace -_ with spaces
                    title = file_stem.replace("-", " ").replace("_", " ")
                    if not title:
                        title = "Untitled"

                    # Sanitize filename for storage
                    safe_title = sanitize_filename(title)
                    if not safe_title:
                        safe_title = file_stem if file_stem else "untitled"

                    # Default extension if missing
                    if not file_ext:
                        file_ext = ".mp3"

                    # Determine audio file path with collision handling
                    audio_path = audio_dir / f"{safe_title}{file_ext}"
                    counter = 1
                    while audio_path.exists():
                        audio_path = audio_dir / f"{safe_title}_{counter}{file_ext}"
                        counter += 1

                    # Save the audio file
                    audio_file.save(str(audio_path))
                    logger.info(f"Uploaded audio file: {audio_path.name}")

                    # Get file size
                    file_size = audio_path.stat().st_size

                    # Generate URLs
                    audio_url = f"{self.config.server.base_url}/audio/{quote(audio_path.name)}"
                    pub_date = datetime.now()

                    # Create episode
                    episode = Episode(
                        title=title,
                        description=description,
                        audio_file=str(audio_path),
                        audio_url=audio_url,
                        pub_date=pub_date,
                        duration=0,
                        file_size=file_size,
                        source_url="",
                        image_url="",
                    )

                    # Save metadata
                    metadata_file = metadata_dir / f"{audio_path.stem}.json"
                    save_episode_metadata(episode, str(metadata_file))

                    # Add to feed
                    self.feed.add_episode(episode)

                    logger.info(f"Created episode via upload: {title}")
                    uploaded_count += 1

                return redirect(f"/?success={uploaded_count}")

            except Exception as e:
                logger.error(f"Error uploading audio: {e}", exc_info=True)
                return redirect(f"/?error={str(e)}")

        @self.app.route("/api/urls", methods=["POST"])
        def api_add_url():
            """
            Add URL(s) for processing
            ---
            tags:
              - URLs
            consumes:
              - application/json
            parameters:
              - name: body
                in: body
                required: true
                schema:
                  type: object
                  properties:
                    url:
                      type: string
                      description: Single URL to process
                      example: https://www.youtube.com/watch?v=dQw4w9WgXcQ
                    urls:
                      type: array
                      items:
                        type: string
                      description: Multiple URLs to process
                      example: ["https://www.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=xyz"]
            produces:
              - application/json
            responses:
              200:
                description: URL(s) added for processing
                schema:
                  type: object
                  properties:
                    success:
                      type: boolean
                    message:
                      type: string
                    urls:
                      type: array
                      items:
                        type: string
                    count:
                      type: integer
              400:
                description: Invalid request
                schema:
                  type: object
                  properties:
                    success:
                      type: boolean
                    error:
                      type: string
              500:
                description: Server error
            """
            try:
                data = request.get_json()

                if not data:
                    return jsonify({"success": False, "error": "Request body must be JSON"}), 400

                # Support both single "url" and multiple "urls"
                urls = []
                if "urls" in data and isinstance(data["urls"], list):
                    urls = [u.strip() for u in data["urls"] if isinstance(u, str) and u.strip()]
                elif "url" in data and data["url"]:
                    urls = [data["url"].strip()]

                if not urls:
                    return jsonify({"success": False, "error": "Missing required field: url or urls"}), 400

                # Validate all URLs
                invalid_urls = [u for u in urls if not u.startswith("http://") and not u.startswith("https://")]
                if invalid_urls:
                    return jsonify({
                        "success": False,
                        "error": f"Invalid URL(s) (must start with http:// or https://): {invalid_urls}"
                    }), 400

                # Append URLs to watch file
                watch_file = self.config.watch.file
                with open(watch_file, "a") as f:
                    for url in urls:
                        f.write(f"{url}\n")

                logger.info(f"Added {len(urls)} URL(s) via API")
                return jsonify({
                    "success": True,
                    "message": f"Added {len(urls)} URL(s) for processing",
                    "urls": urls,
                    "count": len(urls)
                }), 200

            except Exception as e:
                logger.error(f"Error adding URL via API: {e}", exc_info=True)
                return jsonify({"success": False, "error": str(e)}), 500

        @self.app.route("/feed.xml")
        def feed_xml():
            """
            Get podcast RSS feed
            ---
            tags:
              - Feed
            produces:
              - application/xml
            responses:
              200:
                description: RSS 2.0 podcast feed with iTunes extensions
              500:
                description: Error generating feed
            """
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
            """
            Get audio file
            ---
            tags:
              - Media
            parameters:
              - name: filename
                in: path
                type: string
                required: true
                description: Audio filename
            produces:
              - audio/mpeg
            responses:
              200:
                description: Audio file
              404:
                description: File not found
              410:
                description: Episode no longer available
            """
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
            """
            Get thumbnail image
            ---
            tags:
              - Media
            parameters:
              - name: filename
                in: path
                type: string
                required: true
                description: Thumbnail filename
            produces:
              - image/jpeg
              - image/png
              - image/webp
            responses:
              200:
                description: Thumbnail image
              404:
                description: Thumbnail not found
            """
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

                metadata_dir = Path(self.config.storage.metadata_dir)
                thumbnails_dir = Path(self.config.storage.thumbnails_dir)

                files = []
                for file in sorted(audio_dir.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
                    if file.is_file() and file.suffix.lower() in [".mp3", ".m4a", ".wav", ".opus", ".aac", ".ogg", ".flac", ".wma", ".aiff", ".webm"]:
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
                metadata_dir = Path(self.config.storage.metadata_dir)
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
                metadata_dir = Path(self.config.storage.metadata_dir)
                thumbnails_dir = Path(self.config.storage.thumbnails_dir)

                deleted_count = 0

                # Delete all audio files
                if audio_dir.exists():
                    for audio_file in audio_dir.glob("*"):
                        if audio_file.is_file() and audio_file.suffix.lower() in [".mp3", ".m4a", ".wav", ".opus", ".aac", ".ogg", ".flac", ".wma", ".aiff", ".webm"]:
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

        @self.app.route("/api/episodes", methods=["POST"])
        def api_create_episode():
            """
            Create a new episode from an uploaded audio file
            ---
            tags:
              - Episodes
            consumes:
              - multipart/form-data
            parameters:
              - name: audio
                in: formData
                type: file
                required: true
                description: Audio file (any format - mp3, m4a, wav, opus, aac, ogg, flac, webm, etc.)
              - name: title
                in: formData
                type: string
                required: true
                description: Episode title
              - name: description
                in: formData
                type: string
                required: false
                description: Episode description
              - name: source_url
                in: formData
                type: string
                required: false
                description: Original article URL (used as GUID for deduplication)
              - name: pub_date
                in: formData
                type: string
                required: false
                description: Publication date in ISO 8601 format (defaults to now)
              - name: image_url
                in: formData
                type: string
                required: false
                description: URL to episode artwork (will be downloaded)
            produces:
              - application/json
            responses:
              201:
                description: Episode created successfully
                schema:
                  type: object
                  properties:
                    success:
                      type: boolean
                    episode:
                      type: object
                      properties:
                        title:
                          type: string
                        audio_url:
                          type: string
                        image_url:
                          type: string
                        pub_date:
                          type: string
                        guid:
                          type: string
              400:
                description: Missing required fields
                schema:
                  type: object
                  properties:
                    success:
                      type: boolean
                    error:
                      type: string
              409:
                description: Episode with same GUID already exists
                schema:
                  type: object
                  properties:
                    success:
                      type: boolean
                    message:
                      type: string
                    episode:
                      type: object
              500:
                description: Server error
            """
            try:
                # Validate required fields
                if "audio" not in request.files:
                    return jsonify({"success": False, "error": "Missing required field: audio"}), 400

                audio_file = request.files["audio"]
                if audio_file.filename == "":
                    return jsonify({"success": False, "error": "No audio file selected"}), 400

                title = request.form.get("title", "").strip()
                if not title:
                    return jsonify({"success": False, "error": "Missing required field: title"}), 400

                # Optional fields
                description = request.form.get("description", "").strip()
                source_url = request.form.get("source_url", "").strip()
                image_url_param = request.form.get("image_url", "").strip()
                pub_date_str = request.form.get("pub_date", "").strip()

                # Parse pub_date or use current time
                if pub_date_str:
                    try:
                        pub_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
                        # Convert to naive datetime for consistency with existing code
                        if pub_date.tzinfo is not None:
                            pub_date = pub_date.replace(tzinfo=None)
                    except ValueError as e:
                        return jsonify({"success": False, "error": f"Invalid pub_date format: {e}"}), 400
                else:
                    pub_date = datetime.now()

                # Determine GUID (source_url preferred, otherwise will use audio_url)
                guid = source_url if source_url else None

                # Check for duplicate if we have a GUID
                if guid:
                    metadata_dir = Path(self.config.storage.metadata_dir)
                    if metadata_dir.exists():
                        for metadata_file in metadata_dir.glob("*.json"):
                            try:
                                with open(metadata_file, "r") as f:
                                    data = json.load(f)
                                    # Support both source_url (new) and youtube_url (legacy)
                                    existing_guid = data.get("source_url") or data.get("youtube_url") or data.get("audio_url")
                                    if existing_guid == guid:
                                        logger.info(f"Episode already exists with GUID: {guid}")
                                        return jsonify({
                                            "success": True,
                                            "message": "Episode already exists",
                                            "episode": {
                                                "title": data.get("title"),
                                                "audio_url": data.get("audio_url"),
                                                "image_url": data.get("image_url", ""),
                                                "pub_date": data.get("pub_date"),
                                                "guid": existing_guid,
                                            }
                                        }), 409
                            except Exception:
                                continue

                # Sanitize filename
                safe_title = sanitize_filename(title)
                if not safe_title:
                    safe_title = "untitled"

                # Determine audio file extension - accept any audio format
                original_filename = secure_filename(audio_file.filename)
                file_ext = Path(original_filename).suffix.lower()
                if not file_ext:
                    file_ext = ".mp3"  # Default if no extension

                # Ensure directories exist
                audio_dir = Path(self.config.storage.audio_dir)
                metadata_dir = Path(self.config.storage.metadata_dir)
                thumbnails_dir = Path(self.config.storage.thumbnails_dir)
                audio_dir.mkdir(parents=True, exist_ok=True)
                metadata_dir.mkdir(parents=True, exist_ok=True)
                thumbnails_dir.mkdir(parents=True, exist_ok=True)

                # Save audio file
                audio_path = audio_dir / f"{safe_title}{file_ext}"

                # Handle filename collisions
                counter = 1
                while audio_path.exists():
                    audio_path = audio_dir / f"{safe_title}_{counter}{file_ext}"
                    counter += 1

                audio_file.save(str(audio_path))
                logger.info(f"Saved audio file: {audio_path.name}")

                # Get file size
                file_size = audio_path.stat().st_size

                # Generate URLs
                audio_url = f"{self.config.server.base_url}/audio/{quote(audio_path.name)}"

                # Download and process image if URL provided
                episode_image_url = ""
                if image_url_param:
                    thumbnail_path = download_image(
                        url=image_url_param,
                        output_dir=thumbnails_dir,
                        base_filename=audio_path.stem,
                    )
                    if thumbnail_path:
                        episode_image_url = f"{self.config.server.base_url}/thumbnails/{quote(thumbnail_path.name)}"
                        logger.info(f"Downloaded thumbnail: {thumbnail_path.name}")

                # Use audio_url as GUID if source_url not provided
                final_guid = guid if guid else audio_url

                # Create episode
                episode = Episode(
                    title=title,
                    description=description,
                    audio_file=str(audio_path),
                    audio_url=audio_url,
                    pub_date=pub_date,
                    duration=0,  # Duration not provided via API
                    file_size=file_size,
                    source_url=source_url,
                    image_url=episode_image_url,
                )

                # Save metadata
                metadata_file = metadata_dir / f"{audio_path.stem}.json"
                save_episode_metadata(episode, str(metadata_file))

                # Add to feed
                self.feed.add_episode(episode)

                logger.info(f"Created episode via API: {title}")

                return jsonify({
                    "success": True,
                    "episode": {
                        "title": title,
                        "audio_url": audio_url,
                        "image_url": episode_image_url,
                        "pub_date": pub_date.isoformat(),
                        "guid": final_guid,
                    }
                }), 201

            except Exception as e:
                logger.error(f"Error creating episode via API: {e}", exc_info=True)
                return jsonify({"success": False, "error": str(e)}), 500

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
