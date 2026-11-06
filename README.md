# p2-viewer

A Linux and macOS viewer for the InfiRay P2 Pro thermal camera. It decodes
the camera's radiometric data, draws temperature annotations on top, and can
either show them in a window, publish the result as a virtual camera on Linux,
or serve an MJPEG stream over HTTP.

![example frame](docs/screenshot.png)

## Requirements

- Linux or macOS
- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management
- Linux: V4L2 and `v4l2loopback` for loopback output
- macOS: FFmpeg (`brew install ffmpeg`) for camera capture
- macOS: `fontconfig` (`brew install fontconfig`) for `fc-match` font
  resolution; without it labels fall back to Pillow's built-in font
- macOS: `libusb` (`brew install libusb`) only for vendor commands

On Linux the camera must be plugged in and visible as a V4L2 video node:

```sh
v4l2-ctl --list-devices
```

On macOS, list the AVFoundation device index:

```sh
ffmpeg -f avfoundation -list_devices true -i ""
```

The P2 Pro appears as `USB Camera`. Give the terminal or IDE that runs the
viewer access in System Settings -> Privacy & Security -> Camera. macOS does
not allow camera access from SSH sessions.

## Installation

```sh
git clone <repo-url> p2-viewer
cd p2-viewer

uv sync --extra qt --extra opencv --extra usb
```

Extras are optional so the base install stays small:

| Extra    | Provides                          |
| -------- | --------------------------------- |
| `qt`     | Display window (PySide6)          |
| `opencv` | OpenCV capture backend            |
| `usb`    | Vendor command support (PyUSB)    |
| `all`    | All of the above                  |

Without `qt`, use `--no-window` with an `--out` URL or Linux loopback device.

## Virtual camera output

To publish to a loopback device, load `v4l2loopback` with at least one device
and give it a label you will recognise:

```sh
sudo modprobe v4l2loopback video_nr=0,1,8 \
    card_label="obs-virtual-camera,thermal" devices=2
```

Then point the viewer at it (`--out /dev/video8` in the example above), and
at the camera node (`--cam /dev/video4` here; use whichever node
`v4l2-ctl --list-devices` reports):

```sh
uv run p2-viewer --cam /dev/video4 --out /dev/video8
```

Consume it with anything that reads V4L2:

```sh
ffplay -f v4l2 -input_format bgr3 -video_size 768x576 /dev/video8
```

If you use `exclusive_caps=1`, start the viewer *before* the consumer; the
device only advertises a capture format while something is writing to it.

## HTTP MJPEG output

Pass an `http://` URL to `--out` to make the viewer serve a multipart MJPEG
stream. The host and port in the URL are the local bind address:

```sh
uv run p2-viewer --cam 0 --out http://0.0.0.0:8080/stream.mjpg
```

Use `0.0.0.0` to listen on every interface, or a specific local address to
bind only one:

```sh
uv run p2-viewer --cam 0 --out http://192.168.1.50:8080/stream.mjpg
```

On Linux the same command works with the Linux camera node:

```sh
uv run p2-viewer --cam /dev/video4 --out http://0.0.0.0:8080/stream.mjpg
```

Use `--no-window` to serve without showing a window:

```sh
uv run p2-viewer --cam 0 --out http://0.0.0.0:8080/stream.mjpg --no-window
```

Consume the stream with a browser, VLC, FFmpeg, or an OBS Browser Source:

```sh
ffplay http://192.168.1.50:8080/stream.mjpg
```

JPEG frames are encoded at quality 96. There is no authentication: bind to
`0.0.0.0` only on trusted networks, or bind to a specific LAN address and use
a firewall.

## Usage

`--cam` is required: the viewer does not guess which device the camera is on.
On Linux use the V4L2 node, and on macOS the AVFoundation index or name.

```sh
# Linux
uv run p2-viewer --cam /dev/video4                               # window only
uv run p2-viewer --cam /dev/video4 --out /dev/video1             # window + virtual camera
uv run p2-viewer --cam /dev/video4 --out /dev/video1 --no-window # headless
uv run p2-viewer --cam /dev/video4 --palette rainbow
uv run p2-viewer --cam /dev/video4 --unit F

# macOS
uv run p2-viewer --cam 0                                         # window only
uv run p2-viewer --cam 0 --out http://0.0.0.0:8080/stream.mjpg  # window + MJPEG
uv run p2-viewer --cam 0 --out http://0.0.0.0:8080/stream.mjpg --no-window
uv run p2-viewer --cam 0 --palette rainbow
```

