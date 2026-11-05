"""Vendor control commands for the InfiRay P2 Pro.

The camera is a UVC device, but it also accepts a small mailbox protocol on
the USB control endpoint for configuration and telemetry.

Requests go to the *device* control endpoint with a vendor request type. The
``wIndex`` field carries a mailbox address rather than an interface number,
and bit 15 of that address selects an alternate bank used when a command
carries a payload.

Do not claim the USB interface: the kernel's ``uvcvideo`` driver already owns
it, and claiming fails with ``EBUSY``. Control transfers work regardless, as
long as the process can open the USB device node.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

try:
    import usb.core
except ImportError:  # pragma: no cover - only without the usb extra
    usb = None

VENDOR_ID = 0x0BDA
PRODUCT_ID = 0x5830

# bmRequestType: direction | type | recipient
REQUEST_OUT = 0x41  # host -> device, vendor, interface
REQUEST_IN = 0xC1   # device -> host, vendor, interface
REQUEST_OUT_CODE = 0x45
REQUEST_IN_CODE = 0x44
REQUEST_VALUE = 0x0078

# Mailbox addresses.
MAILBOX_STATUS = 0x0200
MAILBOX_HEADER_A = 0x1D00
MAILBOX_HEADER_B = 0x9D00
MAILBOX_DATA = 0x1D08
MAILBOX_BULK = 0x9D08
MAILBOX_LONG_RESULT = 0x1D10

STATUS_BUSY = 0x03
STATUS_ERROR = 0xFC

#: Command codes with the direction bit masked off.
CMD_DEVICE_INFO = 0x8405
CMD_PALETTE = 0x8409
CMD_SHUTTER_VTEMP = 0x840C
CMD_TPD = 0x8514
CMD_CURRENT_VTEMP = 0x8B0D

#: SET commands are the base code with the direction bit set.
SET_FLAG = 0x4000

#: Command codes this module is allowed to send.
#:
#: The camera answers an unrecognised command by going busy and never
#: recovering, which disables the command channel until it is power-cycled.
#: Unknown codes are therefore rejected before they are sent. The preview
#: commands are also deliberately absent: ``preview_stop`` halts the
#: radiometric pipeline, after which only a power cycle recovers it.
KNOWN_COMMANDS = frozenset(
    {
        CMD_DEVICE_INFO,
        CMD_PALETTE,
        CMD_SHUTTER_VTEMP,
        CMD_TPD,
        CMD_CURRENT_VTEMP,
    }
)

#: Largest payload in one block, and in one USB transfer.
BLOCK_SIZE = 256
SEGMENT_SIZE = 64

# Device info items and their fixed response lengths.
DEVICE_INFO_ITEMS = {
    "chip_id": (0, 8),
    "firmware_date": (1, 8),
    "qualification": (2, 8),
    "ir_sensor": (3, 26),
    "project": (4, 4),
    "firmware_version": (5, 50),
    "part_number": (6, 48),
    "serial_number": (7, 16),
    "sensor_id": (8, 4),
}

# Temperature measurement parameters.
TPD_DISTANCE = 0
TPD_REFLECTED_TEMPERATURE = 1
TPD_ATMOSPHERIC_TEMPERATURE = 2
TPD_EMISSIVITY = 3
TPD_TRANSMITTANCE = 4
TPD_GAIN_SELECT = 5

# Palette identifiers understood by the camera's preview half.
#
# This is unrelated to p2_viewer.colormaps: those are the palettes the viewer
# renders itself. These ids only change the camera's own preview image, which
# the viewer does not display, so they are rarely useful.
PALETTES = {
    1: "whitehot",
    3: "ironred",
    4: "rainbow1",
    5: "rainbow2",
    6: "rainbow3",
    7: "redhot",
    8: "hotred",
    9: "rainbow4",
    10: "rainbow5",
    11: "blackhot",
}


class VendorError(RuntimeError):
    """Raised when the camera rejects or fails a vendor command."""


@dataclass
class DeviceInfo:
    """Decoded device information.

    The text fields are NUL-padded strings; ``chip_id`` and ``sensor_id`` are
    binary and are kept as raw bytes.
    """

    chip_id: bytes = b""
    firmware_date: str = ""
    qualification: str = ""
    ir_sensor: str = ""
    project: bytes = b""
    firmware_version: str = ""
    part_number: str = ""
    serial_number: str = ""
    sensor_id: bytes = b""

    def __str__(self) -> str:
        return (
            f"{self.firmware_version} (built {self.firmware_date}), "
            f"sensor {self.ir_sensor}, serial {self.serial_number}"
        )


def _decode_text(raw: bytes) -> str:
    """Decode a NUL-padded fixed-width string."""
    return raw.split(b"\0")[0].decode("ascii", "replace")


class VendorControl:
    """Mailbox-protocol client for the camera's vendor commands."""

    def __init__(self, device=None, timeout: float = 2.0) -> None:
        if usb is None:
            raise VendorError(
                "pyusb is not installed; install the usb extra "
                "(`uv sync --extra usb`)"
            )
        self.timeout = timeout
        self.device = device
        if self.device is None:
            self.device = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
            if self.device is None:
                raise VendorError(
                    f"P2 Pro not found (looking for {VENDOR_ID:04x}:{PRODUCT_ID:04x})"
                )

    # -- transport -----------------------------------------------------------
    def _out(self, mailbox: int, data: bytes) -> None:
        self.device.ctrl_transfer(
            REQUEST_OUT, REQUEST_OUT_CODE, REQUEST_VALUE, mailbox, data
        )

    def _in(self, mailbox: int, length: int) -> bytes:
        return bytes(
            self.device.ctrl_transfer(
                REQUEST_IN, REQUEST_IN_CODE, REQUEST_VALUE, mailbox, length
            )
        )

    def status(self) -> int:
        """Read the command channel status byte."""
        return self._in(MAILBOX_STATUS, 1)[0]

    def wait_ready(self) -> None:
        """Poll the status byte until the camera is ready for the next command."""
        deadline = time.perf_counter() + self.timeout
        while time.perf_counter() < deadline:
            state = self.status()
            if state & STATUS_BUSY == 0:
                if state & STATUS_ERROR:
                    raise VendorError(f"camera reported error status 0x{state:02x}")
                return
            time.sleep(0.001)
        raise VendorError(
            "camera did not become ready in time. If the status byte is "
            "stuck at 0x01 the command channel is wedged and only a power "
            "cycle (unplug the camera) will recover it."
        )

    @staticmethod
    def _check_known(code: int) -> None:
        """Refuse command codes that are not on the allowlist."""
        # SET commands are base | 0x4000, so compare the base code, which is
        # the command code with the set bit cleared.
        base = code & ~SET_FLAG
        if base not in KNOWN_COMMANDS:
            raise VendorError(
                f"refusing to send unknown command 0x{code:04x}; the camera "
                f"stops responding to unknown codes until it is power-cycled"
            )

    @staticmethod
    def _header(code: int, param: int, length: int) -> bytes:
        """Build the 8-byte command header: code (LE), parameter (LE), length (BE)."""
        return (
            struct.pack("<H", code)
            + struct.pack("<I", param)
            + struct.pack(">H", length)
        )

    # -- command forms -------------------------------------------------------
    def standard_read(self, code: int, param: int = 0, length: int = 0) -> bytes:
        """Read `length` bytes using the standard-read transport."""
        self._check_known(code)
        result = bytearray()
        offset = 0
        while offset < length:
            size = min(length - offset, BLOCK_SIZE)
            self._out(
                MAILBOX_HEADER_A,
                self._header(code, param + offset, size),
            )
            self.wait_ready()
            result += self._in(MAILBOX_DATA, size)
            offset += size
        return bytes(result)

    def standard_write(self, code: int, param: int = 0, payload: bytes = b"") -> None:
        """Write a command, optionally with a payload."""
        self._check_known(code)
        if not payload:
            self._out(MAILBOX_HEADER_A, self._header(code, param, 0))
            self.wait_ready()
            return

        offset = 0
        while offset < len(payload):
            block = payload[offset : offset + BLOCK_SIZE]
            self._out(
                MAILBOX_HEADER_B,
                self._header(code, param + offset, len(block)),
            )
            self.wait_ready()
            self._write_block(block)
            offset += len(block)

    def _write_block(self, block: bytes) -> None:
        """Send one block as a sequence of <=64 byte segments."""
        pos = 0
        while pos < len(block):
            remaining = len(block) - pos
            if remaining > SEGMENT_SIZE:
                self._out(MAILBOX_BULK + pos, block[pos : pos + SEGMENT_SIZE])
                pos += SEGMENT_SIZE
                continue
            if remaining > 8:
                self._out(MAILBOX_BULK + pos, block[pos : pos + remaining - 8])
                pos += remaining - 8
            self._out(MAILBOX_DATA + pos, block[pos : pos + 8])
            pos += 8
        self.wait_ready()

    def long_read(self, code: int, p1: int, p2: int = 0, length: int = 0) -> bytes:
        """Read using the long-command transport."""
        self._check_known(code)
        header = (
            struct.pack("<H", code)
            + struct.pack(">H", p1)
            + struct.pack(">I", p2)
        )
        self._out(MAILBOX_HEADER_B, header)
        self._out(MAILBOX_DATA, struct.pack(">I", 0) + struct.pack(">I", length))
        self.wait_ready()
        return self._in(MAILBOX_LONG_RESULT, length)

    def long_write(self, code: int, p1: int, p2: int, p3: int = 0, p4: int = 0) -> None:
        """Write using the long-command transport."""
        self._check_known(code)
        header = (
            struct.pack("<H", code)
            + struct.pack(">H", p1)
            + struct.pack(">I", p2)
        )
        self._out(MAILBOX_HEADER_B, header)
        self._out(MAILBOX_DATA, struct.pack(">I", p3) + struct.pack(">I", p4))
        self.wait_ready()

    # -- high level ----------------------------------------------------------
    def device_info(self) -> DeviceInfo:
        """Read all device information items."""
        info = DeviceInfo()
        for name, (item, length) in DEVICE_INFO_ITEMS.items():
            raw = self.standard_read(CMD_DEVICE_INFO, item, length)
            if name in ("chip_id", "project", "sensor_id"):
                setattr(info, name, raw)
            else:
                setattr(info, name, _decode_text(raw))
        return info

    def get_palette(self, preview: int = 0) -> int:
        """Get the palette id used by the camera's preview."""
        return self.standard_read(CMD_PALETTE, preview, 1)[0]

    def set_palette(self, palette_id: int, preview: int = 0) -> None:
        """Set the palette used by the camera's preview."""
        self.standard_write(
            CMD_PALETTE | SET_FLAG, preview, bytes([palette_id])
        )

    def get_tpd(self, parameter: int) -> int:
        """Read a temperature measurement parameter."""
        return int.from_bytes(self.long_read(CMD_TPD, parameter, 0, 2), "big")

    def set_tpd(self, parameter: int, value: int, timeout: float | None = None) -> None:
        """Write a temperature measurement parameter.

        Some parameters take effect slowly: switching the gain re-runs the
        sensor calibration and takes a couple of seconds to complete, longer
        than the default command timeout.
        """
        if timeout is not None:
            previous = self.timeout
            self.timeout = timeout
            try:
                self.long_write(CMD_TPD | SET_FLAG, parameter, value)
            finally:
                self.timeout = previous
        else:
            self.long_write(CMD_TPD | SET_FLAG, parameter, value)

    def get_emissivity(self) -> float:
        """Object emissivity, 0.0 to 1.0."""
        return self.get_tpd(TPD_EMISSIVITY) / 129.0

    def set_emissivity(self, value: float) -> None:
        self.set_tpd(TPD_EMISSIVITY, min(128, max(0, round(value * 129))))

    def get_transmittance(self) -> float:
        """Atmospheric transmission coefficient, 0.0 to 1.0."""
        return self.get_tpd(TPD_TRANSMITTANCE) / 129.0

    def get_gain_select(self) -> int:
        """0 = low gain, 1 = high gain."""
        return self.get_tpd(TPD_GAIN_SELECT)

    def set_gain_select(self, high: bool) -> None:
        """Select the measurement range: high gain is sensitive, low gain covers
        a wider temperature span. Switching takes a few seconds."""
        self.set_tpd(TPD_GAIN_SELECT, 1 if high else 0, timeout=30.0)

    def get_atmospheric_temperature(self) -> float:
        """Atmospheric temperature in kelvin."""
        return float(self.get_tpd(TPD_ATMOSPHERIC_TEMPERATURE))

    def get_reflected_temperature(self) -> float:
        """Apparent reflected background temperature in kelvin."""
        return float(self.get_tpd(TPD_REFLECTED_TEMPERATURE))

    def get_distance(self) -> float:
        """Object distance in metres."""
        return self.get_tpd(TPD_DISTANCE) / 163.835

    def set_distance(self, metres: float) -> None:
        self.set_tpd(TPD_DISTANCE, min(32767, max(0, round(metres * 163.835))))

    def current_vtemp(self) -> int:
        """Raw current reference temperature value."""
        return int.from_bytes(self.standard_read(CMD_CURRENT_VTEMP, 0, 2), "big")

    def shutter_vtemp(self) -> int:
        """Raw shutter reference temperature value."""
        return int.from_bytes(self.standard_read(CMD_SHUTTER_VTEMP, 0, 2), "big")
