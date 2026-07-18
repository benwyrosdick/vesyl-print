#!/usr/bin/env python3
"""Stream the SPI LCD as MJPEG for local demos.

Used two ways:

1. **Embedded in the display app** (default) — ``main.py`` publishes each
   rendered frame and serves  http://<pi-ip>:8765/

2. **Standalone** (optional) — poll ``/dev/fb1`` without the display loop:

       python3 stream_lcd.py --port 8765 --fps 2 --scale 1

Needs group ``video`` for ``/dev/fb1``. Binds 0.0.0.0 by default (trusted LAN only).

The HTML page shows the LCD at the top and live Pi stats tables below
(pairing, system, printers, jobs, OTA). Stats also available as JSON at
``/api/stats``.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from PIL import Image

log = logging.getLogger("vesyl-print.stream")

BOUNDARY = b"frame"
DEFAULT_PORT = 8765
DEFAULT_FPS = 2.0
DEFAULT_SCALE = 1.0
DEFAULT_QUALITY = 80
_STATS_CACHE_S = 2.0

HTML_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>VESYL Print — LCD</title>
  <style>
    :root {{
      --bg: #101218;
      --panel: #181c24;
      --border: #2a303c;
      --fg: #ebeef5;
      --muted: #8c94a5;
      --accent: #edfc33;
      --ok: #50dc78;
      --warn: #ffb43c;
      --down: #e84848;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0; min-height: 100%; background: var(--bg); color: var(--fg);
      font-family: system-ui, -apple-system, Segoe UI, sans-serif;
    }}
    .page {{
      max-width: 960px;
      margin: 0 auto;
      padding: 16px 16px 40px;
      display: flex;
      flex-direction: column;
      align-items: stretch;
      gap: 16px;
    }}
    header.bar {{
      display: flex; flex-wrap: wrap; align-items: baseline;
      justify-content: space-between; gap: 8px 16px;
    }}
    h1 {{
      font-size: 13px; font-weight: 600; letter-spacing: 0.06em;
      color: var(--muted); margin: 0; text-transform: uppercase;
    }}
    .meta {{ font-size: 12px; color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .lcd-wrap {{
      align-self: flex-start;
      background: #000;
      border: 1px solid var(--border);
      border-radius: 6px;
      box-shadow: 0 8px 28px rgba(0,0,0,0.4);
      overflow: hidden;
      line-height: 0;
    }}
    .lcd-wrap img {{
      image-rendering: pixelated;
      image-rendering: crisp-edges;
      display: block;
      max-width: min(96vw, {css_max}px);
      width: {w}px;
      height: auto;
      background: #000;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
    }}
    section.card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 14px 10px;
      min-width: 0;
    }}
    section.card h2 {{
      margin: 0 0 10px;
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      text-align: left;
      padding: 5px 0;
      vertical-align: top;
      border-bottom: 1px solid rgba(42,48,60,0.65);
    }}
    tr:last-child th, tr:last-child td {{ border-bottom: none; }}
    th {{
      width: 38%;
      color: var(--muted);
      font-weight: 500;
      padding-right: 12px;
    }}
    td {{
      color: var(--fg);
      word-break: break-word;
      font-variant-numeric: tabular-nums;
    }}
    .dot {{
      display: inline-block;
      width: 8px; height: 8px;
      border-radius: 50%;
      margin-right: 6px;
      vertical-align: middle;
    }}
    .dot.ok {{ background: var(--ok); }}
    .dot.warn {{ background: var(--warn); }}
    .dot.down {{ background: var(--down); }}
    .dot.muted {{ background: var(--muted); }}
    .empty {{ color: var(--muted); font-size: 13px; margin: 0; }}
    .err {{ color: var(--down); }}
    footer.foot {{
      font-size: 11px; color: var(--muted);
      display: flex; flex-wrap: wrap; gap: 8px 16px;
      justify-content: space-between;
    }}
  </style>
</head>
<body>
  <div class="page">
    <header class="bar">
      <h1>VESYL Print LCD</h1>
      <div class="meta">{w}&times;{h} · ~{fps} fps ·
        <a href="/snapshot.jpg">snapshot</a> ·
        <a href="/api/stats">api/stats</a>
      </div>
    </header>
    <div class="lcd-wrap">
      <img src="/stream.mjpg" alt="LCD stream" width="{w}" height="{h}"/>
    </div>
    <div class="stats" id="stats">
      <p class="empty">Loading stats…</p>
    </div>
    <footer class="foot">
      <span id="refreshed">—</span>
      <span>Trusted LAN only · polls /api/stats every 2s</span>
    </footer>
  </div>
  <script>
    const root = document.getElementById('stats');
    const refreshed = document.getElementById('refreshed');

    function esc(s) {{
      if (s == null || s === '') return '—';
      return String(s)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;')
        .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }}

    function levelClass(level) {{
      if (level === 'ok') return 'ok';
      if (level === 'warn') return 'warn';
      if (level === 'down') return 'down';
      return 'muted';
    }}

    function row(k, v, level) {{
      let val = esc(v);
      if (level) {{
        val = '<span class="dot ' + levelClass(level) + '"></span>' + val;
      }}
      return '<tr><th>' + esc(k) + '</th><td>' + val + '</td></tr>';
    }}

    function card(title, rowsHtml) {{
      return '<section class="card"><h2>' + esc(title) + '</h2>'
        + '<table><tbody>' + rowsHtml + '</tbody></table></section>';
    }}

    function pairingLevel(p) {{
      if (!p) return 'muted';
      if (p.pairing === 'revoked') return 'down';
      if (p.pairing !== 'paired') return 'warn';
      if (p.cloud === 'online') return 'ok';
      if (p.cloud === 'offline') return 'down';
      return 'warn';
    }}

    function printerLevel(status) {{
      const s = (status || '').toLowerCase();
      if (s === 'idle' || s === 'online') return 'ok';
      if (s === 'printing' || s === 'processing') return 'warn';
      if (s === 'stopped' || s === 'offline' || s === 'error') return 'down';
      return 'muted';
    }}

    function render(data) {{
      const p = data.pairing || {{}};
      const s = data.system || {{}};
      const j = data.jobs || {{}};
      const u = data.update || {{}};
      const printers = data.printers || [];

      let html = '';
      html += card('Pairing',
        row('Pairing', p.pairing, pairingLevel(p))
        + row('Cloud', p.cloud, p.cloud === 'online' ? 'ok' : (p.cloud === 'offline' ? 'down' : 'muted'))
        + row('Node', p.name || p.node_id)
        + row('Organization', p.organization_name)
        + row('Warehouse', p.warehouse_name)
        + row('Last heartbeat', p.last_heartbeat_age
              || p.last_heartbeat_at
              || null)
        + (p.last_heartbeat_at && p.last_heartbeat_age
              ? row('Heartbeat at', p.last_heartbeat_at) : '')
        + row('Agent version', p.agent_version)
        + (p.last_error ? row('Last error', p.last_error, 'down') : '')
      );

      html += card('System',
        row('Hostname', s.hostname)
        + row('LAN IP', s.primary_ip)
        + row('All IPs', (s.ip_addresses || []).join(', ') || null)
        + row('Tailscale', s.tailscale_ip)
        + row('CPU temp', s.cpu_temp)
        + row('Platform', s.platform)
        + row('Time', s.local_time)
      );

      let jobRows =
        row('Queued', j.queued)
        + row('Processed (markers)', j.processed)
        + row('Queue dir', j.queue_dir)
        + row('Processed dir', j.processed_dir);
      html += card('Jobs', jobRows);

      if (printers.length === 0) {{
        html += card('Printers', row('Status', 'No CUPS network queues', 'muted'));
      }} else {{
        let pr = '';
        for (const prn of printers) {{
          const label = prn.status_message || prn.status || 'unknown';
          pr += row(prn.display_name || prn.cups_name, label, printerLevel(prn.status));
        }}
        html += card('Printers', pr);
      }}

      let ota =
        row('Status', u.status || 'idle',
            (u.status && u.status !== 'idle') ? 'warn' : 'muted')
        + row('Current', u.current_version)
        + row('Target', u.target_version)
        + row('Channel', u.channel);
      if (u.last_error) ota += row('Last error', u.last_error, 'down');
      html += card('OTA / update', ota);

      const paths = data.paths || {{}};
      html += card('Paths',
        row('Config', paths.config_dir)
        + row('State', paths.state_dir)
        + row('Status file', paths.status_path)
        + row('Credentials', paths.credentials_present === true
              ? 'present' : (paths.credentials_present === false ? 'missing' : '—'))
        + row('API base', paths.api_base_url)
      );

      root.innerHTML = html;
      refreshed.textContent = 'Updated ' + (data.collected_at || new Date().toISOString());
    }}

    async function poll() {{
      try {{
        const r = await fetch('/api/stats', {{ cache: 'no-store' }});
        if (!r.ok) throw new Error('HTTP ' + r.status);
        render(await r.json());
      }} catch (e) {{
        root.innerHTML = '<section class="card"><h2>Stats</h2>'
          + '<p class="err">Failed to load stats: ' + esc(e.message) + '</p></section>';
      }}
    }}
    poll();
    setInterval(poll, 2000);
  </script>
</body>
</html>
"""


