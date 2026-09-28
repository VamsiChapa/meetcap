"""Local web app for the notes library: http://127.0.0.1:8765 (bookmark it).

Runs inside the menu bar agent, so it is up whenever meetcap is. Listens on 127.0.0.1 only and rejects
requests whose Host header isn't this machine (blocks DNS-rebinding tricks from web pages).

    GET  /                               the app (single page)
    GET  /api/meetings?q=                list / search
    GET  /api/meetings/<id>              notes, key moments, transcript
    GET  /api/meetings/<id>/audio        AAC audio, byte ranges supported (seeking)
    GET  /api/meetings/<id>/video        screen recording streamed from Blob (ranges)
    GET  /api/meetings/<id>/frames/<f>   screenshot
    GET  /api/status                     recording state + queue
    POST /api/sync                       pull new meetings from Azure now
"""
from __future__ import annotations

import json
import logging
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .library import MeetingLibrary
from .pet import sprite_resource

log = logging.getLogger(__name__)

DEFAULT_PORT = 8765
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


def _asset(name: str) -> bytes:
    return resources.files("meetcap").joinpath(name).read_bytes()


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """'bytes=100-199' -> (100, 199) clamped to the file; None = whole file."""
    if not header:
        return None
    m = _RANGE.fullmatch(header.strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    if m.group(1) == "":                     # suffix range: last N bytes
        n = int(m.group(2))
        return max(0, size - n), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size:
        raise ValueError("range not satisfiable")
    return start, min(end, size - 1)


def make_handler(library: MeetingLibrary, status_fn: Callable[[], dict] | None, port: int,
                 species_fn: Callable[[], str] = lambda: "puppy"):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "meetcap"

        def log_message(self, fmt, *args):  # quiet: meetcap's own log is enough
            log.debug("web %s", fmt % args)

        # -- helpers ------------------------------------------------------------------
        def _host_ok(self) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                return False
            return True

        def _send(self, code, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, data: Any, code=HTTPStatus.OK) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _file(self, path: Path, ctype: str) -> None:
            size = path.stat().st_size
            try:
                rng = parse_range(self.headers.get("Range"), size)
            except ValueError:
                self._send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"", ctype, {"Content-Range": f"bytes */{size}"})
                return
            with open(path, "rb") as fh:
                if rng is None:
                    self._send(HTTPStatus.OK, fh.read(), ctype, {"Accept-Ranges": "bytes"})
                else:
                    fh.seek(rng[0])
                    body = fh.read(rng[1] - rng[0] + 1)
                    self._send(HTTPStatus.PARTIAL_CONTENT, body, ctype,
                               {"Accept-Ranges": "bytes", "Content-Range": f"bytes {rng[0]}-{rng[1]}/{size}"})

        def _blob_stream(self, name: str, size: int, ctype: str) -> None:
            try:
                rng = parse_range(self.headers.get("Range"), size) or (0, min(size, 4 * 1024 * 1024) - 1)
            except ValueError:
                self._send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"", ctype, {"Content-Range": f"bytes */{size}"})
                return
            end = min(rng[1], rng[0] + 8 * 1024 * 1024 - 1)  # at most 8 MB per request; the player asks for more
            body = library.blob.download_range(name, rng[0], end - rng[0] + 1)
            self._send(HTTPStatus.PARTIAL_CONTENT, body, ctype,
                       {"Accept-Ranges": "bytes", "Content-Range": f"bytes {rng[0]}-{end}/{size}"})

        # -- routes -------------------------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._host_ok():
                return
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            try:
                if not parts:
                    return self._send(HTTPStatus.OK, _asset("web/index.html"), "text/html; charset=utf-8",
                                      {"Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
                                       "media-src 'self'; style-src 'self' 'unsafe-inline'; "
                                       "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"})
                if parts == ["favicon.png"]:
                    return self._send(HTTPStatus.OK, _asset("icons/idle_0.png"), "image/png",
                                      {"Cache-Control": "max-age=86400"})
                if len(parts) == 2 and parts[0] == "pet" and re.fullmatch(r"[a-z]+_[rl]_\d\.png", parts[1]):
                    return self._send(HTTPStatus.OK, _asset(sprite_resource(species_fn(), parts[1][:-4])), "image/png")
                if parts == ["api", "status"]:
                    st = status_fn() if status_fn else {}
                    st.update(last_sync=library.last_sync, sync_error=library.last_sync_error)
                    return self._json(st)
                if parts == ["api", "meetings"]:
                    q = parse_qs(url.query).get("q", [""])[0][:200]
                    return self._json({"meetings": library.list(q), "processing": [] if q else library.processing()})
                if len(parts) >= 3 and parts[:2] == ["api", "meetings"]:
                    mid = parts[2]
                    if not re.fullmatch(r"[0-9a-f]{6,40}", mid):
                        return self._json({"error": "bad id"}, HTTPStatus.BAD_REQUEST)
                    if len(parts) == 3:
                        m = library.get(mid)
                        return self._json(m) if m else self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                    if parts[3] == "audio":
                        f = library.audio_file(mid)
                        return self._file(f, "audio/mp4") if f else self._json({"error": "no audio"}, HTTPStatus.NOT_FOUND)
                    if parts[3] == "video":
                        v = library.video_blob(mid)
                        if not v:
                            return self._json({"error": "no video"}, HTTPStatus.NOT_FOUND)
                        return self._blob_stream(v[0], v[1], "video/mp4" if v[0].endswith(".mp4") else "video/quicktime")
                    if parts[3] == "frames" and len(parts) == 5:
                        f = library.frame_file(mid, parts[4])
                        return self._file(f, "image/jpeg") if f else self._json({"error": "no frame"}, HTTPStatus.NOT_FOUND)
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (BrokenPipeError, ConnectionResetError):
                return None
            except Exception as exc:  # noqa: BLE001 - show the reason in the page
                log.warning("web request %s failed: %s", self.path, exc)
                return self._json({"error": str(exc)[:300]}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self):
            if not self._host_ok():
                return
            if urlparse(self.path).path == "/api/sync":
                if self.headers.get("X-Meetcap") != "1":        # a plain cross-site form can't set this header
                    return self._json({"error": "missing header"}, HTTPStatus.FORBIDDEN)
                added = library.sync()
                return self._json({"added": added, "error": library.last_sync_error})
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def serve(library: MeetingLibrary, port: int = DEFAULT_PORT, status_fn: Callable[[], dict] | None = None,
          background: bool = True, species_fn: Callable[[], str] = lambda: "puppy") -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    httpd.RequestHandlerClass = make_handler(library, status_fn, httpd.server_address[1], species_fn)  # real port if 0
    httpd.daemon_threads = True
    if background:
        threading.Thread(target=httpd.serve_forever, name="meetcap-web", daemon=True).start()
        log.info("notes library at http://127.0.0.1:%d", httpd.server_address[1])
    return httpd
