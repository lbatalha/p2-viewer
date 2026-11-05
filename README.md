# p2-viewer

A Linux viewer for the InfiRay P2 Pro thermal camera. It decodes the camera's
radiometric data, draws temperature annotations on top, and can either show
them in a window or publish the result as a virtual camera for OBS, Zoom,
Discord or anything else that consumes a webcam.

![example frame](docs/screenshot.png)

## Requirements

- Linux (the default `v4l2` backend depends on V4L2 and `v4l2loopback`)
- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management

The camera must be plugged in and visible as a V4L2 video node:

```sh
v4l2-ctl --list-devices
```

## Installation

```sh
git clone <repo-url> p2-viewer
cd p2-viewer

uv sync --extra qt              # window support (Qt/PySide6)
uv sync --extra qt --extra opencv   # plus the OpenCV capture backend
```

Extras are optional so the base install stays small:

| Extra    | Provides                          |
| -------- | --------------------------------- |
| `qt`     | Display window (PySide6)          |
| `opencv` | OpenCV capture backend            |
| `all`    | Both of the above                 |

Without `qt`, run with `--no-window`.

## Virtual camera output

To publish to a loopback device, load `v4l2loopback` with at least one device
and give it a label you will recognise:

```sh
sudo modprobe v4l2loopback video_nr=0,1,8 \
    card_label="obs-virtual-camera,thermal" devices=2
```

Then point the viewer at it (`--out /dev/video8` in the example above):

```sh
uv run p2-viewer --out /dev/video8
```

Consume it with anything that reads V4L2:

```sh
ffplay -f v4l2 -input_format bgr3 -video_size 768x576 /dev/video8
```

If you use `exclusive_caps=1`, start the viewer *before* the consumer; the
device only advertises a capture format while something is writing to it.

## Usage

```sh
uv run p2-viewer                      # window + virtual camera
uv run p2-viewer --no-sink            # window only
uv run p2-viewer --no-window          # virtual camera only (headless)
uv run p2-viewer --palette rainbow    # different colour map
uv run p2-viewer --unit F             # Fahrenheit
```

### Output modes

| Flags            | Behaviour                              |
| ---------------- | -------------------------------------- |
| *(none)*         | Window **and** publish to `--out`      |
| `--no-window`    | Publish only; safe over SSH            |
| `--no-sink`      | Window only; no virtual camera         |
| both             | Error, since nothing would be produced |

If the window cannot be opened (no display, or `qt` extra not installed) it
prints a warning and carries on publishing to the virtual camera.

### Capture backends

| Backend  | Flag                | Notes |
| -------- | ------------------- | ----- |
| `v4l2`   | `--backend v4l2`    | **Default.** Talks to the kernel directly (ioctl + mmap). Fastest, no extra dependencies, Linux only. |
| `opencv` | `--backend opencv`  | Uses `cv2.VideoCapture`. Exists for portability — OpenCV is how the camera is reached on macOS and Windows, which have no V4L2. Slower and pulls in a large dependency, so it is not the default on Linux. |

Both support `--cam` as either a device node (`/dev/video4`) or an index (`4`).

The output side always uses the V4L2 writer: OpenCV's `VideoWriter` cannot
reliably target a loopback device.

### Options

| Option | Description |
| ------ | ----------- |
| `--cam PATH` | Camera device (default `/dev/video4`) |
| `--out PATH` | Loopback device (default `/dev/video1`) |
| `--no-window` | Do not open a display window |
| `--no-sink` | Do not publish to a loopback device |
| `--backend {v4l2,opencv}` | Capture backend (default `v4l2`) |
| `--palette NAME` | `ironbow` (default), `rainbow`, `whitehot`, `blackhot`, `hotmetal`, `arctic` |
| `--unit {C,F,K}` | Temperature unit for labels (default `C`) |
| `--scale N` | Integer upscale factor (default `3`, so 256x192 becomes 768x576) |
| `--font NAME` | Font family or file path (default `monospace`, resolved via `fc-match`) |
| `--emissivity VALUE` | Apply a grey-body emissivity correction: a number 0-1, or `auto` to use the camera's own setting |
| `--reflected-temp K` | Background temperature for that correction, in kelvin, or `auto` (default `300`) |
| `--gain {high,low}` | Set the camera's measurement range. Off by default; needs USB write access |
| `--frames N` | Stop after N frames (0 = run forever) |

The window shows the frame with a centre crosshair, markers on the coldest and
hottest pixels, and a colour scale. Press `Escape` or close the window to
quit; `Ctrl-C` works too.

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

This needs access to the USB device node, which normally means a udev rule:

```
# /etc/udev/rules.d/60-p2pro.rules
SUBSYSTEMS=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="5830", MODE="0666"
```

Then `sudo udevadm control --reload-rules`.

## Notes on accuracy

Temperatures are the camera's own radiometric output converted with
`kelvin = raw / 64`.

**The camera's own settings have no practical effect.** Changing the
emissivity, object distance, reflected temperature, atmospheric temperature
or transmittance does not change the values in the video stream. They are
the apparent scene temperature, as if every surface were a black body, so
any correction has to be done in software.

That is accurate for high-emissivity surfaces such as skin or matte paint,
and badly wrong for shiny metal or glass, which reflect their surroundings
instead of radiating heat.

### Measurement range

The camera has two gain settings. **High gain** (the default) is more
sensitive but saturates at about 183 C. **Low gain** reaches much further -
the same target measured around 437 C in low gain while high gain pinned it
at 183 C. Which one you want depends on what you are pointing at:

```sh
uv run p2-viewer --gain low
```

The viewer never changes this unless asked, so it does not need write access
to the USB device by default. If you need the corrected value, pass an
emissivity and the viewer will apply a grey-body correction:

```sh
uv run p2-viewer --emissivity 0.95 --reflected-temp 294
uv run p2-viewer --emissivity auto --reflected-temp auto
```

`auto` reads the values back from the camera, so if the app has written an
emissivity into it, the viewer follows along automatically. The viewer prints
the values it is using at startup.

The correction is off by default, so the viewer shows exactly what the
camera reports. Whether the vendor app applies the same correction has not
been established; comparing the two on the same target is the way to find
out.

## How the camera protocol works

The P2 Pro is a plain UVC camera, so no special driver is needed. The trick is
that one of its video modes carries extra data hidden in the image.

### Two views in one frame

The camera offers a 256x384 mode. That is twice the height of the sensor, and
the two halves are different things:

```
row   0 .. 191    the picture, in YUYV 4:2:2 colour
row 192 .. 383    the same scene, as raw temperature numbers
```

The **top half** is an ordinary image: the thermal scene rendered in whichever
false-colour palette the camera has selected. You could display it as-is, but
it only tells you how colours map to heat, not actual values.

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

That is the whole decode. Values in a normal room land around 19,000–20,000,
which works out to roughly 22–25 °C.

Because every pixel has a real temperature, the viewer can take the minimum,
maximum and the pixel at the centre of the frame, and label them directly.

### What is not covered here

The camera also accepts vendor control commands over USB (endpoint 0) for
things like palette selection, emissivity, object distance and measurement
range. This viewer does not use them: it reads the default configuration and
only touches the video stream, so no special permissions or udev rules are
needed.

## License

MIT. See [LICENSE](LICENSE).
