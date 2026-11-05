"""OpenCV capture backend, for platforms without V4L2 (macOS, Windows).

CAP_PROP_CONVERT_RGB must be 0: otherwise OpenCV converts YUYV to BGR and
gives 3 channels per pixel, yielding plausible-looking but wrong temperatures."""

from __future__ import annotations

import numpy as np

from .frame import FRAME_BYTES, FRAME_HEIGHT, FRAME_STRIDE, FRAME_WIDTH

try:
    import cv2
except ImportError:  # pragma: no cover - exercised only without the extra
    cv2 = None


class OpenCVError(RuntimeError):
    """Raised when the OpenCV backend cannot be used."""


class OpenCVCapture:
    """Capture source backed by ``cv2.VideoCapture``."""

    def __init__(self, path: str) -> None:
        if cv2 is None:
            raise OpenCVError(
                "OpenCV is not installed; install the optional extra "
                "(`uv add --optional opencv opencv-python`) or use "
                "--backend v4l2"
            )
        self.path = path
        self.cap = None
        self.stride = FRAME_STRIDE

    def open(self) -> None:
        # Accept both '/dev/video4' and a bare index like '4'.
        source = self.path
        if source.isdigit():
            source = int(source)

        self.cap = cv2.VideoCapture(source, cv2.CAP_V4L2)
        if not self.cap.isOpened():
            raise OpenCVError(f"could not open {self.path!r} via OpenCV")

        # Must be set before the first read, or OpenCV converts to BGR.
        self.cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        self.cap.set(
            cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUYV")
        )
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)

    def read(self) -> bytes | None:
        ok, frame = self.cap.read()
        if not ok or frame is None:
            return None
        # Some platforms wrap the array in a one-element list.
        if isinstance(frame, (list, tuple)):
            frame = frame[0]
        arr = np.asarray(frame)
        if arr.nbytes != FRAME_BYTES:
            raise OpenCVError(
                f"unexpected frame size {arr.shape} ({arr.nbytes} bytes), "
                f"expected {FRAME_BYTES}; CAP_PROP_CONVERT_RGB may not have "
                f"taken effect"
            )
        return arr.tobytes()

    def close(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None