class FrameSource:
    """Thread-safe latest JPEG. Fed by ``publish()`` or a capture loop."""

    def __init__(
        self,
        *,
        scale: float = DEFAULT_SCALE,
        quality: int = DEFAULT_QUALITY,
        native_size: tuple[int, int] | None = None,
    ):
        self.scale = max(scale, 0.25)
        self.quality = max(40, min(quality, 95))
        self._native = native_size or (480, 320)
        self._jpeg = b""
        self._lock = threading.Lock()
        self._error: str | None = None

    @property
    def size(self) -> tuple[int, int]:
        w, h = self._native
        if self.scale != 1.0:
            return (max(1, round(w * self.scale)), max(1, round(h * self.scale)))
        return (w, h)

    def set_native_size(self, size: tuple[int, int]) -> None:
        self._native = size

    def publish(self, image: Image.Image) -> None:
        """Encode ``image`` to JPEG for stream clients."""
        try:
            self._native = image.size
            img = image
            if img.mode != "RGB":
                img = img.convert("RGB")
            if self.scale != 1.0:
                tw, th = self.size
                img = img.resize((tw, th), Image.NEAREST)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=self.quality, optimize=True)
            data = buf.getvalue()
            with self._lock:
                self._jpeg = data
                self._error = None
        except Exception as e:
            with self._lock:
                self._error = str(e)

    def latest_jpeg(self) -> bytes:
        with self._lock:
            return self._jpeg

    def error(self) -> str | None:
        with self._lock:
            return self._error