### Output modes

Showing a window is the default. Publishing is opt-in, so nothing is written
unless you name a device or URL with `--out`.

| Flags | Behaviour |
| ----- | --------- |
| `--cam` | Window only |
| `--cam --out` | Window **and** publish to `--out` |
| `--cam --out --no-window` | Publish only; useful for HTTP MJPEG, and for Linux loopback over SSH |
| `--cam --no-window` | Error, since nothing would be produced |

If the window cannot be opened (no display, or `qt` extra not installed) it
prints a warning and carries on publishing, or fails if there is no `--out`
either.

### Capture backends

| Backend        | Flag                      | Notes |
| -------------- | ------------------------- | ----- |
| `v4l2`         | `--backend v4l2`          | **Linux default.** Talks to the kernel directly (ioctl + mmap). Fastest, no extra dependencies. |
| `avfoundation` | `--backend avfoundation`  | **macOS default.** Uses FFmpeg's AVFoundation input, preserving raw YUYV. |
| `opencv`       | `--backend opencv`        | Uses `cv2.VideoCapture`. |

The OpenCV backend also accepts a bare index (`--cam 4`) instead of a node
path.

The output side is chosen by `--out`: a `/dev/video*` path uses the Linux
V4L2 writer, and an `http://` URL serves MJPEG on either platform.

### Options

| Option | Description |
| ------ | ----------- |
| `--cam PATH` | **Required.** Linux V4L2 node (`/dev/video4`) or macOS AVFoundation index/name (`0`) |
| `--out PATH_OR_URL` | Linux loopback device (`/dev/video1`) or MJPEG bind URL (`http://0.0.0.0:8080/stream.mjpg`). Omit for a window only |
| `--no-window` | Do not open a display window; needs `--out` |
| `--backend NAME` | Capture backend: `v4l2` or `opencv` on Linux; `avfoundation` or `opencv` on macOS |
| `--palette NAME` | `ironbow` (default), `rainbow`, `whitehot`, `blackhot`, `hotmetal`, `arctic` |
| `--unit {C,F,K}` | Temperature unit for labels (default `C`) |
| `--scale N` | Integer upscale factor (default `3`, so 256x192 becomes 768x576) |
| `--font NAME` | Font family or file path (default `monospace`, resolved via `fc-match`) |
| `--font-size PX` | Label font size in pixels (default `14`); markers and legend ticks scale with it |
| `--emissivity VALUE` | Apply a grey-body emissivity correction: a number 0-1, or `auto` to use the camera's own setting |
| `--reflected-temp K` | Background temperature for that correction, in kelvin, or `auto` (default `300`) |
| `--gain {high,low}` | Set the camera's measurement range. Off by default; needs USB write access |
| `--frames N` | Stop after N frames (0 = run forever) |
| `--csv PATH` | Sample the temperatures to `PATH` as CSV, once per second; an existing file is overwritten |

### Logging to CSV

`--csv PATH` samples the temperatures once per second, so a few minutes of
recording can be loaded straight into a spreadsheet or `pandas`:

```
timestamp,name,measurement,value
2026-11-05T20:38:33.614+00:00,min,1,21.98
2026-11-05T20:38:33.614+00:00,max,1,32.54
2026-11-05T20:38:33.614+00:00,center,1,22.85
2026-11-05T20:38:34.631+00:00,min,2,21.96
```

The file grows by about one group of rows per second, whatever the frame
rate. Each row holds the reading from the most recent frame.

| Column | Meaning |
| ------ | ------- |
| `timestamp` | ISO 8601 with a UTC offset and milliseconds |
| `name` | `min`, `max`, `center`, or a sample point `P1`..`P4` |
| `measurement` | Sample number, counted from 1; rows from one sample share it |
| `value` | Temperature in Celsius |

Values are always Celsius regardless of `--unit`, which only affects the
on-screen labels. They are written after any `--emissivity` correction, so
they match the numbers drawn on the image. `P1`..`Pn` rows appear only for points that are placed, and
are numbered by their position in the table, so removing a point shifts the
numbers of the ones after it. Points record the pixel under the marker, while
`min`/`max`/`center` are per-frame readings. A point placed between two samples
first appears in the next one.

All progress messages go to stderr.

### In the window

The image carries a centre crosshair, markers on the coldest and hottest
pixels, and a colour scale. On top of that the window is interactive:

