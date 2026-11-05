# P2 Pro internals

How the camera works: the video format, the vendor command protocol, and how
to talk to it directly.

## 1. Video stream

The camera is a plain UVC device. Its 256x384 mode is twice the height of the
sensor because it carries two stacked images:

| Rows | Contents |
| ---- | -------- |
| 0-191 | pseudo-colour preview, YUYV 4:2:2 |
| 192-383 | radiometric data, one little-endian u16 per pixel |

Pixel `(x, y)` in the lower half corresponds to pixel `(x, y)` in the upper
half. The raw value is 6-bit fixed-point kelvin:

```
kelvin  = raw / 64
celsius = raw / 64 - 273.15
```

Room temperature lands around 19000-20000 raw, i.e. 22-25 C. The full u16
range would be 750 C.

The preview half is the camera's own rendered image. Setting the preview
palette changes only that half, never the radiometric values.

## 2. Vendor command transport

Configuration uses vendor control transfers on the default control endpoint,
independently of the video stream. Both work at the same time.

### Request template

| Direction | `bmRequestType` | `bRequest` | `wValue` |
| --------- | --------------- | ---------- | -------- |
| out | `0x41` | `0x45` | `0x0078` |
| in | `0xC1` | `0x44` | `0x0078` |

`wIndex` carries a **mailbox address**, not an interface number. Bit 15 of
the address selects an alternate bank, used when a payload follows the header.

### Mailboxes

| Address | Role |
| ------- | ---- |
| `0x0200` | status byte, read |
| `0x1D00` | command header, no payload (out) |
| `0x9D00` | command header, with payload (out) |
| `0x1D08` | data bank: read results and final payload bytes (in/out) |
| `0x9D08 + n` | bulk payload segments (out) |
| `0x1D10` | long-command result (in) |

### Status byte

One byte read from `0x0200`:

| Bits | Meaning |
| ---- | ------- |
| 0-1 | set while busy |
| 2-7 | non-zero means the command failed |

Poll until both busy bits are clear. Most commands finish in milliseconds,
but a gain switch takes about 2.6 s because the camera recalibrates, so the
timeout must be generous.

### Command header

Eight bytes, sent to `0x1D00` or `0x9D00`:

| Offset | Size | Field | Byte order |
| ------ | ---- | ----- | ---------- |
| 0 | 2 | command code | little endian |
| 2 | 4 | parameter | little endian |
| 6 | 2 | payload length | **big endian** |

The mixed byte order is intentional.

### The three transports

**Standard read.** Send the header to `0x1D00`, wait, then read `length`
bytes from `0x1D08`.

**Standard write.** Without a payload: send the header to `0x1D00` and wait.
With one: for each block of up to 256 bytes, send the header to `0x9D00` with
the parameter advanced by the block offset and the length set to the block
size, wait, then send the block in segments of at most 64 bytes. The bulk of
each block goes to `0x9D08 + offset`; the last up-to-8 bytes go to
`0x1D08 + offset`. Wait after the final segment.

**Long read / write.** Send a header to `0x9D00` where bytes 2-3 are the
first parameter and bytes 4-7 the second, both big endian. Then send eight
bytes to `0x1D08`: for a read, a zero word followed by the requested length;
for a write, the third and fourth parameters. Wait, then read the result from
`0x1D10`.

### GET and SET

The command code carries a direction bit. GET uses the base code; SET is
`base | 0x4000`. The palette command, for example, is `0x8409` to read and
`0xC409` to write.

## 3. Commands

| Code | Function | Transport |
| ---- | -------- | --------- |
| `0x8405` | device information | standard read |
| `0x8409` | preview palette | standard read / write |
| `0x840C` | shutter reference value | standard read |
| `0x8514` | temperature measurement parameters | long read / write |
| `0x8B0D` | current reference value | standard read |

### Device information (`0x8405`)

Standard read, parameter is the item index, fixed response length:

| Index | Item | Bytes |
| ----- | ---- | ----- |
| 0 | chip id | 8 (binary) |
| 1 | firmware date | 8 (text) |
| 2 | qualification | 8 (text) |
| 3 | IR sensor | 26 (text) |
| 4 | project | 4 (binary) |
| 5 | firmware version | 50 (text) |
| 6 | part number | 48 (text) |
| 7 | serial number | 16 (text) |
| 8 | sensor id | 4 (binary) |

Text items are NUL padded.

### Preview palette (`0x8409`)

One byte, GET or SET. The parameter selects the preview path; 0 is the
default.

| Id | Palette |
| -- | ------- |
| 1 | white hot |
| 3 | iron red |
| 4-6 | rainbow 1-3 |
| 7 | red hot |
| 8 | hot red |
| 9-10 | rainbow 4-5 |
| 11 | black hot |

This command sets the camera's *own* preview palette, which only changes the
bitmap in the top half of the frame. The viewer ignores that half and applies
its own colour ramp to the radiometric data, so this knob has no visible
effect; set it only if you care what the camera's built-in preview looks like.

### Temperature measurement parameters (`0x8514`)

Long read and long write. The first parameter is the index, the second the
value. Reads return two bytes, **big endian**.

