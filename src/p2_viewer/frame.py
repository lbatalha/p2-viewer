"""Frame layout and radiometric decoding.

256x384 YUYV 4:2:2. Rows 0-191 are the pseudo-colour preview; rows 192-383
hold one little-endian u16 per pixel where kelvin = raw / 64."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Width of both halves, in pixels.
CAM_WIDTH = 256
#: Height of one half; a UVC frame is twice this tall.
CAM_HEIGHT = 192
#: Full capture frame dimensions.
FRAME_WIDTH = CAM_WIDTH
FRAME_HEIGHT = CAM_HEIGHT * 2
#: Bytes per row of a YUYV frame (2 bytes per pixel).
FRAME_STRIDE = CAM_WIDTH * 2
#: Total bytes in one frame.
FRAME_BYTES = FRAME_STRIDE * FRAME_HEIGHT

#: Raw values are 6-bit fixed point Kelvin.
RAW_SCALE = 64.0
KELVIN_OFFSET = 273.15


@dataclass
class FrameStats:
    """Per-frame temperature extremes and the centre reading (all Celsius)."""

    t_min: float
    t_max: float
    t_center: float
    min_pos: tuple[int, int]
    max_pos: tuple[int, int]


def decode_radiometric(buf: bytes, stride: int = FRAME_STRIDE) -> np.ndarray:
    """Decode the radiometric half of a frame into a Celsius temperature map.

    `stride` is the buffer's bytes-per-row as reported by the capture backend;
    V4L2 pads it to 512 for this device, but a backend is free to report
    anything >= ``CAM_WIDTH * 2``.
    """
    arr = np.frombuffer(buf, dtype=np.uint8, count=stride * FRAME_HEIGHT)
    arr = arr.reshape(FRAME_HEIGHT, stride)
    bottom = arr[CAM_HEIGHT:FRAME_HEIGHT, : CAM_WIDTH * 2].reshape(
        CAM_HEIGHT, CAM_WIDTH, 2
    )
    raw = bottom[:, :, 0].astype(np.uint16) | (bottom[:, :, 1].astype(np.uint16) << 8)
    return raw.astype(np.float32) / RAW_SCALE - KELVIN_OFFSET


def frame_stats(temps: np.ndarray) -> FrameStats:
    """Min/max/centre readings and the pixel positions of the extremes."""
    min_idx = int(np.argmin(temps))
    max_idx = int(np.argmax(temps))
    return FrameStats(
        t_min=float(temps.flat[min_idx]),
        t_max=float(temps.flat[max_idx]),
        t_center=float(temps[CAM_HEIGHT // 2, CAM_WIDTH // 2]),
        min_pos=(min_idx % CAM_WIDTH, min_idx // CAM_WIDTH),
        max_pos=(max_idx % CAM_WIDTH, max_idx // CAM_WIDTH),
    )
