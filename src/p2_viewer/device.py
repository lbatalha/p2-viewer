"""Locating the camera on the system."""

from __future__ import annotations

from pathlib import Path

VENDOR_ID = 0x0BDA
PRODUCT_ID = 0x5830

VIDEO4LINUX = Path("/sys/class/video4linux")


def _usb_ids(node: Path) -> tuple[str, str] | None:
    """Return (idVendor, idProduct) for a video node, if it has a USB parent."""
    device = node / "device"
    if not device.exists():
        return None
    # The device link points at the interface; the USB device is its parent.
    try:
        parent = device.resolve().parent
    except OSError:
        return None
    vendor = parent / "idVendor"
    product = parent / "idProduct"
    if not (vendor.exists() and product.exists()):
        return None
    return vendor.read_text().strip(), product.read_text().strip()


def find_camera() -> str | None:
    """Return the device path of the P2 Pro's capture node, if present.

    A camera that has been re-plugged can leave stale nodes behind, so every
    candidate is opened and checked rather than taking the first match.
    """
    if not VIDEO4LINUX.is_dir():
        return None
    candidates = []
    for node in sorted(VIDEO4LINUX.iterdir()):
        ids = _usb_ids(node)
        if not ids:
            continue
        vendor, product = ids
        if vendor.lower() == f"{VENDOR_ID:04x}" and product.lower() == f"{PRODUCT_ID:04x}":
            candidates.append(f"/dev/{node.name}")

    for path in candidates:
        if _is_usable(path):
            return path
    return candidates[0] if candidates else None


def _is_usable(path: str) -> bool:
    """True if the node can be opened and offers a capture format.

    Reuses the V4L2 constants rather than re-deriving them: the ioctl number
    and the struct layout already live in :mod:`p2_viewer.v4l2`.
    """
    import os

    # Imported here so locating the camera stays cheap and does not pull in
    # the full V4L2 backend when it is not needed.
    from .v4l2 import FMT_STRUCT_SIZE, VIDIOC_G_FMT

    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        import fcntl
        import struct

        buf = bytearray(FMT_STRUCT_SIZE)
        struct.pack_into("<I", buf, 0, 1)  # V4L2_BUF_TYPE_VIDEO_CAPTURE
        fcntl.ioctl(fd, VIDIOC_G_FMT, buf, True)
        # width and height follow the type word, at the start of the union.
        width, height = struct.unpack_from("<II", buf, 8)
        return width > 0 and height > 0
    except OSError:
        return False
    finally:
        os.close(fd)
