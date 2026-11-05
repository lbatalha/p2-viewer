"""Backend protocols and selection."""

from __future__ import annotations

from typing import Protocol

import numpy as np


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


BACKENDS = ("v4l2", "opencv")
DEFAULT_BACKEND = "v4l2"


def open_capture(backend: str, path: str) -> CaptureSource:
    """Instantiate a capture backend by name."""
    if backend == "v4l2":
        from .v4l2 import V4L2Capture

        return V4L2Capture(path)
    if backend == "opencv":
        from .opencv import OpenCVCapture

        return OpenCVCapture(path)
    raise ValueError(f"unknown backend {backend!r}; available: {', '.join(BACKENDS)}")


def open_sink(path: str) -> FrameSink:
    """Instantiate the output sink.

    Only V4L2 output is implemented: it is the sole reliable way to publish
    frames to a loopback device on Linux.
    """
    from .v4l2 import V4L2Sink

    return V4L2Sink(path)
