#!/usr/bin/env python3
"""InfiRay P2 Pro thermal viewer: capture, annotate, display and/or publish."""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import backend, colormaps
from .calibration import DEFAULT_REFLECTED_TEMPERATURE, correct_celsius
from .frame import CAM_HEIGHT, CAM_WIDTH, FrameStats, decode_radiometric, frame_stats
from .overlay import LegendItem, LineItem, OverlayLayout, RectItem, TextItem
from .window import DisplayError, Frame, Window

# unit -> (scale, offset, symbol); value = celsius * scale + offset
UNITS = {
    "C": (1.0, 0.0, "\u00b0C"),
    "F": (9.0 / 5.0, 32.0, "\u00b0F"),
    "K": (1.0, 273.15, "K"),
}
DEFAULT_UNIT = "C"


#: Outline drawn around label text, so it stays readable over any colour.
LABEL_STROKE = 2

#: Outline drawn around sample-point shapes, in image pixels.
MARKER_OUTLINE = 1

#: Default font size for the main labels.
DEFAULT_FONT_SIZE = 14

#: Gap between the legend's unit caption and the top of the colour bar.
LEGEND_CAPTION_GAP = 5

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
    or "DejaVu Sans Mono". Family names are resolved through fontconfig with
    ``fc-match``. If nothing resolves, labels use Pillow's built-in bitmap font.
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
        font_size: int = DEFAULT_FONT_SIZE,
    ) -> None:
        self.scale = scale
        self.out_w = CAM_WIDTH * scale
        self.out_h = CAM_HEIGHT * scale
        self.lut = colormaps.build_lut(palette)
        self.unit = unit
        self.font_spec = font
        self.font_size = font_size
        self.font = load_font(font, font_size)
        # Legend ticks are slightly smaller than the main labels.
        self.small_font_size = max(7, round(font_size * 0.8))
        self.font_small = load_font(font, self.small_font_size)
        # legend geometry (kept here so render and layout agree)
        self.bar_w = 14
        self.legend_pad = 6          # gap between bar and its tick labels
        self.legend_right = 10       # margin from right edge
        self.legend_h = min(self.out_h - 80, 360)
        # Widest tick label the legend can show, so the gutter fits whatever
        # font size was asked for.
        self.legend_text_w = self._text_size("-888\u00b0", self.font_small)[0]
        # The bar itself never changes, so build it once for either painter.
        grad = np.linspace(255, 0, self.legend_h).astype(np.uint8)
        self.legend_bar = self.lut[
            np.repeat(grad[:, None], self.bar_w, axis=1)
        ]

    def format_temp(self, celsius: float, decimals: int = 1) -> str:
        """Format a Celsius value for display in the configured unit."""
        scale, offset, sym = UNITS[self.unit]
        return f"{celsius * scale + offset:.{decimals}f}{sym}"

    # -- layout helpers ------------------------------------------------------
    def _text_size(self, text: str, font) -> tuple[int, int]:
        left, top, right, bottom = font.getbbox(text)
        return right - left, bottom - top

    def _text_ink_bottom(self, text: str, font) -> int:
        """How far the ink reaches below the drawing origin.

        The y passed to ``draw.text`` is the top of the font's box, not of the
        ink, so a caption positioned from the box height alone can end up
        touching whatever sits underneath it.
        """
        return font.getbbox(text)[3]

    def _legend_geometry(self) -> tuple[int, int, int, int]:
        """Return (bar_x, bar_y, bar_w, bar_h)."""
        bar_x = (
            self.out_w - self.legend_right - self.legend_pad
            - self.legend_text_w - self.bar_w
        )
        bar_y = (self.out_h - self.legend_h) // 2
        return bar_x, bar_y, self.bar_w, self.legend_h

    def _legend_obstacle(self) -> tuple[int, int, int, int]:
        """Box covering the legend bar plus its labels, for label avoidance."""
        bx, by, bw, bh = self._legend_geometry()
        top = by - self._text_ink_bottom("\u00b0C", self.font_small) - 2 * LABEL_STROKE - 8
        return (
            bx - 6,
            top,
            bx + bw + self.legend_text_w + self.legend_pad + 4,
            by + bh + 8,
        )

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
        # The labels are drawn with a 2px outline, which extends past the
        # glyph box, so keep the whole outline inside the frame.
        inset = LABEL_STROKE + 1
        for cx_, cy_ in candidates:
            x = min(max(cx_, inset), self.out_w - tw - inset)
            y = min(max(cy_, inset), self.out_h - th - inset)
            box = (x - 2, y - 2, x + tw + 2, y + th + 2)
            score = sum(_overlap(box, o) for o in obstacles)
            if score == 0:
                return x, y, 0
            if best_score is None or score < best_score:
                best_score = score
                best = (x, y, score)
        return best if best else (px + margin, py, 0)

    # -- main render ---------------------------------------------------------
    def render_base(self, temps: np.ndarray, stats: FrameStats) -> np.ndarray:
        """Render the thermal image without any annotations."""
        lo, hi = stats.t_min, stats.t_max
        span = max(hi - lo, 0.1)
        idx = ((temps - lo) * (255.0 / span)).clip(0, 255).astype(np.uint8)
        rgb = self.lut[idx]
        im = Image.fromarray(rgb, "RGB").resize(
            (self.out_w, self.out_h), Image.NEAREST
        )
        return np.asarray(im, dtype=np.uint8)

    def _legend_layout(self, stats: FrameStats) -> LegendItem:
        bx, by, bw, bh = self._legend_geometry()
        n_ticks = 5
        label_x = bx + bw + self.legend_pad
        ticks = []
        labels = []
        for i in range(n_ticks):
            frac = i / (n_ticks - 1)               # 0 = hottest (top)
            temp = stats.t_max - frac * (stats.t_max - stats.t_min)
            tick_y = by + int(round(frac * (bh - 1)))
            ticks.append(LineItem(
                bx + bw, tick_y, bx + bw + 3, tick_y, (255, 255, 255),
            ))
            text = self.format_temp(temp, decimals=0)
            _tw, th = self._text_size(text, self.font_small)
            ty = min(max(tick_y - th // 2, 0), self.out_h - th - 1)
            labels.append(TextItem(
                text, label_x, ty, (255, 255, 255), small=True,
            ))

        cap = UNITS[self.unit][2]
        cw, _ = self._text_size(cap, self.font_small)
        cap_y = (
            by - LEGEND_CAPTION_GAP - LABEL_STROKE
            - self._text_ink_bottom(cap, self.font_small)
        )
        bar_centre = bx + bw // 2
        caption = TextItem(
            cap, bar_centre - cw // 2,
            max(LABEL_STROKE + 1, cap_y), (200, 200, 200), small=True,
        )
        return LegendItem(
            bx, by, bw, bh, self.legend_bar,
            RectItem(bx - 1, by - 1, bx + bw, by + bh, (0, 0, 0)),
            tuple(ticks), tuple(labels), caption,
        )

    def overlay(self, stats: FrameStats) -> OverlayLayout:
        """Build the annotation layout in frame coordinates."""
        s = self.scale
        centre_colour = (0, 255, 0)
        cx, cy = self.out_w // 2, self.out_h // 2
        obstacles = [self._legend_obstacle()]

        # centre crosshair + reading
        lines = [
            LineItem(cx - 10, cy, cx + 10, cy, centre_colour),
            LineItem(cx, cy - 10, cx, cy + 10, centre_colour),
        ]
        centre_text = self.format_temp(stats.t_center)
        csize = self._text_size(centre_text, self.font)
        cx_t, cy_t, _ = self._pick_label_pos(cx + 12, cy, csize, obstacles)
        labels = [TextItem(centre_text, cx_t, cy_t - 3, centre_colour)]
        obstacles.append(
            (cx_t - 2, cy_t - 5, cx_t + csize[0] + 2, cy_t + csize[1] + 1)
        )

        # min / max markers (max comes last so it wins any residual overlap)
        markers = [
            (stats.min_pos, self.format_temp(stats.t_min), (120, 180, 255)),
            (stats.max_pos, self.format_temp(stats.t_max), (255, 80, 80)),
        ]
        rects = []
        for pos, label, colour in markers:
            px, py = pos[0] * s + s // 2, pos[1] * s + s // 2
            size = self._text_size(label, self.font_small)
            tx, ty, _ = self._pick_label_pos(px, py, size, obstacles)
            rects.append(RectItem(px - 4, py - 4, px + 4, py + 4, colour))
            lines.append(LineItem(
                px, py,
                tx if tx > px else tx + size[0],
                ty + size[1] // 2,
                colour,
            ))
            labels.append(TextItem(label, tx, ty, colour, small=True))

        return OverlayLayout(
            self._legend_layout(stats), tuple(lines), tuple(rects), tuple(labels)
        )

    def annotate(self, base: np.ndarray, layout: OverlayLayout) -> np.ndarray:
        """Draw `layout` onto a copy of `base` and return the RGB array."""
        im = Image.fromarray(base, "RGB").copy()
        draw = ImageDraw.Draw(im)

        legend = layout.legend
        im.paste(Image.fromarray(legend.pixels, "RGB"), (legend.x, legend.y))
        self._draw_rect(draw, legend.border)
        for tick in legend.ticks:
            self._draw_line(draw, tick)
        for text in legend.labels:
            self._draw_text(draw, text)
        self._draw_text(draw, legend.caption)

        for rect in layout.rects:
            self._draw_rect(draw, rect)
        for line in layout.lines:
            self._draw_line(draw, line)
        for text in layout.labels:
            self._draw_text(draw, text)

        return np.asarray(im, dtype=np.uint8)

    def render(self, temps: np.ndarray, stats: FrameStats) -> np.ndarray:
        """Render one annotated frame as an (h, w, 3) uint8 RGB array."""
        base = self.render_base(temps, stats)
        return self.annotate(base, self.overlay(stats))

    def _draw_line(self, draw, line: LineItem) -> None:
        draw.line(
            [(line.x0, line.y0), (line.x1, line.y1)],
            fill=line.colour, width=line.width,
        )

    def _draw_rect(self, draw, rect: RectItem) -> None:
        draw.rectangle(
            [rect.x0, rect.y0, rect.x1, rect.y1],
            outline=rect.colour, width=rect.width,
        )

    def _draw_text(self, draw, text: TextItem) -> None:
        font = self.font_small if text.small else self.font
        draw.text(
            (text.x, text.y), text.text, fill=text.colour, font=font,
            stroke_width=LABEL_STROKE, stroke_fill=(0, 0, 0),
        )

    def render_points(self, rgb: np.ndarray, points, temps: np.ndarray) -> np.ndarray:
        """Draw sample-point markers on `rgb` and return the result.

        Returns `rgb` unchanged when `points` is empty.
        """
        if not points:
            return rgb

        im = Image.fromarray(rgb, "RGB").copy()
        draw = ImageDraw.Draw(im)
        s = self.scale
        radius = 5
        arm = 9
        outline = MARKER_OUTLINE

        for index, point in enumerate(points):
            px = point.x * s + s // 2
            py = point.y * s + s // 2
            colour = point.colour
            # Draw the black outline first, expanded by its width, then the
            # coloured shape on top so the outline stays outside it.
            draw.ellipse(
                [px - radius - outline, py - radius - outline,
                 px + radius + outline - 1, py + radius + outline - 1],
                outline=(0, 0, 0),
                width=outline + 1,
            )
            draw.line([(px - arm, py), (px + arm, py)],
                      fill=(0, 0, 0), width=1 + 2 * outline)
            draw.line([(px, py - arm), (px, py + arm)],
                      fill=(0, 0, 0), width=1 + 2 * outline)
            draw.ellipse(
                [px - radius, py - radius,
                 px + radius - 1, py + radius - 1],
                outline=colour,
                width=1,
            )
            draw.line([(px - arm, py), (px + arm, py)],
                      fill=colour, width=1)
            draw.line([(px, py - arm), (px, py + arm)],
                      fill=colour, width=1)
            label = (
                f"P{index + 1} "
                f"{self.format_temp(float(temps[point.y, point.x]))}"
            )
            draw.text((px + 8, py - 7), label, fill=colour,
                      font=self.font, stroke_width=LABEL_STROKE,
                      stroke_fill=(0, 0, 0))

        return np.asarray(im, dtype=np.uint8)


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


class CsvLogger:
    """Stream measurements to a CSV file at a fixed rate.

    Each sample produces one row per tracked value: the minimum, maximum and
    centre readings plus one row for every sample point (``P1``, ``P2``, ...).
    Columns are timestamp, name, measurement number and value.

    ``log`` is called for every frame but only writes once the interval has
    elapsed, so the file grows at a predictable size instead of at the
    camera's frame rate. The value written is the most recent frame's reading
    rather than an average, so it matches what the overlay showed. The
    measurement number counts samples (not frames), so rows from one sample
    share it and the numbers never repeat.
    """

    COLUMNS = ("timestamp", "name", "measurement", "value")

    def __init__(self, path: Path, interval: float = 1.0) -> None:
        self._file = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(self.COLUMNS)
        self._interval = interval
        self._last_written: float | None = None
        self.measurement = 0

    def log(self, stats: FrameStats, points, temps: np.ndarray) -> None:
        """Record this frame's readings, unless the interval has not elapsed."""
        now = time.monotonic()
        if (
            self._last_written is not None
            and now - self._last_written < self._interval
        ):
            return
        self._last_written = now
        self.measurement += 1
        # ISO 8601 with a UTC offset, so merged logs stay unambiguous.
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        readings = [
            ("min", stats.t_min),
            ("max", stats.t_max),
            ("center", stats.t_center),
        ]
        # Read point values straight from this frame; SamplePoint keeps only
        # running totals and extremes, not the latest value.
        readings.extend(
            (f"P{index + 1}", float(temps[point.y, point.x]))
            for index, point in enumerate(points)
        )
        for name, value in readings:
            self._writer.writerow(
                (stamp, name, self.measurement, f"{value:.2f}")
            )
        # Flush per sample so a kill or crash does not lose the tail.
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def status(message: str) -> None:
    """Write a progress line to stderr."""
    print(message, file=sys.stderr)


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

    status(f"gain: {'high' if cam.get_gain_select() else 'low'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stream annotated thermal video from an InfiRay P2 Pro.",
    )
    ap.add_argument("--cam", required=True, metavar="PATH",
                    help="P2 Pro capture device: /dev/videoN on Linux, or an "
                         "AVFoundation index/name on macOS (for example 0)")
    ap.add_argument("--out", default=None, metavar="PATH_OR_URL",
                    help="publish to a v4l2loopback node on Linux "
                         "(/dev/video1) or serve MJPEG at an HTTP bind "
                         "address (http://0.0.0.0:8080/stream.mjpg). "
                         "Without this the viewer only shows a window")
    ap.add_argument("--no-window", action="store_true",
                    help="do not open a display window")
    ap.add_argument("--csv", default=None, metavar="PATH",
                    help="log every measurement to PATH as CSV: timestamp, "
                         "name (min/max/center/P1..), sample number and value")
    ap.add_argument("--scale", type=int, default=3, help="integer upscale factor")
    ap.add_argument("--font-size", type=int, default=DEFAULT_FONT_SIZE, metavar="PX",
                    help="label font size in pixels (default: %(default)s)")
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

    # The window is the default output; publishing is opt-in, so nothing is
    # written anywhere unless --out names a device or URL.
    use_window = not args.no_window
    use_sink = args.out is not None
    out_path = args.out
    if not use_window and not use_sink:
        ap.error("--no-window without --out leaves no output")

    cam_path = args.cam
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
    sink = None
    if use_sink:
        try:
            sink = backend.open_sink(out_path)
        except backend.BackendError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    renderer = Renderer(
        args.scale, args.font, args.palette, args.unit, args.font_size
    )
    out_w, out_h = renderer.out_w, renderer.out_h

    logger = CsvLogger(args.csv) if args.csv else None

    class Producer:
        """Pull one annotated frame; shared by the window and the sink loop."""

        def __init__(self) -> None:
            self.frames = 0
            self.done = False
            self.error: str | None = None

        def grab(self):
            if self.done:
                return None
            buf = cam.read()
            if buf is None:
                error = getattr(cam, "error", None)
                if error:
                    self.error = error
                    status(f"capture stopped: {error}")
                self.done = True
                return None
            temps = decode_radiometric(buf, cam.stride)
            if emissivity is not None:
                temps = correct_celsius(temps, emissivity, reflected_temp)

            stats = frame_stats(temps)
            base = renderer.render_base(temps, stats)
            overlay = renderer.overlay(stats)
            rgb = renderer.annotate(base, overlay)
            if sink is not None:
                output = rgb
                if window is not None and window.points:
                    output = renderer.render_points(
                        rgb, window.points, temps
                    )
                # Sinks consume packed BGR frames.
                try:
                    sink.write(output[:, :, ::-1])
                except backend.BackendError as exc:
                    self.error = str(exc)
                    status(f"output stopped: {exc}")
                    self.done = True
                    return None
            if logger is not None:
                # Points only exist when a window is running; without one the
                # log still carries the min/max/centre readings.
                points = window.points if window is not None else ()
                logger.log(stats, points, temps)
            self.frames += 1
            if args.frames and self.frames >= args.frames:
                self.done = True
            return Frame(rgb, temps, base=base, overlay=overlay)

    producer = Producer()
    window = None
    try:
        cam.open()
        status(f"capture {cam_path} via {args.backend} (stride {cam.stride})")
        if emissivity is None:
            status("temperatures: as reported by the camera (no correction)")
        else:
            status(
                f"temperatures: grey-body corrected, emissivity "
                f"{emissivity:.4f}, reflected {reflected_temp:.1f} K"
            )

        if sink is not None:
            sink.open(out_w, out_h)
            kind = (
                "MJPEG over HTTP"
                if out_path.startswith("http://")
                else "BGR3"
            )
            status(f"output  {out_path}: {out_w}x{out_h} {kind}")
        else:
            status("output  window only (no loopback)")

        if use_window:
            try:
                window = Window(
                    f"P2 Pro {out_w}x{out_h}",
                    format_temp=renderer.format_temp,
                    font=args.font,
                    font_size=renderer.font_size,
                    frame_width=out_w,
                    label_stroke=LABEL_STROKE,
                    marker_outline=MARKER_OUTLINE,
                    small_font_size=renderer.small_font_size,
                )
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
                status("\ninterrupted")
        else:
            # Headless: pump frames at the camera's rate. grab() returns None
            # once the producer is done or capture stops.
            while producer.grab() is not None:
                pass

        if args.frames:
            status(f"done after {producer.frames} frames")

        if producer.error is not None:
            return 1

    except KeyboardInterrupt:
        status("\ninterrupted")
    except backend.BackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        cam.close()
        if sink is not None:
            sink.close()
        if window is not None:
            window.close()
        if logger is not None:
            logger.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
