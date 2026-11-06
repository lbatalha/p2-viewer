"""macOS AVFoundation capture via an FFmpeg subprocess.

FFmpeg is an external dependency. The camera must be allowed in System
Settings -> Privacy & Security -> Camera for the application that launches
the viewer.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections import deque

from .backend import BackendError
from .frame import FRAME_BYTES, FRAME_HEIGHT, FRAME_STRIDE, FRAME_WIDTH

#: How long to wait for the first frame before assuming capture cannot start.
FIRST_FRAME_TIMEOUT = 5.0

#: Number of FFmpeg stderr lines kept for error messages.
STDERR_LINES = 30


class AVFoundationError(BackendError):
    """Raised when FFmpeg cannot deliver AVFoundation frames."""


class AVFoundationCapture:
    """Capture source backed by FFmpeg's AVFoundation input."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.stride = FRAME_STRIDE
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._condition = threading.Condition()
        self._latest: bytes | None = None
        self._eof = False
        self._stop = threading.Event()
        self._stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self._error: str | None = None

    # -- setup ---------------------------------------------------------------
    @staticmethod
    def _command(ffmpeg: str, path: str) -> list[str]:
        return [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "warning",
            "-nostats",
            "-nostdin",
            "-f", "avfoundation",
            "-framerate", "25",
            "-video_size", f"{FRAME_WIDTH}x{FRAME_HEIGHT}",
            "-pixel_format", "yuyv422",
            "-i", f"{path}:none",
            "-pix_fmt", "yuyv422",
            "-f", "rawvideo",
            "pipe:1",
        ]

    def open(self) -> None:
        if self._proc is not None:
            return

        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise AVFoundationError(
                "ffmpeg was not found. On macOS, install it with "
                "`brew install ffmpeg`; it is required for camera capture."
            )

        try:
            self._proc = subprocess.Popen(
                self._command(ffmpeg, self.path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            raise AVFoundationError(f"could not start ffmpeg: {exc}") from exc

        self._stderr_reader = threading.Thread(
            target=self._read_stderr, name="p2-ffmpeg-stderr", daemon=True
        )
        self._reader = threading.Thread(
            target=self._read_frames, name="p2-ffmpeg-frames", daemon=True
        )
        self._stderr_reader.start()
        self._reader.start()

        try:
            self._wait_for_first_frame(FIRST_FRAME_TIMEOUT)
        except AVFoundationError:
            self.close()
            raise

    def _wait_for_first_frame(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._latest is None and not self._eof:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)

            if self._latest is not None:
                return

            reason = self._error
            if reason is None and self._eof:
                reason = self._format_detail("ffmpeg stopped before delivering a frame")
            if reason is None:
                reason = self._format_detail(
                    f"timed out after {timeout:.1f}s waiting for the first frame"
                )
            self._error = reason

        raise AVFoundationError(reason)

    # -- threads -------------------------------------------------------------
    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                with self._condition:
                    self._stderr.append(line)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _read_exact(stream, size: int) -> bytes | None:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_frames(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        try:
            while not self._stop.is_set():
                data = self._read_exact(stream, FRAME_BYTES)
                if data is None:
                    break
                with self._condition:
                    if self._stop.is_set():
                        break
                    self._latest = data
                    self._condition.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._condition:
                self._eof = True
                if not self._stop.is_set() and self._error is None:
                    self._error = self._format_detail("ffmpeg stopped")
                self._condition.notify_all()

    # -- reads ---------------------------------------------------------------
    def read(self) -> bytes | None:
        """Return the next raw frame, blocking until one is available.

        Returns ``None`` once the stream has ended.  ``error`` then holds the
        reason, if FFmpeg stopped unexpectedly.
        """
        with self._condition:
            while (
                self._latest is None
                and not self._eof
                and not self._stop.is_set()
            ):
                self._condition.wait()

            if self._latest is not None:
                data = self._latest
                self._latest = None
                return data
            return None

    @property
    def error(self) -> str | None:
        with self._condition:
            return self._error

    # -- errors --------------------------------------------------------------
    def _format_detail(self, summary: str) -> str:
        lines = [line for line in self._stderr if line.strip()]
        detail = "\n".join(lines[-8:])
        message = (
            f"could not capture from {self.path!r} via AVFoundation: {summary}."
        )
        if detail:
            message += f"\nffmpeg: {detail}"
        message += (
            "\nCheck that the camera is connected, that the device index or "
            "name is correct (`ffmpeg -f avfoundation -list_devices true -i \"\"`), "
            "and that the terminal/IDE running the viewer has Camera permission "
            "in System Settings -> Privacy & Security -> Camera. "
            "macOS does not allow camera access from SSH sessions."
        )
        return message

    # -- teardown ------------------------------------------------------------
    def close(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()

        proc = self._proc
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2)

        self._proc = None
        self._reader = None
        self._stderr_reader = None


__all__ = ["AVFoundationCapture", "AVFoundationError"]
