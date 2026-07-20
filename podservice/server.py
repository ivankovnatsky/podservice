"""HTTP server for serving podcast feed."""

import json
import logging
import os
import secrets
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote
from uuid import uuid4

from flasgger import Swagger
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    request,
    send_from_directory,
)
from markupsafe import escape
from werkzeug.utils import secure_filename

from .config import ServiceConfig
from .events import (
    PARTIAL_UPLOAD_SUFFIX,
    DatabaseStatus,
    KafkaStatus,
    LifecycleEvent,
)
from .feed import Episode, PodcastFeed, save_episode_metadata
from .messaging import DownloadJob, MessagePublishError, PartialPublishError
from .status import RabbitMQStatus
from .utils import download_image, extract_video_id, sanitize_filename

logger = logging.getLogger(__name__)

FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#2563eb"/>
<path d="M14 35v-5a18 18 0 0 1 36 0v5" fill="none" stroke="white" stroke-width="6" stroke-linecap="round"/>
<rect x="10" y="31" width="10" height="20" rx="5" fill="white"/>
<rect x="44" y="31" width="10" height="20" rx="5" fill="white"/>
</svg>"""

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

# Submitted filenames are untrusted; bound them before they reach storage.
MAX_SUBMITTED_FILENAME = 255

AUDIO_EXTENSIONS = [
    ".mp3",
    ".m4a",
    ".wav",
    ".opus",
    ".aac",
    ".ogg",
    ".flac",
    ".wma",
    ".aiff",
    ".webm",
]

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

    def __init__(
        self,
        config: ServiceConfig,
        feed: PodcastFeed,
        submit_urls: Optional[Callable[[list[str]], list[DownloadJob]]] = None,
        rabbitmq_status: Optional[Callable[[], RabbitMQStatus]] = None,
        kafka_status: Optional[Callable[[], KafkaStatus]] = None,
        database_status: Optional[Callable[[], DatabaseStatus]] = None,
        recent_events: Optional[Callable[[int], list[LifecycleEvent]]] = None,
        emit_upload_event: Optional[Callable[..., None]] = None,
    ):
        self.config = config
        self.feed = feed
        self.submit_urls = submit_urls
        self.rabbitmq_status = rabbitmq_status
        self.kafka_status = kafka_status
        self.database_status = database_status
        self.recent_events = recent_events
        self.emit_upload_event = emit_upload_event
        self.csrf_token = secrets.token_urlsafe(32)
        self.app = Flask(__name__)
        self.swagger = Swagger(
            self.app, config=SWAGGER_CONFIG, template=SWAGGER_TEMPLATE
        )
        self._setup_routes()
        self.server_thread = None

    def _setup_routes(self):
        """Setup Flask routes."""

        @self.app.route("/favicon.ico")
        @self.app.route("/favicon.svg")
        def favicon():
            return Response(FAVICON_SVG, mimetype="image/svg+xml")

        @self.app.route("/", methods=["GET"])
        def index():
            """Root endpoint."""
            success = request.args.get("success")
            error = request.args.get("error")

            try:
                recent = self._recent_episodes()
            except Exception:
                logger.exception("Failed to collect recent episodes")
                recent = []

            if recent:
                cards = "\n".join(
                    f'''<a class="episode-card" href="{ep["url"]}" title="{
                        escape(ep["title"])
                    }">
                        {
                        f'<img src="{ep["thumbnail"]}" alt="">'
                        if ep["thumbnail"]
                        else '<div class="episode-art episode-art-placeholder">🎵</div>'
                    }
                        <span class="episode-title">{escape(ep["title"])}</span>
                    </a>'''
                    for ep in recent
                )
                episodes_row = f'<div class="episode-row">{cards}</div>'
            else:
                episodes_row = '<p class="episode-empty">No episodes yet.</p>'

            message = ""
            if success:
                if success == "1":
                    message = '<div style="padding: 10px; background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 4px; margin-bottom: 20px;">✓ Added successfully!</div>'
                else:
                    message = f'<div style="padding: 10px; background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 4px; margin-bottom: 20px;">✓ {escape(success)} files uploaded successfully!</div>'
            elif error:
                message = f'<div style="padding: 10px; background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 4px; margin-bottom: 20px;">✗ Error: {escape(error)}</div>'

            return f"""
            <html>
            <head>
                <title>Podservice</title>
                <link rel="icon" href="/favicon.svg" type="image/svg+xml">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <style>
                    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; background-color: #fff; color: #333; }}
                    h1 {{ color: #333; }}
                    .links {{ margin: 40px 0 20px 0; padding-top: 30px; border-top: 1px solid #eee; }}
                    .links ul {{ list-style: none; padding: 0; }}
                    .links li {{ margin: 15px 0; }}
                    .links a {{ color: #007bff; text-decoration: none; font-size: 18px; display: inline-block; padding: 5px 0; }}
                    .links a:hover {{ text-decoration: underline; }}
                    .section {{ margin: 40px 0 20px 0; padding-top: 30px; border-top: 1px solid #eee; }}
                    .section-header {{ display: flex; justify-content: space-between; align-items: center; gap: 15px; }}
                    .section-header h2 {{ margin: 0; }}
                    .view-all {{ background-color: #000; color: #fff; text-decoration: none; padding: 8px 16px; border-radius: 4px; font-size: 14px; white-space: nowrap; }}
                    .view-all:hover {{ background-color: #333; text-decoration: none; }}
                    .episode-row {{ display: flex; gap: 14px; overflow-x: auto; padding: 15px 0 5px 0; }}
                    .episode-card {{ flex: 0 0 auto; width: 110px; text-decoration: none; color: inherit; }}
                    .episode-card:hover .episode-title {{ text-decoration: underline; }}
                    .episode-card img, .episode-art {{ width: 110px; height: 110px; border-radius: 6px; object-fit: cover; border: 1px solid #e0e0e0; display: block; }}
                    .episode-art-placeholder {{ background-color: #ddd; display: flex; align-items: center; justify-content: center; font-size: 32px; }}
                    .episode-title {{ display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; font-size: 13px; margin-top: 6px; }}
                    .episode-empty {{ color: #666; }}
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
                        .form-group, .section, .links {{ border-top-color: #333; }}
                        input[type="text"], input[type="file"], textarea {{ background-color: #2a2a2a; color: #e0e0e0; border-color: #444; }}
                        button {{ background-color: #0d6efd; }}
                        button:hover {{ background-color: #0b5ed7; }}
                        label {{ color: #e0e0e0; }}
                        #drop-zone {{ background-color: #2a2a2a !important; border-color: #444 !important; }}
                        #drop-zone span {{ color: #e0e0e0; }}
                        #drop-zone strong {{ color: #4a9eff !important; }}
                        .view-all {{ background-color: #000; color: #fff; border: 1px solid #444; }}
                        .view-all:hover {{ background-color: #222; }}
                        .episode-card img, .episode-art {{ border-color: #333; }}
                        .episode-art-placeholder {{ background-color: #333; }}
                        .episode-empty {{ color: #999; }}
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

                <div class="section">
                    <div class="section-header">
                        <h2>Episodes</h2>
                        <a class="view-all" href="/episodes">View all</a>
                    </div>
                    {episodes_row}
                </div>

                <div class="links">
                    <h2>Internal</h2>
                    <ul>
                        <li><a href="/feed.xml">📡 Podcast Feed</a></li>
                        <li><a href="/status">📊 Data Status</a></li>
                        <li><a href="/apidocs/">📚 API Docs</a></li>
                    </ul>

                </div>

                <div class="form-group">
                    <h2>Add from URL</h2>
                    <form method="POST" action="/add-url">
                        <input type="hidden" name="csrf_token" value="{self.csrf_token}">
                        <div class="input-wrapper">
                            <input type="text" name="url" placeholder="Paste URL here..." required>
                            <button type="submit">Add to Podcast</button>
                        </div>
                    </form>
                </div>

                <div class="form-group">
                    <h2>Upload Audio Files</h2>
                    <form id="upload-form" method="POST" action="/upload-audio" enctype="multipart/form-data">
                        <input type="hidden" name="csrf_token" value="{self.csrf_token}">
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

        @self.app.route("/api/status")
        def api_status():
            rabbitmq, kafka, database, events = self._data_status()
            return jsonify(
                {
                    "rabbitmq": {
                        **asdict(rabbitmq),
                        "ready": rabbitmq.ready,
                        "unacknowledged": rabbitmq.unacknowledged,
                        "consumers": rabbitmq.consumers,
                    },
                    "kafka": asdict(kafka),
                    "database": asdict(database),
                    "events": [asdict(event) for event in events],
                }
            )

        @self.app.route("/status")
        def data_status():
            rabbitmq, kafka, database, events = self._data_status()
            rabbit_state = "up" if rabbitmq.connected else "down"
            kafka_state = "up" if kafka.connected else "down"
            database_state = "up" if database.connected else "down"
            queue_rows = "".join(
                f"""
                <tr>
                    <td>{escape(queue.role)}</td>
                    <td class="technical">{escape(queue.name)}</td>
                    <td>{queue.ready}</td>
                    <td>{queue.unacknowledged}</td>
                    <td>{queue.consumers}</td>
                    <td>{escape(queue.state)}</td>
                </tr>
                """
                for queue in rabbitmq.queues
            )
            event_rows = "".join(
                f"""
                <tr>
                    <td>{escape(event.occurred_at)}</td>
                    <td><span class="event-type">{escape(event.event_type)}</span></td>
                    <td class="technical">{escape(event.job_id)}</td>
                    <td>{event.attempt}</td>
                    <td><span class="source-type">{escape(event.source_type)}</span></td>
                    <td class="source">{escape(event.source)}</td>
                    <td>{escape(event.detail or "")}</td>
                </tr>
                """
                for event in events
            )
            if not event_rows:
                event_rows = '<tr><td colspan="7" class="empty">No lifecycle events projected yet.</td></tr>'
            return f"""
            <!doctype html>
            <html>
            <head>
                <title>Data Status · Podservice</title>
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <meta http-equiv="refresh" content="15">
                <link rel="icon" href="/favicon.svg" type="image/svg+xml">
                <style>
                    :root {{ color-scheme: light dark; }}
                    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 1180px; margin: 32px auto; padding: 0 20px 40px; background: #f6f7fb; color: #172033; }}
                    a {{ color: #2563eb; }}
                    .header {{ display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 24px; }}
                    .header-title {{ text-align: right; }}
                    .header h1 {{ margin-bottom: 4px; }}
                    .header p {{ margin: 0; color: #64748b; }}
                    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 18px; margin-bottom: 24px; }}
                    .card, .panel {{ background: white; border: 1px solid #e2e8f0; border-radius: 12px; padding: 20px; box-shadow: 0 2px 8px rgba(15, 23, 42, .05); }}
                    .card h2 {{ display: flex; align-items: center; gap: 9px; margin-top: 0; }}
                    .state {{ width: 11px; height: 11px; border-radius: 50%; display: inline-block; }}
                    .state.up {{ background: #16a34a; box-shadow: 0 0 0 4px #dcfce7; }}
                    .state.down {{ background: #dc2626; box-shadow: 0 0 0 4px #fee2e2; }}
                    .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(72px, 1fr)); gap: 12px; }}
                    .metric strong {{ display: block; font-size: 24px; }}
                    .metric span, .error {{ color: #64748b; font-size: 13px; }}
                    .panel {{ margin-bottom: 24px; overflow-x: auto; }}
                    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
                    th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #e2e8f0; vertical-align: top; }}
                    th {{ color: #64748b; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
                    .technical {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
                    .source {{ max-width: 340px; overflow-wrap: anywhere; }}
                    .source-type {{ font-size: 12px; padding: 2px 6px; border-radius: 3px; background-color: #eef1f4; color: #445; }}
                    .event-type {{ border-radius: 999px; padding: 3px 8px; background: #dbeafe; color: #1d4ed8; white-space: nowrap; }}
                    .empty {{ color: #64748b; text-align: center; padding: 24px; }}
                    @media (prefers-color-scheme: dark) {{
                        body {{ background: #111827; color: #e5e7eb; }}
                        .card, .panel {{ background: #1f2937; border-color: #374151; }}
                        th, td {{ border-color: #374151; }}
                        .header p, .metric span, .error, th, .empty {{ color: #9ca3af; }}
                        .event-type {{ background: #1e3a8a; color: #bfdbfe; }}
                    }}
                </style>
            </head>
            <body>
                <div class="header">
                    <a href="/">← Podservice</a>
                    <div class="header-title"><h1>Data Status</h1><p>Refreshes every 15 seconds</p></div>
                </div>
                <div class="cards">
                    <section class="card">
                        <h2><span class="state {rabbit_state}"></span>RabbitMQ</h2>
                        <div class="metrics">
                            <div class="metric"><strong>{rabbitmq.ready}</strong><span>Ready</span></div>
                            <div class="metric"><strong>{rabbitmq.unacknowledged}</strong><span>Unacked</span></div>
                            <div class="metric"><strong>{rabbitmq.consumers}</strong><span>Consumers</span></div>
                        </div>
                        <p class="error">{escape(rabbitmq.error or f"RabbitMQ {rabbitmq.version or ''}")}</p>
                    </section>
                    <section class="card">
                        <h2><span class="state {kafka_state}"></span>Kafka</h2>
                        <div class="metrics">
                            <div class="metric"><strong>{kafka.broker_count}</strong><span>Brokers</span></div>
                            <div class="metric"><strong>{kafka.partition_count}</strong><span>Partitions</span></div>
                            <div class="metric"><strong>{kafka.consumer_lag}</strong><span>Consumer lag</span></div>
                            <div class="metric"><strong>{kafka.outbox_pending}</strong><span>Outbox pending</span></div>
                        </div>
                        <p class="error">{escape(kafka.error or self.config.kafka.topic)}</p>
                    </section>
                    <section class="card">
                        <h2><span class="state {database_state}"></span>SQLite</h2>
                        <div class="metrics">
                            <div class="metric"><strong>{database.event_count}</strong><span>Events</span></div>
                            <div class="metric"><strong>{database.outbox_pending}</strong><span>Pending</span></div>
                            <div class="metric"><strong>{self._format_bytes(database.size_bytes)}</strong><span>Storage</span></div>
                        </div>
                        <p class="error">{escape(database.error or database.path)}</p>
                        <p class="error">Latest event: {escape(database.last_event_at or "none")}</p>
                    </section>
                </div>
                <section class="panel">
                    <h2>RabbitMQ queues</h2>
                    <table><thead><tr><th>Role</th><th>Queue</th><th>Ready</th><th>Unacked</th><th>Consumers</th><th>State</th></tr></thead>
                    <tbody>{queue_rows}</tbody></table>
                </section>
                <section class="panel">
                    <h2>Recent Kafka lifecycle events</h2>
                    <table><thead><tr><th>Time</th><th>Event</th><th>Job</th><th>Attempt</th><th>Type</th><th>Source</th><th>Detail</th></tr></thead>
                    <tbody>{event_rows}</tbody></table>
                </section>
            </body>
            </html>
            """

        @self.app.route("/add-url", methods=["POST"])
        def add_url():
            """Queue a URL for download."""
            try:
                if not self._valid_csrf_token():
                    return Response("Forbidden", status=403)
                url = request.form.get("url", "").strip()

                if not url:
                    return redirect("/?error=URL is required")

                # Basic URL validation - must be http or https
                if not url.startswith("http://") and not url.startswith("https://"):
                    return redirect(
                        "/?error=Invalid URL (must start with http:// or https://)"
                    )

                if self.submit_urls is None:
                    raise MessagePublishError("Download queue is unavailable")

                jobs = self.submit_urls([url])
                logger.info("Queued download job %s via web interface", jobs[0].job_id)
                return redirect("/?success=1")

            except MessagePublishError as e:
                logger.error("Unable to queue URL: %s", e)
                return redirect("/?error=Download queue is unavailable")
            except Exception as e:
                logger.error(f"Error adding URL: {e}", exc_info=True)
                return redirect(f"/?error={str(e)}")

        @self.app.route("/upload-audio", methods=["POST"])  # noqa: C901
        def upload_audio():
            """Upload audio files via web form."""
            try:
                if not self._valid_csrf_token():
                    return Response("Forbidden", status=403)
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

                batch_id = str(uuid4())
                uploaded_count = 0
                failed_count = 0
                for audio_file in audio_files:
                    # Werkzeug gives None, not "", when a part carries no
                    # filename; secure_filename would then raise outside the
                    # per-file guard below and abandon the rest of the batch.
                    if not audio_file.filename:
                        continue

                    # Get file info
                    # Events carry the name as submitted so a row is
                    # recognisable against the local file; only the filesystem
                    # uses the sanitized form.
                    submitted_filename = audio_file.filename[:MAX_SUBMITTED_FILENAME]
                    original_filename = secure_filename(audio_file.filename)
                    job_id = str(uuid4())
                    self._emit_upload(
                        "upload.received", job_id, submitted_filename, batch_id
                    )

                    # A failure here must not abandon the rest of the batch.
                    partial_path = None
                    stored_path = None
                    metadata_path = None
                    try:
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

                        # Stage under .partial so an interrupted write is
                        # identifiable and never mistaken for a real episode.
                        partial_path = audio_path.with_name(
                            f"{audio_path.name}{PARTIAL_UPLOAD_SUFFIX}"
                        )
                        audio_file.save(str(partial_path))
                        partial_path.replace(audio_path)
                        partial_path = None
                        stored_path = audio_path
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

                        # Metadata last: its presence marks the episode complete
                        metadata_file = metadata_dir / f"{audio_path.stem}.json"
                        save_episode_metadata(episode, str(metadata_file))
                        metadata_path = metadata_file

                        # Add to feed
                        self.feed.add_episode(episode)

                        logger.info(f"Created episode via upload: {title}")
                        uploaded_count += 1
                        self._emit_upload(
                            "upload.stored", job_id, submitted_filename, batch_id
                        )
                    except Exception as file_error:
                        failed_count += 1
                        logger.error(
                            "Failed to store uploaded file %s: %s",
                            submitted_filename,
                            file_error,
                            exc_info=True,
                        )
                        # Roll back so a failed file leaves nothing behind: a
                        # stored audio file without metadata is invisible to the
                        # feed yet still served, and would pile up on retries.
                        for leftover in (partial_path, metadata_path, stored_path):
                            if leftover is None:
                                continue
                            try:
                                leftover.unlink(missing_ok=True)
                            except OSError as cleanup_error:
                                logger.warning(
                                    "Unable to clean up %s: %s",
                                    leftover,
                                    cleanup_error,
                                )
                        self._emit_upload(
                            "upload.failed",
                            job_id,
                            submitted_filename,
                            batch_id,
                            detail=str(file_error),
                        )

                if failed_count and not uploaded_count:
                    return redirect(f"/?error={quote('No files could be uploaded')}")
                if failed_count:
                    failure_note = quote(f"{failed_count} file(s) failed")
                    return redirect(f"/?success={uploaded_count}&error={failure_note}")
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
              202:
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
                    jobs:
                      type: array
                      items:
                        type: object
              400:
                description: Invalid request
                schema:
                  type: object
                  properties:
                    success:
                      type: boolean
                    error:
                      type: string
              503:
                description: Download queue unavailable
            """
            try:
                data = request.get_json(silent=True)

                if not isinstance(data, dict):
                    return jsonify(
                        {
                            "success": False,
                            "error": "Request body must be a JSON object",
                        }
                    ), 400

                # Support both single "url" and multiple "urls"
                urls = []
                if "urls" in data and isinstance(data["urls"], list):
                    urls = [
                        u.strip()
                        for u in data["urls"]
                        if isinstance(u, str) and u.strip()
                    ]
                elif isinstance(data.get("url"), str) and data["url"].strip():
                    urls = [data["url"].strip()]

                if not urls:
                    return jsonify(
                        {
                            "success": False,
                            "error": "Missing required field: url or urls",
                        }
                    ), 400

                # Validate all URLs
                invalid_urls = [
                    u
                    for u in urls
                    if not u.startswith("http://") and not u.startswith("https://")
                ]
                if invalid_urls:
                    return jsonify(
                        {
                            "success": False,
                            "error": f"Invalid URL(s) (must start with http:// or https://): {invalid_urls}",
                        }
                    ), 400

                if self.submit_urls is None:
                    raise MessagePublishError("Download queue is unavailable")

                jobs = self.submit_urls(urls)
                logger.info("Queued %s download job(s) via API", len(jobs))
                return jsonify(
                    {
                        "success": True,
                        "message": f"Queued {len(urls)} URL(s) for processing",
                        "urls": urls,
                        "count": len(urls),
                        "jobs": [
                            {"job_id": job.job_id, "url": job.url} for job in jobs
                        ],
                    }
                ), 202

            except PartialPublishError as e:
                logger.error(
                    "Queued %s job(s), but %s job(s) were not accepted",
                    len(e.accepted_jobs),
                    len(e.unaccepted_jobs),
                )
                return jsonify(
                    {
                        "success": False,
                        "error": "Download queue accepted only part of the batch",
                        "accepted_jobs": [
                            {"job_id": job.job_id, "url": job.url}
                            for job in e.accepted_jobs
                        ],
                        "unaccepted_urls": [job.url for job in e.unaccepted_jobs],
                    }
                ), 503
            except MessagePublishError as e:
                logger.error("Unable to queue URLs: %s", e)
                return jsonify(
                    {
                        "success": False,
                        "error": "Download queue is unavailable",
                    }
                ), 503
            except Exception as e:
                logger.error(f"Error adding URL via API: {e}", exc_info=True)
                return jsonify({"success": False, "error": "Unable to queue URLs"}), 500

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
                        mimetype="text/plain",
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
                    return Response(
                        "Thumbnail not found", status=404, mimetype="text/plain"
                    )

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
                    message = f'<div style="padding: 10px; background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 4px; margin: 15px 0;">✗ Error: {escape(error)}</div>'

                metadata_dir = Path(self.config.storage.metadata_dir)
                thumbnails_dir = Path(self.config.storage.thumbnails_dir)

                files = []
                total_size_bytes = 0
                for file in sorted(
                    audio_dir.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True
                ):
                    if file.is_file() and file.suffix.lower() in AUDIO_EXTENSIONS:
                        file_size = file.stat().st_size
                        total_size_bytes += file_size
                        size_mb = file_size / (1024 * 1024)

                        # Try to find thumbnail (prefer JPEG first for compatibility)
                        thumbnail_html = ""
                        for ext in [".jpg", ".jpeg", ".webp", ".png"]:
                            thumb_file = thumbnails_dir / f"{file.stem}{ext}"
                            if thumb_file.exists():
                                thumbnail_html = f'<img src="/thumbnails/{quote(thumb_file.name)}" alt="" style="width: 60px; height: 60px; object-fit: cover; border-radius: 4px;">'
                                break

                        # Fallback placeholder if no thumbnail
                        if not thumbnail_html:
                            thumbnail_html = '<div class="thumbnail-placeholder" style="width: 60px; height: 60px; background-color: #ddd; border-radius: 4px; display: flex; align-items: center; justify-content: center; color: #999; font-size: 24px;">🎵</div>'

                        # Look up episode title from metadata
                        episode_title = ""
                        meta_file = metadata_dir / f"{file.stem}.json"
                        if meta_file.exists():
                            try:
                                with open(meta_file) as mf:
                                    meta = json.load(mf)
                                    episode_title = meta.get("title", "")
                            except Exception:
                                pass

                        title_html = (
                            f'<div style="font-weight: 500;">{escape(episode_title)}</div>'
                            if episode_title
                            else ""
                        )
                        audio_path = quote(file.name)
                        escaped_filename = escape(file.name)

                        files.append(
                            f'''<li style="margin: 15px 0; display: flex; align-items: center; gap: 12px;">
                                {thumbnail_html}
                                <div style="flex: 1; min-width: 0; overflow: hidden;">
                                    {title_html}
                                    <a href="/audio/{audio_path}" style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: block; font-size: {"12px; color: #888" if episode_title else "14px"};">{escaped_filename}</a>
                                </div>
                                <span style="color: #666; white-space: nowrap;">({size_mb:.1f} MB)</span>
                                <form method="POST" action="/delete-episode" style="margin: 0;" onsubmit="return confirm('Delete this episode? This cannot be undone.');">
                                    <input type="hidden" name="csrf_token" value="{self.csrf_token}">
                                    <input type="hidden" name="filename" value="{escaped_filename}">
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
                total_size_gb = total_size_bytes / (1024 * 1024 * 1024)
                total_size_mb = total_size_bytes / (1024 * 1024)
                # Show GB if >= 1 GB, otherwise MB
                if total_size_gb >= 1:
                    total_size_str = f"{total_size_gb:.2f} GB"
                else:
                    total_size_str = f"{total_size_mb:.1f} MB"
                episode_count = len(files)
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
                        .stats {{ color: #666; font-size: 14px; margin: 10px 0; }}

                        /* Dark mode */
                        @media (prefers-color-scheme: dark) {{
                            body {{ background-color: #1a1a1a; color: #e0e0e0; }}
                            h1 {{ color: #e0e0e0; }}
                            a {{ color: #4a9eff; }}
                            li span {{ color: #999 !important; }}
                            img {{ border-color: #333; }}
                            .thumbnail-placeholder {{ background-color: #333 !important; }}
                            .stats {{ color: #999; }}
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
                            <input type="hidden" name="csrf_token" value="{self.csrf_token}">
                            <button type="submit" class="delete-all-btn">Delete All Episodes</button>
                        </form>
                    </div>
                    <p class="stats">{episode_count} episodes &middot; {total_size_str} total</p>
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
                if not self._valid_csrf_token():
                    return Response("Forbidden", status=403)
                filename = request.form.get("filename", "").strip()

                if not filename:
                    return redirect("/episodes?error=No filename provided")

                # Security: prevent path traversal by ensuring no directory separators
                # and that the filename is just a basename (no path components)
                if (
                    "/" in filename
                    or "\\" in filename
                    or filename != os.path.basename(filename)
                ):
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
                for ext in [".jpg", ".jpeg", ".png", ".webp"]:
                    thumbnail_file = thumbnails_dir / f"{audio_file.stem}{ext}"
                    if thumbnail_file.exists():
                        thumbnail_file.unlink()
                        logger.info(f"Deleted thumbnail file: {thumbnail_file.name}")
                        break

                # Reload episodes from metadata to update the feed
                self.feed.episodes.clear()
                self.feed.load_episodes_from_metadata(
                    str(metadata_dir),
                    audio_dir=self.config.storage.audio_dir,
                    thumbnails_dir=self.config.storage.thumbnails_dir,
                )

                return redirect("/episodes?success=1")

            except Exception as e:
                logger.error(f"Error deleting episode: {e}", exc_info=True)
                return redirect(f"/episodes?error={str(e)}")

        @self.app.route("/delete-all-episodes", methods=["POST"])
        def delete_all_episodes():
            """Delete all episodes (audio files, metadata, and thumbnails)."""
            try:
                if not self._valid_csrf_token():
                    return Response("Forbidden", status=403)
                audio_dir = Path(self.config.storage.audio_dir)
                metadata_dir = Path(self.config.storage.metadata_dir)
                thumbnails_dir = Path(self.config.storage.thumbnails_dir)

                deleted_count = 0

                # Delete all audio files
                if audio_dir.exists():
                    for audio_file in audio_dir.glob("*"):
                        if audio_file.is_file() and audio_file.suffix.lower() in [
                            ".mp3",
                            ".m4a",
                            ".wav",
                            ".opus",
                            ".aac",
                            ".ogg",
                            ".flac",
                            ".wma",
                            ".aiff",
                            ".webm",
                        ]:
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
                        if thumbnail_file.is_file() and thumbnail_file.suffix in [
                            ".jpg",
                            ".jpeg",
                            ".png",
                            ".webp",
                        ]:
                            thumbnail_file.unlink()
                            logger.info(
                                f"Deleted thumbnail file: {thumbnail_file.name}"
                            )

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
                    return jsonify(
                        {"success": False, "error": "Missing required field: audio"}
                    ), 400

                audio_file = request.files["audio"]
                if audio_file.filename == "":
                    return jsonify(
                        {"success": False, "error": "No audio file selected"}
                    ), 400

                title = request.form.get("title", "").strip()
                if not title:
                    return jsonify(
                        {"success": False, "error": "Missing required field: title"}
                    ), 400

                # Optional fields
                description = request.form.get("description", "").strip()
                source_url = request.form.get("source_url", "").strip()
                image_url_param = request.form.get("image_url", "").strip()
                pub_date_str = request.form.get("pub_date", "").strip()

                # Parse pub_date or use current time
                if pub_date_str:
                    try:
                        pub_date = datetime.fromisoformat(
                            pub_date_str.replace("Z", "+00:00")
                        )
                        # Convert to naive datetime for consistency with existing code
                        if pub_date.tzinfo is not None:
                            pub_date = pub_date.replace(tzinfo=None)
                    except ValueError as e:
                        return jsonify(
                            {"success": False, "error": f"Invalid pub_date format: {e}"}
                        ), 400
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
                                    existing_guid = (
                                        data.get("source_url")
                                        or data.get("youtube_url")
                                        or data.get("audio_url")
                                    )
                                    if existing_guid == guid:
                                        logger.info(
                                            f"Episode already exists with GUID: {guid}"
                                        )
                                        return jsonify(
                                            {
                                                "success": True,
                                                "message": "Episode already exists",
                                                "episode": {
                                                    "title": data.get("title"),
                                                    "audio_url": data.get("audio_url"),
                                                    "image_url": data.get(
                                                        "image_url", ""
                                                    ),
                                                    "pub_date": data.get("pub_date"),
                                                    "guid": existing_guid,
                                                },
                                            }
                                        ), 409
                            except Exception:
                                continue

                # Determine audio file extension - accept any audio format
                original_filename = secure_filename(audio_file.filename)
                file_ext = Path(original_filename).suffix.lower()
                if not file_ext:
                    file_ext = ".mp3"  # Default if no extension

                # Use video_id for filename
                video_id = (
                    extract_video_id(source_url)
                    if source_url
                    else extract_video_id(title)
                )

                # Ensure directories exist
                audio_dir = Path(self.config.storage.audio_dir)
                metadata_dir = Path(self.config.storage.metadata_dir)
                thumbnails_dir = Path(self.config.storage.thumbnails_dir)
                audio_dir.mkdir(parents=True, exist_ok=True)
                metadata_dir.mkdir(parents=True, exist_ok=True)
                thumbnails_dir.mkdir(parents=True, exist_ok=True)

                # Save audio file
                audio_path = audio_dir / f"{video_id}{file_ext}"

                # Handle filename collisions
                counter = 1
                while audio_path.exists():
                    audio_path = audio_dir / f"{video_id}_{counter}{file_ext}"
                    counter += 1

                audio_file.save(str(audio_path))
                logger.info(f"Saved audio file: {audio_path.name}")

                # Get file size
                file_size = audio_path.stat().st_size

                # Generate URLs
                audio_url = (
                    f"{self.config.server.base_url}/audio/{quote(audio_path.name)}"
                )

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
                    video_id=video_id,
                )

                # Save metadata
                metadata_file = metadata_dir / f"{video_id}.json"
                save_episode_metadata(episode, str(metadata_file))

                # Add to feed
                self.feed.add_episode(episode)

                logger.info(f"Created episode via API: {title}")

                return jsonify(
                    {
                        "success": True,
                        "episode": {
                            "title": title,
                            "audio_url": audio_url,
                            "image_url": episode_image_url,
                            "pub_date": pub_date.isoformat(),
                            "guid": final_guid,
                        },
                    }
                ), 201

            except Exception as e:
                logger.error(f"Error creating episode via API: {e}", exc_info=True)
                return jsonify({"success": False, "error": str(e)}), 500

    def _data_status(
        self,
    ) -> tuple[
        RabbitMQStatus,
        KafkaStatus,
        DatabaseStatus,
        list[LifecycleEvent],
    ]:
        try:
            rabbitmq = (
                self.rabbitmq_status()
                if self.rabbitmq_status is not None
                else RabbitMQStatus(False, error="RabbitMQ status is unavailable")
            )
        except Exception:
            logger.exception("RabbitMQ status callback failed")
            rabbitmq = RabbitMQStatus(False, error="RabbitMQ status is unavailable")
        try:
            kafka = (
                self.kafka_status()
                if self.kafka_status is not None
                else KafkaStatus(False, error="Kafka status is unavailable")
            )
        except Exception:
            logger.exception("Kafka status callback failed")
            kafka = KafkaStatus(False, error="Kafka status is unavailable")
        try:
            database = (
                self.database_status()
                if self.database_status is not None
                else DatabaseStatus(
                    False,
                    path="",
                    error="SQLite status is unavailable",
                )
            )
        except Exception:
            logger.exception("SQLite status callback failed")
            database = DatabaseStatus(
                False,
                path="",
                error="SQLite status is unavailable",
            )
        try:
            events = self.recent_events(50) if self.recent_events is not None else []
        except Exception:
            logger.exception("Lifecycle event projection query failed")
            events = []
        return rabbitmq, kafka, database, events

    @staticmethod
    def _format_bytes(size_bytes: int) -> str:
        size = float(size_bytes)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if size < 1024 or unit == "GiB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size_bytes} B"

    def _recent_episodes(self, limit: int = 8) -> list[dict]:
        """Newest episodes with title and thumbnail, for the index page row."""
        audio_dir = Path(self.config.storage.audio_dir)
        if not audio_dir.exists():
            return []

        metadata_dir = Path(self.config.storage.metadata_dir)
        thumbnails_dir = Path(self.config.storage.thumbnails_dir)

        audio_files = [
            file
            for file in audio_dir.glob("*")
            if file.is_file() and file.suffix.lower() in AUDIO_EXTENSIONS
        ]
        audio_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

        episodes = []
        for file in audio_files[:limit]:
            thumbnail = None
            for ext in (".jpg", ".jpeg", ".webp", ".png"):
                thumb_file = thumbnails_dir / f"{file.stem}{ext}"
                if thumb_file.exists():
                    thumbnail = f"/thumbnails/{quote(thumb_file.name)}"
                    break

            title = ""
            meta_file = metadata_dir / f"{file.stem}.json"
            if meta_file.exists():
                try:
                    with open(meta_file) as mf:
                        title = json.load(mf).get("title", "")
                except Exception:
                    logger.debug("Unreadable metadata for %s", file.name)

            episodes.append(
                {
                    "url": f"/audio/{quote(file.name)}",
                    "title": title or file.name,
                    "thumbnail": thumbnail,
                }
            )
        return episodes

    def _valid_csrf_token(self) -> bool:
        token = request.form.get("csrf_token", "")
        return bool(token) and secrets.compare_digest(token, self.csrf_token)

    def _emit_upload(
        self,
        event_type: str,
        job_id: str,
        filename: str,
        batch_id: str,
        detail: Optional[str] = None,
    ) -> None:
        if self.emit_upload_event is None:
            return
        try:
            self.emit_upload_event(
                event_type=event_type,
                job_id=job_id,
                filename=filename,
                batch_id=batch_id,
                detail=detail,
            )
        except Exception:
            logger.exception("Unable to emit upload event %s", event_type)

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

        logger.info(f"Server started: {self.config.server.base_url}/feed.xml")

    def stop(self):
        """Stop the server."""
        logger.info("Stopping HTTP server...")
        # Flask doesn't have a clean way to stop from another thread
        # The daemon thread will be terminated when the main program exits
