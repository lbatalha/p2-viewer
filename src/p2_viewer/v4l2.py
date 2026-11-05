"""V4L2 capture and v4l2loopback output, via raw ioctl/mmap."""

from __future__ import annotations

import fcntl
import mmap
import os
import struct

import numpy as np

from .frame import FRAME_HEIGHT, FRAME_STRIDE, FRAME_WIDTH

# --- V4L2 ioctl numbers -------------------------------------------------------
VIDIOC_QUERYCAP = 0x80685600
VIDIOC_G_FMT = 0xC0D05604
VIDIOC_S_FMT = 0xC0D05604
VIDIOC_REQBUFS = 0xC0145608
VIDIOC_QUERYBUF = 0xC0585609
VIDIOC_QBUF = 0xC058560F
VIDIOC_DQBUF = 0xC0585611
VIDIOC_STREAMON = 0x40045612
VIDIOC_STREAMOFF = 0x40045613

V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_BUF_TYPE_VIDEO_OUTPUT = 2
V4L2_MEMORY_MMAP = 1
V4L2_FIELD_NONE = 1

FMT_STRUCT_SIZE = 208  # sizeof(struct v4l2_format)

# struct v4l2_buffer field offsets, x86_64. Total size 88 bytes.
BUF_INDEX = 0
BUF_TYPE = 4
BUF_BYTESUSED = 8
BUF_SEQUENCE = 56
BUF_MEMORY = 60
BUF_OFFSET = 64
BUF_LENGTH = 72
BUF_STRUCT_SIZE = 88


def fourcc(code: str) -> int:
    """'YUYV' -> the little-endian int V4L2 expects."""
    return struct.unpack("<I", code.encode("ascii"))[0]


def fourcc_str(code: int) -> str:
    """Inverse of :func:`fourcc`."""
    return struct.pack("<I", code).decode("ascii", "replace")