| Index | Parameter | Scale | Range |
| ----- | --------- | ----- | ----- |
| 0 | object distance | 1/163.835 m | 0-32767 |
| 1 | reflected temperature | 1 K | 0-1024 |
| 2 | atmospheric temperature | 1 K | 0-1024 |
| 3 | emissivity | 1/129 | 0-128 |
| 4 | atmospheric transmittance | 1/129 | 0-128 |
| 5 | gain select | 0 = low, 1 = high | 0-1 |

Indices above 5 are not valid.

## 4. Behaviour

### Emissivity and the other parameters have no practical effect

Setting emissivity, object distance, reflected temperature, atmospheric
temperature or transmittance on the camera does not change the temperatures
it reports in the video stream. The settings are accepted and read back
correctly, but the values coming out of the camera stay the same.

So the stream carries the **apparent** temperature, treating every surface
as if it were a black body. Correcting for a real surface has to happen in
software, which is what `p2_viewer.calibration` does.

### Gain selects the measurement range

Gain does not change the values, only where the sensor saturates: high gain is
more sensitive and saturates at about 183 C, low gain reaches at least 437 C.
A gain switch takes about 2.6 s, during which the status byte reads busy.

## 5. Safety notes

### Do not claim the USB interface

`uvcvideo` owns both interfaces, and claiming them fails with `EBUSY`.
Control transfers work regardless, as long as the process can open
`/dev/bus/usb/...`.

### Unknown command codes wedge the camera

An unrecognised code makes the camera answer `Unrecognized command!` and then
stay permanently busy until the camera is power cycled. The video stream
keeps working throughout. This is why `VendorControl` refuses codes outside
its allowlist.

### Preview commands are destructive

`preview_stop` (`0x020F`) halts the radiometric pipeline. Afterwards the
temperature half of every frame reads a constant `0x8000` (238.85 C) until
the camera is power cycled. Do not send it.

## 6. Talking to the camera manually

Everything above can be done with plain `pyusb` and no part of this package.
The only requirement is permission to open the USB device node.

### Permissions

Add a udev rule so the device is writable without root:

```
# /etc/udev/rules.d/60-p2pro.rules
SUBSYSTEMS=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="5830", MODE="0666"
```

```sh
sudo udevadm control --reload-rules
```

Do not claim the interface: `uvcvideo` owns it and claiming fails.

### A minimal example

```python
import struct
import time

import usb.core

OUT, IN = 0x41, 0xC1          # request types
OUT_CODE, IN_CODE = 0x45, 0x44
VALUE = 0x0078
STATUS, HEADER, DATA, RESULT = 0x0200, 0x1D00, 0x1D08, 0x1D10

dev = usb.core.find(idVendor=0x0BDA, idProduct=0x5830)
if dev is None:
    raise SystemExit("camera not found")


def ready(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = dev.ctrl_transfer(IN, IN_CODE, VALUE, STATUS, 1)[0]
        if not state & 0x03:
            if state & 0xFC:
                raise RuntimeError(f"camera error 0x{state:02x}")
            return
        time.sleep(0.005)
    raise RuntimeError("camera never became ready")


def read(code, param=0, length=0):
    """Standard read."""
    header = struct.pack("<H", code) + struct.pack("<I", param) + struct.pack(">H", length)
    dev.ctrl_transfer(OUT, OUT_CODE, VALUE, HEADER, header)
    ready()
    return bytes(dev.ctrl_transfer(IN, IN_CODE, VALUE, DATA, length))


def read_long(code, index, length=2):
    """Long read, used by the measurement parameters."""
    header = struct.pack("<H", code) + struct.pack(">H", index) + struct.pack(">I", 0)
    dev.ctrl_transfer(OUT, OUT_CODE, VALUE, 0x9D00, header)
    dev.ctrl_transfer(OUT, OUT_CODE, VALUE, DATA,
                      struct.pack(">I", 0) + struct.pack(">I", length))
    ready()
    return bytes(dev.ctrl_transfer(IN, IN_CODE, VALUE, RESULT, length))


def write_long(code, index, value):
    """Long write, used by the measurement parameters."""
    header = struct.pack("<H", code) + struct.pack(">H", index) + struct.pack(">I", value)
    dev.ctrl_transfer(OUT, OUT_CODE, VALUE, 0x9D00, header)
    dev.ctrl_transfer(OUT, OUT_CODE, VALUE, DATA, struct.pack(">I", 0) * 2)
    ready()


# Firmware version is device info item 5, a 50-byte string.
print("firmware:", read(0x8405, 5, 50).split(b"\0")[0].decode())

# Emissivity is measurement parameter 3, scaled by 1/129.
print("emissivity:", int.from_bytes(read_long(0x8514, 3), "big") / 129)

# Write it back to 0.95 (SET is base | 0x4000), then restore the original.
original = int.from_bytes(read_long(0x8514, 3), "big")
write_long(0x8514 | 0x4000, 3, round(0.95 * 129))
print("now:", int.from_bytes(read_long(0x8514, 3), "big") / 129)
write_long(0x8514 | 0x4000, 3, original)
print("restored:", int.from_bytes(read_long(0x8514, 3), "big") / 129)
```

A standard write without a payload is just a header to `0x1D00` followed by
`ready()`, which is how the palette and the preview commands are sent.
