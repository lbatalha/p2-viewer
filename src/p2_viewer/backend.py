"""Backend protocols and selection."""

from __future__ import annotations

import sys
from typing import Protocol

import numpy as np


class BackendError(RuntimeError):
    """Raised when a capture or output backend cannot be used."""


class CaptureSource(Protocol):
    """A device that produces raw P2 Pro frames."""

    #: Bytes per row as reported by the backend.
    stride: int

    def open(self) -> None: ...
    def read(self) -> bytes | None:
        """Return one raw frame buffer, or None if none is available."""
    def close(self) -> None: ...


class FrameSink(Protocol):
    """A device that accepts rendered frames."""

    def open(self, width: int, height: int) -> None: ...
    def write(self, frame: np.ndarray) -> None: ...
    def close(self) -> None: ...


if sys.platform == "darwin":
    BACKENDS = ("avfoundation", "opencv")
    DEFAULT_BACKEND = "avfoundation"
else:
    BACKENDS = ("v4l2", "opencv")
    DEFAULT_BACKEND = "v4l2"


def open_capture(backend: str, path: str) -> CaptureSource:
    """Instantiate a capture backend by name."""
    if backend == "v4l2":
        if sys.platform == "darwin":
            raise BackendError(
                "the v4l2 backend is Linux-only; use --backend avfoundation"
            )
        from .v4l2 import V4L2Capture

        return V4L2Capture(path)
    if backend == "avfoundation":
        if sys.platform != "darwin":
            raise BackendError(
                "the avfoundation backend is macOS-only; use --backend v4l2"
            )
        from .avfoundation import AVFoundationCapture

        return AVFoundationCapture(path)
    if backend == "opencv":
        from .opencv import OpenCVCapture

        return OpenCVCapture(path)
    raise ValueError(f"unknown backend {backend!r}; available: {', '.join(BACKENDS)}")


def open_sink(path: str) -> FrameSink:
    """Instantiate the output sink.

    ``http://`` URLs serve MJPEG on the requested bind address.  Other
    paths are V4L2 loopback devices, which only exist on Linux.
    """
    if path.startswith("http://"):
        from .mjpeg import MjpegSink

        return MjpegSink(path)
    if sys.platform == "darwin":
        raise BackendError(
            "on macOS, --out accepts an http:// URL; omit --out for window-only "
            "mode"
        )
    from .v4l2 import V4L2Sink

    return V4L2Sink(path)