| Action | Effect |
| ------ | ------ |
| Move the mouse | Temperature under the cursor, in a small readout |
| Left click | Drop a sample point, up to 4 at once |
| Right click | Remove the most recently placed point |
| `Space` or **Pause** | Freeze the image; click again to resume |
| `S` or **Snapshot** | Save the current frame as `p2pro-<timestamp>.png` in the working directory |
| `Escape` or close | Quit; `Ctrl-C` works too |

The panel in the top-left lists each sample point with its current, minimum,
maximum and average temperature since the point was placed. The `×` beside a
row removes that point, and the remaining points renumber themselves.

The `▸`/`▾` button at the end of the button row collapses the panel down to
just that row, which is useful when you want the image unobstructed. The table
only appears once at least one point is placed, so a fresh window starts
unobstructed and the toggle button is disabled until then. The glyph shows the
state: `▸` means the table is hidden, `▾` means it is showing.

The three buttons in the control strip are constant size and sit at 50%
opacity.

The hover readout and the point panel are drawn by the window only. Sample
points (`P1`..`P4`) are also drawn into frames published to `--out` while a
window is open and points exist, so V4L2 and HTTP consumers see the same
markers. Snapshots remain the rendered frame without the window overlay.
Running with `--no-window` has no click points to publish.

## Camera settings

The camera accepts vendor commands over USB for palette, emissivity, object
distance, atmospheric and reflected temperature, transmittance and gain. A
small CLI is included:

```sh
uv sync --extra usb
uv run p2-viewer-vendor info          # firmware, sensor, serial number
uv run p2-viewer-vendor show          # current settings
uv run p2-viewer-vendor palette --set 11
uv run p2-viewer-vendor emissivity --set 0.95
```

On Linux this needs access to the USB device node, which normally means a
udev rule:

```
# /etc/udev/rules.d/60-p2pro.rules
SUBSYSTEMS=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="5830", MODE="0666"
```

Then `sudo udevadm control --reload-rules`.

On macOS, install `libusb` (`brew install libusb`) so PyUSB has a backend.
No udev rule is needed.

## Notes on accuracy

Temperatures are the camera's own radiometric output converted with
`kelvin = raw / 64`.

**The camera's own settings have no practical effect.** Changing the
emissivity, object distance, reflected temperature, atmospheric temperature
or transmittance does not change the values in the video stream. They are
the apparent scene temperature, as if every surface were a black body, so
any correction has to be done in software.

It is accurate for high-emissivity surfaces such as skin or matte paint, and
wrong for shiny metal or glass, which reflect their surroundings instead of
radiating heat.

### Measurement range

The camera has two gain settings. **High gain** (the default) is more
sensitive but saturates at about 183 C. **Low gain** reaches much further,
to at least 437 C. Which one you want depends on what you are pointing at:

```sh
uv run p2-viewer --gain low
```

The viewer never changes this unless asked, so it does not need write access
to the USB device by default.

If you need the corrected value, pass an emissivity and the viewer will apply
a grey-body correction:

```sh
uv run p2-viewer --emissivity 0.95 --reflected-temp 294
uv run p2-viewer --emissivity auto --reflected-temp auto
```

`auto` reads the values back from the camera, so if the camera has an
emissivity set, the viewer follows it automatically. The viewer prints the
values it is using at startup.

The correction is off by default, so the viewer shows exactly what the
camera reports.

## How the camera protocol works

The P2 Pro is a plain UVC camera, so no special driver is needed. One of its
video modes carries extra data hidden in the image.

### Two views in one frame

The camera offers a 256x384 mode. That is twice the height of the sensor, and
the two halves are different things:

```
row   0 .. 191    the picture, in YUYV 4:2:2 colour
row 192 .. 383    the same scene, as raw temperature numbers
```

The **top half** is an ordinary image: the thermal scene rendered in whichever
false-colour palette the camera has selected. It shows how colours map to
heat, not actual values.

The **bottom half** is the useful part. It is not really image data despite
sharing the YUYV container: every pixel there is a **16-bit little-endian
integer**, one per sensor pixel, at exactly the same position as the pixel in
the top half. So `bottom[0][0]` is the temperature of the same spot as
`top[0][0]`.

### Turning the numbers into degrees

The raw values are fixed point with 6 fractional bits, in kelvin:

```python
raw    = bytes[0] | (bytes[1] << 8)      # little-endian u16
kelvin = raw / 64
celsius = kelvin - 273.15
```

Values in a normal room land around 19,000–20,000, which works out to roughly
22–25 °C.