class _V4L2Device:
    """Shared mmap/ioctl plumbing for capture and output nodes."""

    def __init__(self, path: str, buf_type: int, num_buffers: int = 4) -> None:
        self.path = path
        self.buf_type = buf_type
        self.num_buffers = num_buffers
        self.fd = -1
        self.maps: list[mmap.mmap] = []
        self.bytesperline = 0
        self.width = 0
        self.height = 0
        self.pixfmt = 0
        self.sizeimage = 0

    # -- low level -----------------------------------------------------------
    def _ioctl(self, request: int, arg, label: str) -> None:
        try:
            fcntl.ioctl(self.fd, request, arg, True)
        except OSError as exc:
            raise RuntimeError(f"{self.path}: {label} failed: {exc}") from exc

    def _ioctl_ret(self, request: int, arg, label: str) -> int:
        try:
            return fcntl.ioctl(self.fd, request, arg, True)
        except OSError as exc:
            raise RuntimeError(f"{self.path}: {label} failed: {exc}") from exc

    # -- setup ---------------------------------------------------------------
    def _open(self) -> None:
        self.fd = os.open(self.path, os.O_RDWR)

    def query_caps(self) -> dict:
        buf = bytearray(104)
        self._ioctl(VIDIOC_QUERYCAP, buf, "QUERYCAP")
        driver, card, bus = struct.unpack_from("<16s32s32s", buf, 0)
        caps, device_caps = struct.unpack_from("<II", buf, 84)
        return {
            "driver": driver.rstrip(b"\0").decode("utf-8", "replace"),
            "card": card.rstrip(b"\0").decode("utf-8", "replace"),
            "bus": bus.rstrip(b"\0").decode("utf-8", "replace"),
            "caps": caps,
            "device_caps": device_caps,
        }

    def set_format(self, width: int, height: int, pixfmt: str) -> None:
        buf = bytearray(FMT_STRUCT_SIZE)
        struct.pack_into("<I", buf, 0, self.buf_type)
        struct.pack_into(
            "<IIIIIIIIIIII",
            buf, 8,
            width, height, fourcc(pixfmt), V4L2_FIELD_NONE,
            0, 0, 0, 0, 0, 0, 0, 0,
        )
        self._ioctl(VIDIOC_S_FMT, buf, "S_FMT")
        self._parse_fmt(buf)

    def _parse_fmt(self, buf: bytearray) -> None:
        (self.width, self.height, self.pixfmt,
         _field, self.bytesperline, self.sizeimage) = struct.unpack_from(
            "<IIIIII", buf, 8
        )

    def _buf_template(self) -> bytearray:
        b = bytearray(BUF_STRUCT_SIZE)
        struct.pack_into("<I", b, BUF_TYPE, self.buf_type)
        struct.pack_into("<I", b, BUF_MEMORY, V4L2_MEMORY_MMAP)
        return b

    def _start_streaming(self) -> None:
        """Request, map, pre-queue buffers and start streaming."""
        buf = bytearray(20)
        struct.pack_into("<III", buf, 0, self.num_buffers, self.buf_type,
                         V4L2_MEMORY_MMAP)
        self._ioctl(VIDIOC_REQBUFS, buf, "REQBUFS")
        self.num_buffers = struct.unpack_from("<I", buf, 0)[0]

        for index in range(self.num_buffers):
            qb = self._buf_template()
            struct.pack_into("<I", qb, BUF_INDEX, index)
            self._ioctl(VIDIOC_QUERYBUF, qb, "QUERYBUF")
            length = struct.unpack_from("<I", qb, BUF_LENGTH)[0]
            offset = struct.unpack_from("<Q", qb, BUF_OFFSET)[0]
            self.maps.append(
                mmap.mmap(
                    self.fd, length, mmap.MAP_SHARED,
                    mmap.PROT_READ | mmap.PROT_WRITE, offset=offset,
                )
            )
        for index in range(self.num_buffers):
            self._queue(index)
        arg = bytearray(struct.pack("<I", self.buf_type))
        self._ioctl(VIDIOC_STREAMON, arg, "STREAMON")

    # -- buffer queue --------------------------------------------------------
    def _queue(self, index: int, bytesused: int = 0) -> None:
        qb = self._buf_template()
        struct.pack_into("<I", qb, BUF_INDEX, index)
        if bytesused:
            struct.pack_into("<I", qb, BUF_BYTESUSED, bytesused)
        self._ioctl(VIDIOC_QBUF, qb, "QBUF")

    def _dequeue(self) -> int:
        dq = self._buf_template()
        self._ioctl_ret(VIDIOC_DQBUF, dq, "DQBUF")
        return struct.unpack_from("<I", dq, BUF_INDEX)[0]

    # -- teardown ------------------------------------------------------------
    def close(self) -> None:
        if self.fd >= 0:
            try:
                arg = bytearray(struct.pack("<I", self.buf_type))
                fcntl.ioctl(self.fd, VIDIOC_STREAMOFF, arg, True)
            except OSError:
                pass
        for mm in self.maps:
            try:
                mm.close()
            except (BufferError, ValueError):
                pass
        self.maps.clear()
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


class V4L2Capture(_V4L2Device):
    """Capture source reading raw frames from a V4L2 video node."""

    def __init__(self, path: str, num_buffers: int = 4) -> None:
        super().__init__(path, V4L2_BUF_TYPE_VIDEO_CAPTURE, num_buffers)

    def open(self) -> None:
        self._open()
        self.set_format(FRAME_WIDTH, FRAME_HEIGHT, "YUYV")
        self._start_streaming()

    @property
    def stride(self) -> int:
        return self.bytesperline

    def read(self) -> bytes | None:
        index = self._dequeue()
        buf = bytes(self.maps[index])
        self._queue(index)
        return buf


class V4L2Sink(_V4L2Device):
    """Output sink writing BGR frames into a v4l2loopback device."""

    def __init__(self, path: str, num_buffers: int = 4) -> None:
        super().__init__(path, V4L2_BUF_TYPE_VIDEO_OUTPUT, num_buffers)

    def open(self, width: int, height: int) -> None:
        self._open()
        self.set_format(width, height, "BGR3")
        if (self.width, self.height) != (width, height):
            raise RuntimeError(
                f"{self.path}: negotiated {self.width}x{self.height}, "
                f"want {width}x{height}"
            )
        self._start_streaming()

    def write(self, frame: np.ndarray) -> None:
        """Write one packed BGR frame; `frame` must be (h, w, 3) uint8."""
        index = self._dequeue()
        self.maps[index][:] = frame.tobytes()
        self._queue(index, bytesused=frame.nbytes)


__all__ = [
    "V4L2Capture",
    "V4L2Sink",
    "fourcc",
    "fourcc_str",
    "FRAME_STRIDE",
]
