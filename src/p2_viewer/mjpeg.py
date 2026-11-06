"""HTTP MJPEG output.

The viewer listens on the address in ``--out`` and serves a multipart MJPEG
stream. Each connected reader receives the latest encoded frame.
"""

from __future__ import annotations

import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import numpy as np
from PIL import Image

from .backend import BackendError

#: Boundary between JPEG frames in the multipart response.
BOUNDARY = "frame"

#: JPEG quality used for the served stream.
JPEG_QUALITY = 96


class _MjpegServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class MjpegSink:
    """Serve rendered BGR frames as an HTTP multipart MJPEG stream."""

    def __init__(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "http":
            raise BackendError(f"not an HTTP MJPEG URL: {url!r}")
        host = parsed.hostname
        if not host:
            raise BackendError(
                f"HTTP output needs a bind address, for example "
                f"http://0.0.0.0:8080/stream.mjpg (got {url!r})"
            )
        self._bind_host = host
        self._bind_port = parsed.port or 8080
        path = parsed.path or "/stream.mjpg"
        if path == "/":
            path = "/stream.mjpg"
        self._path = path

        self._server: _MjpegServer | None = None
        self._thread: threading.Thread | None = None
        self._condition = threading.Condition()
        self._jpeg: bytes | None = None
        self._frame_id = 0
        self._clients = 0
        self._closed = False

    # -- lifecycle -----------------------------------------------------------
    def open(self, width: int, height: int) -> None:
        if self._server is not None:
            return

        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                sink._serve_client(self)

            def log_message(self, format: str, *args) -> None:
                pass

        try:
            self._server = _MjpegServer(
                (self._bind_host, self._bind_port), Handler
            )
        except OSError as exc:
            raise BackendError(
                f"could not listen on {self._bind_host}:{self._bind_port}: {exc}"
            ) from exc

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="p2-mjpeg-server",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

        server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    # -- frames --------------------------------------------------------------
    def write(self, frame: np.ndarray) -> None:
        """Encode one BGR frame, if at least one client is connected."""
        with self._condition:
            if self._clients == 0 or self._closed:
                return

        try:
            rgb = np.ascontiguousarray(frame[:, :, ::-1])
            buffer = io.BytesIO()
            Image.fromarray(rgb, "RGB").save(
                buffer, format="JPEG", quality=JPEG_QUALITY
            )
            jpeg = buffer.getvalue()
        except Exception as exc:  # noqa: BLE001 - Pillow raises various types
            raise BackendError(f"could not encode MJPEG frame: {exc}") from exc

        with self._condition:
            self._jpeg = jpeg
            self._frame_id += 1
            self._condition.notify_all()

    # -- HTTP ----------------------------------------------------------------
    def _serve_client(self, handler: BaseHTTPRequestHandler) -> None:
        if urlparse(handler.path).path != self._path:
            handler.send_error(404, "not the MJPEG stream path")
            return

        handler.send_response(200)
        handler.send_header(
            "Content-Type",
            f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        )
        handler.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        handler.send_header("Pragma", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.flush()

        with self._condition:
            self._clients += 1

        last_frame = -1
        try:
            while True:
                with self._condition:
                    while (
                        not self._closed
                        and (self._jpeg is None or self._frame_id == last_frame)
                    ):
                        self._condition.wait(timeout=1.0)
                    if self._closed:
                        break
                    jpeg = self._jpeg
                    frame_id = self._frame_id

                if jpeg is None:
                    continue
                last_frame = frame_id
                header = (
                    f"--{BOUNDARY}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n"
                    f"\r\n"
                ).encode("ascii")
                handler.wfile.write(header)
                handler.wfile.write(jpeg)
                handler.wfile.write(b"\r\n")
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self._condition:
                self._clients -= 1


__all__ = ["MjpegSink"]