class FrameGrabber:
    """Background thread that captures /dev/fb1 into a FrameSource (standalone)."""

    def __init__(
        self,
        device: str,
        source: FrameSource,
        fps: float,
    ):
        from framebuffer import Framebuffer

        self.fb = Framebuffer(device)
        source.set_native_size(self.fb.size)
        self.source = source
        self.interval = 1.0 / max(fps, 0.2)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="lcd-grabber", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.source.publish(self.fb.capture())
            except Exception as e:
                with self.source._lock:
                    self.source._error = str(e)
            elapsed = time.monotonic() - t0
            self._stop.wait(max(0.0, self.interval - elapsed))


def _count_json_files(directory: Path | None) -> int:
    if directory is None or not directory.is_dir():
        return 0
    try:
        return sum(1 for f in directory.iterdir() if f.is_file() and f.suffix == ".json")
    except OSError:
        return 0


def collect_stats(
    *,
    status_path: Path | str | None = None,
    update_status_path: Path | str | None = None,
    queue_dir: Path | str | None = None,
    processed_dir: Path | str | None = None,
    credentials_path: Path | str | None = None,
    config_dir: Path | str | None = None,
    state_dir: Path | str | None = None,
    api_base_url: str | None = None,
    include_printers: bool = True,
) -> dict[str, Any]:
    """Gather pairing / system / print / OTA snapshot for the stream page."""
    # Lazy imports keep standalone stream import light when modules fail.
    import statusio
    import sysinfo
    import update as update_mod
    from config import AGENT_VERSION, default_platform
    from display_status import format_agent_version, heartbeat_age_label

    st = None
    if status_path:
        st = statusio.read_status(Path(status_path))

    pairing: dict[str, Any] = {
        "pairing": st.pairing if st else "unpaired",
        "cloud": st.cloud if st else "unknown",
        "node_id": st.node_id if st else None,
        "name": st.name if st else None,
        "organization_name": st.organization_name if st else None,
        "warehouse_name": st.warehouse_name if st else None,
        "last_heartbeat_at": st.last_heartbeat_at if st else None,
        "last_heartbeat_age": heartbeat_age_label(
            st.last_heartbeat_at if st else None
        ),
        "last_error": st.last_error if st else None,
        "agent_version": format_agent_version(
            (st.agent_version if st and st.agent_version else None) or AGENT_VERSION
        ),
        "status_updated_at": st.updated_at if st else None,
    }

    system = {
        "hostname": sysinfo.hostname(),
        "primary_ip": sysinfo.primary_ip(),
        "ip_addresses": sysinfo.ip_addresses(),
        "tailscale_ip": sysinfo.tailscale_ip(),
        "cpu_temp": sysinfo.cpu_temp_c(),
        "platform": default_platform(),
        "local_time": sysinfo.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    qdir = Path(queue_dir) if queue_dir else None
    pdir = Path(processed_dir) if processed_dir else None
    jobs = {
        "queued": _count_json_files(qdir),
        "processed": _count_json_files(pdir),
        "queue_dir": str(qdir) if qdir else None,
        "processed_dir": str(pdir) if pdir else None,
    }

    printers_list: list[dict[str, Any]] = []
    if include_printers:
        try:
            import printers

            for item in printers.inventory_payload():
                printers_list.append(
                    {
                        "cups_name": item.get("cups_name"),
                        "display_name": item.get("display_name"),
                        "status": item.get("status"),
                        "status_message": item.get("status_message"),
                        "uri": item.get("uri"),
                    }
                )
        except Exception as e:
            log.debug("printer inventory for stream stats failed: %s", e)

    ust = None
    if update_status_path:
        ust = update_mod.read_update_status(Path(update_status_path))
    update_info: dict[str, Any] = {
        "status": ust.status if ust else "idle",
        "current_version": (
            (ust.current_version if ust and ust.current_version else None)
            or format_agent_version(AGENT_VERSION)
        ),
        "target_version": ust.target_version if ust else None,
        "channel": ust.channel if ust else None,
        "last_error": ust.last_error if ust else None,
        "last_checked_at": ust.last_checked_at if ust else None,
        "previous_version": ust.previous_version if ust else None,
    }

    creds_present: bool | None = None
    if credentials_path:
        creds_present = Path(credentials_path).is_file()

    paths = {
        "config_dir": str(config_dir) if config_dir else None,
        "state_dir": str(state_dir) if state_dir else None,
        "status_path": str(status_path) if status_path else None,
        "credentials_path": str(credentials_path) if credentials_path else None,
        "credentials_present": creds_present,
        "api_base_url": api_base_url,
    }

    return {
        "collected_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "pairing": pairing,
        "system": system,
        "jobs": jobs,
        "printers": printers_list,
        "update": update_info,
        "paths": paths,
    }


class StatsProvider:
    """Cached stats collector for the HTTP handler."""

    def __init__(
        self,
        collector: Callable[[], dict[str, Any]] | None = None,
        *,
        cache_s: float = _STATS_CACHE_S,
    ):
        self._collector = collector
        self._cache_s = cache_s
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    def get(self) -> dict[str, Any]:
        if self._collector is None:
            return {
                "collected_at": datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat(),
                "pairing": {},
                "system": {},
                "jobs": {},
                "printers": [],
                "update": {},
                "paths": {},
                "error": "stats not configured",
            }
        now = time.monotonic()
        with self._lock:
            if self._cached is not None and (now - self._cached_at) < self._cache_s:
                return self._cached
        try:
            data = self._collector()
        except Exception as e:
            log.exception("stats collect failed")
            data = {
                "collected_at": datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat(),
                "error": str(e),
                "pairing": {},
                "system": {},
                "jobs": {},
                "printers": [],
                "update": {},
                "paths": {},
            }
        with self._lock:
            self._cached = data
            self._cached_at = time.monotonic()
            return data


def make_handler(
    source: FrameSource,
    fps: float,
    stats: StatsProvider | None = None,
) -> type[BaseHTTPRequestHandler]:
    stats_provider = stats or StatsProvider(None)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._serve_html()
            elif path in ("/stream.mjpg", "/stream.mjpeg", "/mjpeg"):
                self._serve_mjpeg()
            elif path in ("/snapshot.jpg", "/snapshot.jpeg", "/snap.jpg"):
                self._serve_snapshot()
            elif path in ("/api/stats", "/stats.json"):
                self._serve_stats()
            elif path == "/health":
                self._serve_health()
            else:
                self.send_error(404, "not found")

        def _serve_html(self) -> None:
            w, h = source.size
            body = HTML_PAGE.format(
                w=w, h=h, fps=fps, css_max=max(w, 480) * 2
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _serve_stats(self) -> None:
            data = stats_provider.get()
            body = json.dumps(data, indent=2).encode("utf-8") + b"\n"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _serve_snapshot(self) -> None:
            data = source.latest_jpeg()
            if not data:
                self.send_error(503, source.error() or "no frame yet")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _serve_health(self) -> None:
            body = b"ok\n" if source.latest_jpeg() else b"warming\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_mjpeg(self) -> None:
            self.send_response(200)
            self.send_header(
                "Content-Type",
                f"multipart/x-mixed-replace; boundary={BOUNDARY.decode()}",
            )
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            interval = 1.0 / max(fps, 0.2)
            try:
                while True:
                    data = source.latest_jpeg()
                    if data:
                        header = (
                            b"--" + BOUNDARY + b"\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(data)).encode() + b"\r\n"
                            b"\r\n"
                        )
                        self.wfile.write(header)
                        self.wfile.write(data)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    time.sleep(interval)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return Handler


class LcdStreamServer:
    """Background HTTP MJPEG server fed by ``publish(image)`` each display frame."""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        fps: float = DEFAULT_FPS,
        scale: float = DEFAULT_SCALE,
        quality: int = DEFAULT_QUALITY,
        native_size: tuple[int, int] | None = None,
        stats_collector: Callable[[], dict[str, Any]] | None = None,
    ):
        self.host = host
        self.port = port
        self.fps = fps
        self.source = FrameSource(
            scale=scale, quality=quality, native_size=native_size
        )
        self.stats = StatsProvider(stats_collector)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        handler = make_handler(self.source, self.fps, self.stats)
        try:
            self._server = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as e:
            log.warning("LCD stream not started on %s:%s: %s", self.host, self.port, e)
            self._server = None
            return
        # daemon thread so display service can exit on SIGTERM without hang
        self._thread = threading.Thread(
            target=self._serve, name="lcd-stream", daemon=True
        )
        self._thread.start()
        log.info(
            "LCD stream http://0.0.0.0:%s/ (scale=%s fps~%s)",
            self.port,
            self.source.scale,
            self.fps,
        )

    def _serve(self) -> None:
        assert self._server is not None
        try:
            self._server.serve_forever(poll_interval=0.5)
        except Exception:
            log.exception("LCD stream server stopped with error")

    def publish(self, image: Image.Image) -> None:
        self.source.publish(image)

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None


def _default_stats_collector() -> Callable[[], dict[str, Any]]:
    from config import load_config

    cfg = load_config()

    def _collect() -> dict[str, Any]:
        return collect_stats(
            status_path=cfg.status_path,
            update_status_path=cfg.update_status_path,
            queue_dir=cfg.queue_dir,
            processed_dir=cfg.processed_dir,
            credentials_path=cfg.credentials_path,
            config_dir=cfg.config_dir,
            state_dir=cfg.state_dir,
            api_base_url=cfg.api_base_url,
        )

    return _collect


def main() -> None:
    ap = argparse.ArgumentParser(description="MJPEG stream of the vesyl-print LCD")
    ap.add_argument("--device", default="/dev/fb1", help="framebuffer device")
    ap.add_argument("--host", default="0.0.0.0", help="bind address")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP port")
    ap.add_argument(
        "--fps", type=float, default=DEFAULT_FPS, help="capture / stream rate"
    )
    ap.add_argument(
        "--scale",
        type=float,
        default=DEFAULT_SCALE,
        help="upscale factor (nearest-neighbor)",
    )
    ap.add_argument(
        "--quality", type=int, default=DEFAULT_QUALITY, help="JPEG quality 40-95"
    )
    args = ap.parse_args()

    try:
        source = FrameSource(scale=args.scale, quality=args.quality)
        grabber = FrameGrabber(args.device, source, args.fps)
    except (OSError, RuntimeError) as e:
        print(f"Cannot open {args.device}: {e}", file=sys.stderr)
        print("Is the display service running? Are you in group 'video'?", file=sys.stderr)
        raise SystemExit(1) from e

    grabber.start()
    for _ in range(50):
        if source.latest_jpeg():
            break
        time.sleep(0.05)

    stats = StatsProvider(_default_stats_collector())
    handler = make_handler(source, args.fps, stats)
    server = ThreadingHTTPServer((args.host, args.port), handler)

    def _shutdown(*_args: object) -> None:
        print("\nStopping…", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    w, h = source.size
    print(
        f"Streaming {args.device} ({grabber.fb.width}x{grabber.fb.height}"
        f" → {w}x{h} @ ~{args.fps} fps)",
        flush=True,
    )
    print(f"  Open  http://<pi-ip>:{args.port}/", flush=True)
    print(f"  Snap  http://<pi-ip>:{args.port}/snapshot.jpg", flush=True)
    print(f"  Stats http://<pi-ip>:{args.port}/api/stats", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        grabber.stop()
        server.server_close()


if __name__ == "__main__":
    main()
