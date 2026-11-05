#!/usr/bin/env python3
"""InfiRay P2 Pro thermal viewer: capture, annotate, display and/or publish."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import backend, colormaps
from .calibration import DEFAULT_REFLECTED_TEMPERATURE, correct_celsius
from .device import find_camera
from .frame import CAM_HEIGHT, CAM_WIDTH, FrameStats, decode_radiometric, frame_stats
from .window import DisplayError, Window

# unit -> (scale, offset, symbol); value = celsius * scale + offset
UNITS = {
    "C": (1.0, 0.0, "\u00b0C"),
    "F": (9.0 / 5.0, 32.0, "\u00b0F"),
    "K": (1.0, 273.15, "K"),
}
DEFAULT_UNIT = "C"


# --- Rendering ----------------------------------------------------------------
# Markers are colour-coded: red = hottest, blue = coldest, green = centre.

def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    """Area of intersection of two (x0, y0, x1, y1) boxes."""
    dx = min(a[2], b[2]) - max(a[0], b[0])
    dy = min(a[3], b[3]) - max(a[1], b[1])
    if dx <= 0 or dy <= 0:
        return 0
    return dx * dy


def load_font(spec: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a font at `size`.

    `spec` may be a path to a font file, or a family name such as "monospace"
    or "DejaVu Sans Mono". Family names are resolved through fontconfig by
    shelling out to fc-match: Pillow's ImageFont.truetype() passes its argument
    straight to FreeType, which only understands paths, and driving fontconfig
    via ctypes means binding a lot of C API for very little gain.

    Falls back to Pillow's built-in bitmap font if nothing resolves, so a
    missing font degrades the labels rather than killing the stream.
    """
    path = spec
    if not Path(spec).is_file():
        try:
            path = subprocess.run(
                ["fc-match", "-f", "%{file}", spec],
                capture_output=True, text=True, check=True, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            path = ""
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


class Renderer:
    def __init__(
        self,
        scale: int,
        font: str = "monospace",
        palette: str = colormaps.DEFAULT_PALETTE,
        unit: str = DEFAULT_UNIT,
    ) -> None:
        self.scale = scale
        self.out_w = CAM_WIDTH * scale
        self.out_h = CAM_HEIGHT * scale
        self.lut = colormaps.build_lut(palette)
        self.unit = unit
        self.font = load_font(font, 14)
        self.font_small = load_font(font, 11)
        # legend geometry (kept here so render and layout agree)
        self.bar_w = 14
        self.legend_pad = 6          # gap between bar and its tick labels
        self.legend_right = 10       # margin from right edge
        self.legend_h = min(self.out_h - 80, 360)

    def _fmt(self, celsius: float, decimals: int = 1) -> str:
        """Format a Celsius value for display in the configured unit."""
        scale, offset, sym = UNITS[self.unit]
        return f"{celsius * scale + offset:.{decimals}f}{sym}"
        # legend geometry (set in __init__ so both render and layout agree)
        self.bar_w = 14
        self.legend_pad = 6          # gap between bar and its tick labels
        self.legend_right = 10       # margin from right edge
        self.legend_h = min(self.out_h - 80, 360)

    # -- layout helpers ------------------------------------------------------
    def _text_size(self, text: str, font) -> tuple[int, int]:
        left, top, right, bottom = font.getbbox(text)
        return right - left, bottom - top

    def _legend_geometry(self) -> tuple[int, int, int, int]:
        """Return (bar_x, bar_y, bar_w, bar_h)."""
        bar_x = self.out_w - self.legend_right - self.legend_pad - 34 - self.bar_w
        bar_y = (self.out_h - self.legend_h) // 2
        return bar_x, bar_y, self.bar_w, self.legend_h

    def _legend_obstacle(self) -> tuple[int, int, int, int]:
        """Box covering the legend bar plus its labels, for label avoidance."""
        bx, by, bw, bh = self._legend_geometry()
        return (bx - 6, by - 20, bx + bw + 34 + self.legend_pad, by + bh + 8)

    def _pick_label_pos(
        self, px: int, py: int, size: tuple[int, int], obstacles
    ) -> tuple[int, int, int]:
        """Choose a label position near (px, py) avoiding obstacles.

        Tries right, left, above, below in order, then falls back to the
        candidate with the least overlap. Returns (x, y, score) where score is
        the total overlapping area (0 == clean placement).
        """
        tw, th = size
        margin = 7
        candidates = [
            (px + margin, py - th // 2),          # right
            (px - margin - tw, py - th // 2),     # left
            (px - tw // 2, py - margin - th),     # above
            (px - tw // 2, py + margin),          # below
        ]
        best = None
        best_score = None
        for cx_, cy_ in candidates:
            x = min(max(cx_, 2), self.out_w - tw - 2)
            y = min(max(cy_, 2), self.out_h - th - 2)
            box = (x - 2, y - 2, x + tw + 2, y + th + 2)
            score = sum(_overlap(box, o) for o in obstacles)
            if score == 0:
                return x, y, 0
            if best_score is None or score < best_score:
                best_score = score
                best = (x, y, score)
        return best if best else (px + margin, py, 0)

    # -- main render ---------------------------------------------------------
    def render(self, temps: np.ndarray, stats: FrameStats) -> np.ndarray:
        """Render one annotated frame as an (h, w, 3) uint8 RGB array."""
        lo, hi = stats.t_min, stats.t_max
        span = max(hi - lo, 0.1)
        idx = ((temps - lo) * (255.0 / span)).clip(0, 255).astype(np.uint8)
        rgb = self.lut[idx]

        im = Image.fromarray(rgb, "RGB").resize(
            (self.out_w, self.out_h), Image.NEAREST
        )
        draw = ImageDraw.Draw(im)
        s = self.scale
        centre_colour = (0, 255, 0)

        cx, cy = self.out_w // 2, self.out_h // 2
        obstacles = [self._legend_obstacle()]

        # centre crosshair + reading
        draw.line([(cx - 10, cy), (cx + 10, cy)], fill=centre_colour, width=1)
        draw.line([(cx, cy - 10), (cx, cy + 10)], fill=centre_colour, width=1)
        centre_text = self._fmt(stats.t_center)
        csize = self._text_size(centre_text, self.font)
        cx_t, cy_t, _ = self._pick_label_pos(cx + 12, cy, csize, obstacles)
        draw.text(
            (cx_t, cy_t - 3), centre_text, fill=centre_colour, font=self.font,
            stroke_width=2, stroke_fill=(0, 0, 0),
        )
        obstacles.append(
            (cx_t - 2, cy_t - 5, cx_t + csize[0] + 2, cy_t + csize[1] + 1)
        )

        # min / max markers (draw max last so it wins any residual overlap)
        markers = [
            (stats.min_pos, self._fmt(stats.t_min), (120, 180, 255)),
            (stats.max_pos, self._fmt(stats.t_max), (255, 80, 80)),
        ]
        placed = []
        for pos, label, colour in markers:
            px, py = pos[0] * s + s // 2, pos[1] * s + s // 2
            placed.append((px, py, label, colour))

        # The legend is drawn first, then the markers go on top, so markers
        # always win the overlap with it.
        self._draw_legend(im, draw, stats)
        for px, py, label, colour in placed:
            size = self._text_size(label, self.font_small)
            tx, ty, _ = self._pick_label_pos(px, py, size, obstacles)
            draw.rectangle(
                [px - 4, py - 4, px + 4, py + 4], outline=colour, width=1
            )
            draw.line([(px, py), (tx if tx > px else tx + size[0], ty + size[1] // 2)],
                      fill=colour, width=1)
            draw.text((tx, ty), label, fill=colour, font=self.font_small,
                      stroke_width=2, stroke_fill=(0, 0, 0))

        return np.asarray(im, dtype=np.uint8)

    def _draw_legend(self, im, draw, stats: FrameStats) -> None:
        bx, by, bw, bh = self._legend_geometry()
        grad = np.linspace(255, 0, bh).astype(np.uint8)
        bar = self.lut[np.repeat(grad[:, None], bw, axis=1)]
        im.paste(Image.fromarray(bar, "RGB"), (bx, by))
        draw.rectangle([bx - 1, by - 1, bx + bw, by + bh], outline=(0, 0, 0))

        tick_font = self.font_small
        # temperatures the bar represents: top = max, bottom = min
        n_ticks = 5
        label_x = bx + bw + self.legend_pad
        for i in range(n_ticks):
            frac = i / (n_ticks - 1)               # 0 = hottest (top)
            temp = stats.t_max - frac * (stats.t_max - stats.t_min)
            tick_y = by + int(round(frac * (bh - 1)))
            # tick mark
            draw.line([(bx + bw, tick_y), (bx + bw + 3, tick_y)],
                      fill=(255, 255, 255), width=1)
            text = self._fmt(temp, decimals=0)
            tw, th = self._text_size(text, tick_font)
            ty = min(max(tick_y - th // 2, 0), self.out_h - th - 1)
            draw.text((label_x, ty), text, fill=(255, 255, 255), font=tick_font,
                      stroke_width=2, stroke_fill=(0, 0, 0))

        # unit caption, centred over the bar itself
        cap = UNITS[self.unit][2]
        cw, ch = self._text_size(cap, tick_font)
        bar_centre = bx + bw // 2
        draw.text((bar_centre - cw // 2, by - ch - 5), cap, fill=(200, 200, 200),
                  font=tick_font, stroke_width=2, stroke_fill=(0, 0, 0))


# --- Main ---------------------------------------------------------------------
def _resolve_setting(value: str, reader, name: str) -> float:
    """Resolve a numeric setting that may be the literal "auto".

    "auto" asks the camera for its current value; anything else is a number.
    """
    if value.strip().lower() != "auto":
        return float(value)
    try:
        from .vendor import VendorControl
    except ImportError:
        raise SystemExit(
            f"error: --{name} auto needs the usb extra (uv sync --extra usb)"
        )
    return reader(VendorControl())


def apply_gain(mode: str) -> int:
    """Set the camera's gain. Returns 0 on success.

    This is the only camera setting the viewer changes, and only when asked:
    it needs write access to the USB device node (a udev rule), and switching
    takes a couple of seconds while the camera recalibrates.
    """
    try:
        from .vendor import VendorControl, VendorError
    except ImportError:
        print(
            "error: --gain needs the usb extra (uv sync --extra usb)",
            file=sys.stderr,
        )
        return 1

    try:
        cam = VendorControl()
        cam.set_gain_select(mode == "high")
    except VendorError as exc:
        print(f"error: could not set gain: {exc}", file=sys.stderr)
        print(
            "       this needs write access to /dev/bus/usb/...; see the "
            "udev rule in the README",
            file=sys.stderr,
        )
        return 1

    print(f"gain: {'high' if cam.get_gain_select() else 'low'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stream annotated thermal video from an InfiRay P2 Pro "
                    "to a v4l2loopback device.",
    )
    ap.add_argument("--cam", default=None,
                    help="P2 Pro capture node (default: auto-detect)")
    ap.add_argument("--out", default=None,
                    help="v4l2loopback node to publish to "
                         "(default: /dev/video1)")
    ap.add_argument("--no-window", action="store_true",
                    help="do not open a display window")
    ap.add_argument("--no-sink", action="store_true",
                    help="do not publish to a v4l2loopback device")
    ap.add_argument("--scale", type=int, default=3, help="integer upscale factor")
    ap.add_argument("--font", default="monospace",
                    help="font family name or path (default: monospace, "
                         "resolved via fc-match)")
    ap.add_argument("--palette", default=colormaps.DEFAULT_PALETTE,
                    choices=colormaps.palette_names(),
                    help="false-colour palette (default: %(default)s)")
    ap.add_argument("--unit", default=DEFAULT_UNIT, choices=list(UNITS),
                    help="temperature unit for labels: C, F or K "
                         "(default: %(default)s)")
    ap.add_argument("--emissivity", default=None, metavar="VALUE",
                    help="apply a grey-body emissivity correction: a number "
                         "from 0 to 1, or 'auto' to use the camera's own "
                         "setting. The camera's own settings have no "
                         "practical effect, so use this to correct readings "
                         "yourself or to match an app that does")
    ap.add_argument("--reflected-temp", default=str(DEFAULT_REFLECTED_TEMPERATURE),
                    metavar="KELVIN",
                    help="background temperature for the correction, in "
                         "kelvin, or 'auto' to use the camera's own setting "
                         "(default: %(default)s)")
    ap.add_argument("--gain", default=None, choices=("high", "low"),
                    help="set the camera's measurement range: high gain for "
                         "sensitive readings up to about 183 C, low gain to "
                         "reach further. Off by default: the camera keeps "
                         "whatever it has, so no USB write access is needed")
    ap.add_argument("--backend", default=backend.DEFAULT_BACKEND,
                    choices=backend.BACKENDS,
                    help="capture backend (default: %(default)s)")
    ap.add_argument("--frames", type=int, default=0, help="0 = run forever")
    args = ap.parse_args()

    # Decide the outputs: by default both a window and the loopback sink.
    # Window is on by default; --no-window and --no-sink turn the ends off.
    use_sink = not args.no_sink
    use_window = not args.no_window
    out_path = args.out or "/dev/video1"
    if not use_sink and not use_window:
        ap.error("--no-sink and --no-window together leave no output")

    cam_path = args.cam or find_camera()
    if cam_path is None:
        print("error: no P2 Pro found; pass --cam", file=sys.stderr)
        return 1
    emissivity = None
    reflected_temp = DEFAULT_REFLECTED_TEMPERATURE
    if args.emissivity is not None:
        emissivity = _resolve_setting(
            args.emissivity, lambda cam: cam.get_emissivity(), "emissivity"
        )
        reflected_temp = _resolve_setting(
            args.reflected_temp,
            lambda cam: cam.get_reflected_temperature(),
            "reflected-temp",
        )
        if not 0 < emissivity <= 1:
            print("error: emissivity must be between 0 and 1", file=sys.stderr)
            return 1

    if args.gain is not None:
        if apply_gain(args.gain) != 0:
            return 1

    cam = backend.open_capture(args.backend, cam_path)
    sink = backend.open_sink(out_path) if use_sink else None

    renderer = Renderer(args.scale, args.font, args.palette, args.unit)
    out_w, out_h = renderer.out_w, renderer.out_h

    class Producer:
        """Pull one annotated frame; shared by the window and the sink loop."""

        def __init__(self) -> None:
            self.frames = 0
            self.done = False

        def grab(self):
            if self.done:
                return None
            buf = cam.read()
            if buf is None:
                return None
            temps = decode_radiometric(buf, cam.stride)
            if emissivity is not None:
                temps = correct_celsius(temps, emissivity, reflected_temp)

            rgb = renderer.render(temps, frame_stats(temps))
            if sink is not None:
                # loopback expects packed BGR
                sink.write(rgb[:, :, ::-1])
            self.frames += 1
            if args.frames and self.frames >= args.frames:
                self.done = True
            return rgb

    producer = Producer()
    window = None
    try:
        cam.open()
        print(f"capture {cam_path} via {args.backend} (stride {cam.stride})")
        if emissivity is None:
            print("temperatures: as reported by the camera (no correction)")
        else:
            print(
                f"temperatures: grey-body corrected, emissivity "
                f"{emissivity:.4f}, reflected {reflected_temp:.1f} K"
            )

        if sink is not None:
            sink.open(out_w, out_h)
            print(f"output  {out_path}: {out_w}x{out_h} BGR3")
        else:
            print("output  window only (no loopback)")

        if use_window:
            try:
                window = Window(f"P2 Pro {out_w}x{out_h}")
                window.open(out_w, out_h)
            except DisplayError as exc:
                # A missing display should not kill a sink-only pipeline.
                window = None
                print(f"warning: {exc}", file=sys.stderr)
                if not use_sink:
                    return 1

        if window is not None:
            # The window's Qt event loop drives capture.
            window.run(producer)
            if window.interrupted:
                print("\ninterrupted")
        else:
            # Headless: pump frames ourselves at the camera's own rate.
            # grab() returns None once the producer is done or the camera
            # stops delivering frames, so that is the only exit we need.
            while producer.grab() is not None:
                pass

        if args.frames:
            print(f"done after {producer.frames} frames")

    except KeyboardInterrupt:
        print("\ninterrupted")
    except DisplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        cam.close()
        if sink is not None:
            sink.close()
        if window is not None:
            window.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
