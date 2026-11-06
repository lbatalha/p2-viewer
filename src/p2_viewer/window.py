"""Qt display window, driven by a QTimer on Qt's own event loop.

The window is the interactive surface: hovering reports the temperature under
the cursor, clicking drops sample points tracked over time, and the overlay
panel has pause and screenshot buttons. The window paints its own controls,
annotations and hover readout; published frames contain the same annotations
and sample-point markers baked into the image.
"""

from __future__ import annotations

import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from .frame import CAM_HEIGHT, CAM_WIDTH
from .overlay import OverlayLayout, TextItem

try:
    from PySide6.QtCore import QRect, QRectF, QSize, Qt, QTimer
    from PySide6.QtGui import (
        QColor,
        QFont,
        QFontDatabase,
        QFontMetricsF,
        QImage,
        QPainter,
        QPainterPath,
        QPen,
        QPixmap,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QGraphicsOpacityEffect,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QToolButton,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # pragma: no cover - only without the qt extra
    QApplication = None

#: Colours used for the sample points, in the order they are added.
POINT_COLOURS = (
    (255, 214, 0),
    (0, 220, 255),
    (255, 105, 180),
    (170, 255, 120),
)

#: How many sample points can be tracked at once.
MAX_POINTS = 4

#: Gap between the cursor and the hover readout.
HOVER_OFFSET = 14

#: Default style for sample-point labels, overridden by the renderer so the
#: window markers match the labels baked into the frame.
DEFAULT_LABEL_FONT = "monospace"
DEFAULT_LABEL_FONT_SIZE = 14
DEFAULT_LABEL_STROKE = 2
DEFAULT_MARKER_OUTLINE = 1

#: Glyphs for the control buttons. Qt falls back to another font when the
#: chosen family lacks them, so these render even in a monospace face.
PAUSE_GLYPH = "\u23f8"      # double vertical bar
PLAY_GLYPH = "\u23f5"       # right-pointing triangle
SNAPSHOT_GLYPH = "\U0001f4f7"  # photo camera

#: Base control/table geometry, scaled with the displayed image.
BUTTON_SIZE = QSize(30, 22)
GLYPH_FONT_SIZE = 15
PANEL_FONT_SIZE = 11
REMOVE_FONT_SIZE = 12
PANEL_MARGINS = (6, 4, 6, 4)
PANEL_SPACING = 3
GRID_H_SPACING = 8
GRID_V_SPACING = 1
CONTROL_SPACING = 4
CONTROL_MARGIN = 8
PANEL_GAP = 10

#: Opacity of the control buttons: 0.5 means 50% transparent.
BUTTON_OPACITY = 0.5

#: How often the table text is refreshed, in seconds.
TABLE_INTERVAL = 0.2


@dataclass
class Frame:
    """One rendered frame plus what the window needs to draw its overlay."""

    rgb: np.ndarray
    temps: np.ndarray
    base: np.ndarray | None = None
    overlay: OverlayLayout | None = None


@dataclass
class SamplePoint:
    """A pixel whose temperature is tracked over time."""

    x: int
    y: int
    colour: tuple[int, int, int]
    count: int = 0
    total: float = 0.0
    t_min: float = float("inf")
    t_max: float = float("-inf")

    def update(self, temps: np.ndarray) -> float:
        """Fold the current frame in and return this point's temperature."""
        value = float(temps[self.y, self.x])
        self.count += 1
        self.total += value
        self.t_min = min(self.t_min, value)
        self.t_max = max(self.t_max, value)
        return value

    @property
    def average(self) -> float:
        return self.total / self.count if self.count else float("nan")


class DisplayError(RuntimeError):
    """Raised when no display window can be created."""


class FrameProducer(Protocol):
    def grab(self): ...


class _AspectLabel(QLabel):
    """QLabel that scales its pixmap to fit while keeping aspect ratio.

    The image is painted into a centred, aspect-correct rectangle; the rest of
    the widget is black. The hover readout and window overlays are painted
    here, separately from the rendered frame.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(1, 1)
        # Without this, move events only arrive while a button is held.
        self.setMouseTracking(True)
        self._pixmap = None
        self._scaled = None
        self._scaled_size = None
        self._frame = None
        self._points: list[SamplePoint] = []
        self._hover: tuple[int, int] | None = None
        self._format_temp = None
        self._on_add_point = None
        self._on_remove_point = None
        self._on_key = None
        self._on_resize = None
        self._label_font_family = DEFAULT_LABEL_FONT
        self._label_font_size = DEFAULT_LABEL_FONT_SIZE
        self._label_small_size = max(7, round(DEFAULT_LABEL_FONT_SIZE * 0.8))
        self._label_stroke = DEFAULT_LABEL_STROKE
        self._marker_outline = DEFAULT_MARKER_OUTLINE
        self._frame_width: int | None = None
        self._label_font = None
        self._label_font_px = None
        self._small_font = None
        self._small_font_px = None
        # Keys are handled by this widget so they work without an active
        # window.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # -- collaborators -------------------------------------------------------
    def set_formatter(self, format_temp) -> None:
        self._format_temp = format_temp

    def set_point_handler(self, handler) -> None:
        self._on_add_point = handler

    def set_remove_handler(self, handler) -> None:
        self._on_remove_point = handler

    def set_key_handler(self, handler) -> None:
        self._on_key = handler

    def set_resize_handler(self, handler) -> None:
        self._on_resize = handler

    def display_scale(self) -> float:
        """How much the frame is enlarged in the widget right now."""
        rect = self._drawn_rect()
        if rect is None or not self._frame_width:
            return 1.0
        return rect.width() / self._frame_width

    def set_label_style(
        self,
        font_spec: str,
        font_size: int,
        frame_width: int | None,
        stroke: int,
        marker_outline: int = DEFAULT_MARKER_OUTLINE,
        small_font_size: int | None = None,
    ) -> None:
        """Match sample-point markers to the frame's baked-in labels.

        The family, size and outlines come from the renderer; `frame_width` is
        the width the labels were rendered at, so markers can be scaled with
        the displayed image instead of staying a fixed screen size.
        """
        self._label_font_family = self._resolve_font_family(font_spec)
        self._label_font_size = font_size
        self._label_small_size = small_font_size or max(7, round(font_size * 0.8))
        self._label_stroke = stroke
        self._marker_outline = marker_outline
        self._frame_width = frame_width
        self._label_font = None
        self._label_font_px = None
        self._small_font = None
        self._small_font_px = None
        self.update()

    @staticmethod
    def _resolve_font_family(font_spec: str) -> str:
        """Resolve a font file path to a Qt family, or pass a family through."""
        if Path(font_spec).is_file():
            font_id = QFontDatabase.addApplicationFont(font_spec)
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                return families[0]
            return DEFAULT_LABEL_FONT
        return font_spec

    def _label_font_for(self, rect: QRect):
        """The point-label font at the current display scale."""
        scale = rect.width() / self._frame_width if self._frame_width else 1.0
        px = max(1, round(self._label_font_size * scale))
        if self._label_font is None or self._label_font_px != px:
            font = QFont(self._label_font_family)
            font.setPixelSize(px)
            self._label_font = font
            self._label_font_px = px
        return self._label_font

    def _small_font_for(self, rect: QRect):
        """The small font used by legend ticks and min/max labels."""
        scale = rect.width() / self._frame_width if self._frame_width else 1.0
        px = max(1, round(self._label_small_size * scale))
        if self._small_font is None or self._small_font_px != px:
            font = QFont(self._label_font_family)
            font.setPixelSize(px)
            self._small_font = font
            self._small_font_px = px
        return self._small_font

    def set_points(self, points: list[SamplePoint]) -> None:
        self._points = points
        self.update()

    # -- geometry ------------------------------------------------------------
    def _drawn_rect(self) -> QRect | None:
        """The rectangle the image actually occupies, inside the letterbox."""
        if self._pixmap is None or self._pixmap.isNull():
            return None
        area = self.rect()
        scaled = self._pixmap.size().scaled(
            area.size(), Qt.AspectRatioMode.KeepAspectRatio
        )
        x = area.x() + (area.width() - scaled.width()) // 2
        y = area.y() + (area.height() - scaled.height()) // 2
        return QRect(x, y, scaled.width(), scaled.height())

    def image_coord(self, pos) -> tuple[int, int] | None:
        """Map a widget position to a camera pixel, or None if outside."""
        rect = self._drawn_rect()
        if rect is None or not rect.contains(pos):
            return None
        u = (pos.x() - rect.x()) / rect.width()
        v = (pos.y() - rect.y()) / rect.height()
        x = min(CAM_WIDTH - 1, max(0, int(u * CAM_WIDTH)))
        y = min(CAM_HEIGHT - 1, max(0, int(v * CAM_HEIGHT)))
        return x, y

    def widget_pos(self, x: int, y: int):
        """Inverse of :meth:`image_coord`, for drawing markers."""
        rect = self._drawn_rect()
        if rect is None:
            return None
        return (
            rect.x() + int((x + 0.5) / CAM_WIDTH * rect.width()),
            rect.y() + int((y + 0.5) / CAM_HEIGHT * rect.height()),
        )

    # -- events --------------------------------------------------------------
    def set_frame(self, frame: Frame) -> None:
        self._frame = frame
        source = frame.base if frame.base is not None else frame.rgb
        height, width, _ = source.shape
        image = QImage(
            source.data, width, height, width * 3, QImage.Format.Format_RGB888
        )
        # QImage does not own the buffer, so hand QPixmap a copy.
        self._pixmap = QPixmap.fromImage(image.copy())
        self._scaled = None
        self._scaled_size = None
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # The cached scaling is only valid for one size.
        self._scaled = None
        self._scaled_size = None
        if self._on_resize is not None:
            self._on_resize()
        super().resizeEvent(event)

    def _scaled_pixmap(self, rect: QRect) -> QPixmap:
        """Return the frame scaled to `rect`, cached until the size changes."""
        size = (rect.width(), rect.height())
        if self._scaled is None or self._scaled_size != size:
            self._scaled = self._pixmap.scaled(
                rect.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            self._scaled_size = size
        return self._scaled

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        coord = self.image_coord(event.position().toPoint())
        if coord == self._hover:
            # Still within the same sensor pixel: nothing to redraw.
            return
        dirty = self._hover_dirty_rect(self._hover)
        self._hover = coord
        dirty = dirty.united(self._hover_dirty_rect(coord))
        self._hover_rect = None
        if dirty.isNull():
            self.update()
        else:
            self.update(dirty)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        dirty = self._hover_dirty_rect(self._hover)
        self._hover = None
        if dirty.isNull():
            self.update()
        else:
            self.update(dirty)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._on_key is not None and self._on_key(event.key()):
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton and self._on_add_point:
            coord = self.image_coord(event.position().toPoint())
            if coord is not None:
                self._on_add_point(*coord)
        elif event.button() == Qt.MouseButton.RightButton and self._on_remove_point:
            # Undo the most recent point, wherever the cursor is.
            self._on_remove_point()

    # -- painting ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        rect = self._drawn_rect()
        if rect is None:
            return
        painter.drawPixmap(rect.topLeft(), self._scaled_pixmap(rect))
        self._paint_overlay(painter, rect)
        self._paint_points(painter, rect)
        self._paint_hover(painter)

    def _paint_overlay(self, painter: QPainter, rect: QRect) -> None:
        frame = self._frame
        if frame is None or frame.overlay is None:
            return
        layout = frame.overlay
        scale = rect.width() / self._frame_width if self._frame_width else 1.0

        def px(value: float) -> float:
            return rect.x() + value * scale

        def py(value: float) -> float:
            return rect.y() + value * scale

        legend = layout.legend
        bar = QImage(
            legend.pixels.data, legend.w, legend.h, legend.w * 3,
            QImage.Format.Format_RGB888,
        )
        painter.drawImage(
            QRectF(px(legend.x), py(legend.y),
                   legend.w * scale, legend.h * scale),
            bar,
        )
        self._draw_rect_item(painter, legend.border, rect, scale)
        for tick in legend.ticks:
            self._draw_line_item(painter, tick, rect, scale)
        for text in legend.labels:
            self._draw_text_item(painter, text, rect, scale)
        self._draw_text_item(painter, legend.caption, rect, scale)

        for item in layout.rects:
            self._draw_rect_item(painter, item, rect, scale)
        for item in layout.lines:
            self._draw_line_item(painter, item, rect, scale)
        for item in layout.labels:
            self._draw_text_item(painter, item, rect, scale)

    def _draw_line_item(self, painter, item, rect: QRect, scale: float) -> None:
        painter.setPen(QPen(QColor(*item.colour), max(1.0, item.width * scale)))
        painter.drawLine(
            round(rect.x() + item.x0 * scale),
            round(rect.y() + item.y0 * scale),
            round(rect.x() + item.x1 * scale),
            round(rect.y() + item.y1 * scale),
        )

    def _draw_rect_item(self, painter, item, rect: QRect, scale: float) -> None:
        # PIL treats the rectangle bounds as inclusive pixels and draws the
        # outline inside them. A centred QPen on a QRectF does neither, which
        # leaves the border offset from the bar once the window is scaled, so
        # draw the outline as four filled stripes instead.
        colour = QColor(*item.colour)
        x = rect.x() + item.x0 * scale
        y = rect.y() + item.y0 * scale
        w = (item.x1 - item.x0 + 1) * scale
        h = (item.y1 - item.y0 + 1) * scale
        t = max(1.0, item.width * scale)
        painter.fillRect(QRectF(x, y, w, t), colour)
        painter.fillRect(QRectF(x, y + h - t, w, t), colour)
        painter.fillRect(QRectF(x, y, t, h), colour)
        painter.fillRect(QRectF(x + w - t, y, t, h), colour)

    def _draw_text_item(self, painter, item: TextItem, rect: QRect,
                        scale: float) -> None:
        font = (
            self._small_font_for(rect) if item.small
            else self._label_font_for(rect)
        )
        metrics = QFontMetricsF(font)
        path = QPainterPath()
        path.addText(
            rect.x() + item.x * scale,
            rect.y() + item.y * scale + metrics.ascent(),
            font,
            item.text,
        )
        stroke = max(1.0, 2.0 * self._label_stroke * scale)
        outline = QPen(QColor(0, 0, 0), stroke)
        outline.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.strokePath(path, outline)
        painter.fillPath(path, QColor(*item.colour))
        painter.restore()

    def _paint_points(self, painter: QPainter, rect: QRect) -> None:
        frame = self._frame
        if frame is None:
            return
        font = self._label_font_for(rect)
        scale = rect.width() / self._frame_width if self._frame_width else 1.0
        # The path is filled after it is stroked, so only the outer half of
        # the pen remains visible: double the width to match the frame's
        # outline.
        stroke = max(1.0, 2.0 * self._label_stroke * scale)
        outline = QPen(QColor(0, 0, 0), stroke)
        outline.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        # Point shapes are 1 image pixel wide, so they scale with the image.
        line_width = max(1.0, scale)
        shape_outline = max(1.0, self._marker_outline * scale)
        black = QColor(0, 0, 0)
        radius = max(1, round(5 * scale))
        arm = max(1, round(9 * scale))
        # Pen strokes are centred on the shape, so put the black ring just
        # outside the coloured ring rather than overlapping it.
        ring_radius = radius + (line_width + shape_outline) / 2
        ring_pen = QPen(black, shape_outline)
        cross_pen = QPen(black, line_width + 2 * shape_outline)
        colour_pen = QPen(QColor(0, 0, 0), line_width)
        for index, point in enumerate(self._points):
            pos = self.widget_pos(point.x, point.y)
            if pos is None:
                continue
            colour = QColor(*point.colour)
            colour_pen.setColor(colour)
            # Black shape first, then the coloured shape on top, so the
            # outline stays outside the marker.
            painter.setPen(ring_pen)
            painter.drawEllipse(QRectF(
                pos[0] - ring_radius, pos[1] - ring_radius,
                2 * ring_radius, 2 * ring_radius,
            ))
            painter.setPen(cross_pen)
            painter.drawLine(pos[0] - arm, pos[1], pos[0] + arm, pos[1])
            painter.drawLine(pos[0], pos[1] - arm, pos[0], pos[1] + arm)
            painter.setPen(colour_pen)
            painter.drawEllipse(QRectF(
                pos[0] - radius, pos[1] - radius, 2 * radius, 2 * radius,
            ))
            painter.drawLine(pos[0] - arm, pos[1], pos[0] + arm, pos[1])
            painter.drawLine(pos[0], pos[1] - arm, pos[0], pos[1] + arm)
            label = f"P{index + 1}"
            if self._format_temp is not None:
                label += " " + self._format_temp(float(frame.temps[point.y, point.x]))
            path = QPainterPath()
            path.addText(pos[0] + 8, pos[1] - 7, font, label)
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.strokePath(path, outline)
            painter.fillPath(path, colour)
            painter.restore()

    def _hover_text(self, coord) -> str | None:
        if coord is None or self._frame is None or self._format_temp is None:
            return None
        x, y = coord
        return self._format_temp(float(self._frame.temps[y, x]))

    def _hover_readout_rect(self, coord) -> QRect | None:
        """Where the readout box goes for a cursor at `coord`."""
        text = self._hover_text(coord)
        pos = self.widget_pos(*coord) if coord is not None else None
        if text is None or pos is None:
            return None
        metrics = self.fontMetrics()
        width = metrics.horizontalAdvance(text) + 8
        height = metrics.height() + 4
        # Keep the readout inside the image, flipping sides near the edges.
        tx = pos[0] + HOVER_OFFSET
        if tx + width > self.width():
            tx = pos[0] - HOVER_OFFSET - width
        ty = pos[1] - height - 4
        if ty < 0:
            ty = pos[1] + HOVER_OFFSET
        return QRect(max(0, tx), max(0, ty), width, height)

    def _hover_dirty_rect(self, coord) -> QRect:
        """Area to repaint when the cursor moves to or from `coord`."""
        pos = self.widget_pos(*coord) if coord is not None else None
        if pos is None:
            return QRect()
        area = QRect(pos[0] - 8, pos[1] - 8, 17, 17)  # crosshair
        readout = self._hover_readout_rect(coord)
        if readout is not None:
            area = area.united(readout)
        return area.adjusted(-2, -2, 2, 2)

    def _paint_hover(self, painter: QPainter) -> None:
        text = self._hover_text(self._hover)
        rect = self._hover_readout_rect(self._hover)
        pos = self.widget_pos(*self._hover) if self._hover is not None else None
        if text is None or rect is None or pos is None:
            return

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 180))
        painter.drawRect(rect)
        painter.setPen(QPen(QColor(255, 255, 255), 1))
        painter.drawLine(pos[0] - 4, pos[1], pos[0] + 4, pos[1])
        painter.drawLine(pos[0], pos[1] - 4, pos[0], pos[1] + 4)
        painter.drawText(
            rect.x() + 4, rect.y() + rect.height() - 4, text
        )


class _OverlayPanel(QWidget):
    """Overlay container that swallows mouse events.

    Unhandled presses propagate to the parent widget, so without this a click
    on the panel background would land on the image behind it and drop a
    sample point.
    """

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        event.accept()


class _GlyphButton(QPushButton):
    """A button that centres its glyph using the glyph's rendered ink.

    The glyph may come from a fallback font with different metrics, so the
    button measures the rendered ink box and shifts the glyph to the centre.
    """

    def __init__(self, glyph: str, parent=None) -> None:
        super().__init__("", parent)
        self._glyph = glyph
        self._pixmap: QPixmap | None = None
        font = QFont("monospace")
        font.setPixelSize(GLYPH_FONT_SIZE)
        self.setFont(font)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(BUTTON_SIZE)

    def setGlyph(self, glyph: str) -> None:  # noqa: N802 - Qt naming
        self._glyph = glyph
        self._pixmap = None
        self.update()

    def set_scale(self, scale: float) -> None:
        """Resize the button and its glyph with the displayed image."""
        font = self.font()
        font.setPixelSize(max(1, round(GLYPH_FONT_SIZE * scale)))
        self.setFont(font)
        self.setFixedSize(
            max(1, round(BUTTON_SIZE.width() * scale)),
            max(1, round(BUTTON_SIZE.height() * scale)),
        )
        self._pixmap = None
        self.update()

    def _render_glyph(self) -> QPixmap:
        """Render the glyph so its ink box is centred in the button."""
        width, height = self.width(), self.height()
        font = self.font()

        def draw(offset_x: int, offset_y: int) -> QImage:
            image = QImage(width, height, QImage.Format.Format_ARGB32)
            image.fill(QColor(0, 0, 0, 0))
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            painter.setFont(font)
            painter.setPen(QColor(255, 255, 255))
            metrics = painter.fontMetrics()
            baseline = (height + metrics.ascent() - metrics.descent()) // 2
            painter.drawText(offset_x, baseline + offset_y, self._glyph)
            painter.end()
            return image

        probe = draw(0, 0)
        min_x, max_x, min_y, max_y = width, -1, height, -1
        for y in range(height):
            for x in range(width):
                if probe.pixelColor(x, y).alpha() > 40:
                    min_x = min(min_x, x)
                    max_x = max(max_x, x)
                    min_y = min(min_y, y)
                    max_y = max(max_y, y)
        if max_x < 0:
            return QPixmap.fromImage(probe)

        # round rather than floor so odd-sized ink boxes land on the centre
        shift_x = round((width - (max_x - min_x + 1)) / 2) - min_x
        shift_y = round((height - (max_y - min_y + 1)) / 2) - min_y
        return QPixmap.fromImage(draw(shift_x, shift_y))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Let QPushButton paint background, border and checked state.
        super().paintEvent(event)
        if self._pixmap is None:
            self._pixmap = self._render_glyph()
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._pixmap)
        painter.end()


class Window:
    """A Qt window showing annotated thermal frames from a producer."""

    def __init__(
        self,
        title: str = "P2 Pro",
        format_temp=None,
        font: str = DEFAULT_LABEL_FONT,
        font_size: int = DEFAULT_LABEL_FONT_SIZE,
        frame_width: int | None = None,
        label_stroke: int = DEFAULT_LABEL_STROKE,
        marker_outline: int = DEFAULT_MARKER_OUTLINE,
        small_font_size: int | None = None,
        point_renderer=None,
    ) -> None:
        if QApplication is None:
            raise DisplayError(
                "PySide6 is not installed; install the qt extra "
                "(`uv sync --extra qt`) or run with --no-window"
            )
        self.title = title
        self.format_temp = format_temp
        self.label_font = font
        self.label_font_size = font_size
        self.frame_width = frame_width
        self.label_stroke = label_stroke
        self.marker_outline = marker_outline
        self.small_font_size = small_font_size
        self.point_renderer = point_renderer
        self.app = None
        self._label = None
        self._timer = None
        self._producer = None
        self._closed = False
        self._interrupted = False
        self._old_sigint = None
        self._paused = False
        self._points: list[SamplePoint] = []
        self._rows: list[dict] = []
        self._panel = None
        self._frame: Frame | None = None
        self._rows_updated = 0.0
        self._panel_scale: float | None = None

    # -- lifecycle -----------------------------------------------------------
    def open(self, width: int, height: int) -> None:
        try:
            self.app = QApplication.instance() or QApplication([])
        except Exception as exc:  # noqa: BLE001 - Qt raises assorted types
            raise DisplayError(f"cannot open a display window: {exc}") from exc
        if self.app.platformName() not in ("wayland", "xcb", "cocoa", "windows"):
            raise DisplayError(
                f"Qt selected the {self.app.platformName()!r} platform plugin, "
                f"which cannot show a window; set QT_QPA_PLATFORM or use "
                f"--no-window"
            )

        self._label = _AspectLabel()
        self._label.setWindowTitle(self.title)
        self._label.resize(width, height)
        self._label.setStyleSheet("background: black;")
        self._label.set_label_style(
            self.label_font,
            self.label_font_size,
            self.frame_width or width,
            self.label_stroke,
            self.marker_outline,
            self.small_font_size,
        )
        if self.format_temp is not None:
            self._label.set_formatter(self.format_temp)
        self._label.set_point_handler(self.add_point)
        self._label.set_remove_handler(self.remove_last_point)
        self._label.set_key_handler(self._on_key)
        self._label.set_resize_handler(self._apply_panel_scale)
        self._label.closeEvent = self._on_close_event
        self._build_panel()
        self._label.show()

    def run(self, producer: FrameProducer, fps: float = 25.0) -> int:
        """Drive `producer` from a QTimer until the window closes."""
        self._producer = producer

        def tick() -> None:
            if self._closed:
                return
            if self._interrupted:
                self._request_close()
                return
            if not self._paused:
                frame = self._producer.grab()
                if frame is not None:
                    self._set_frame(frame)
            # A finite-run producer (e.g. --frames) must end the event loop,
            # otherwise app.exec() never returns.
            if getattr(self._producer, "done", False):
                self._request_close()

        interval_ms = max(1, int(round(1000.0 / fps))) if fps > 0 else 0
        self._timer = QTimer()
        self._timer.timeout.connect(tick)
        self._timer.start(interval_ms)

        # Signal handlers don't run while QApplication.exec() blocks.
        self._old_sigint = signal.signal(signal.SIGINT, self._on_sigint)
        try:
            self.app.exec()
        finally:
            if self._old_sigint is not None:
                signal.signal(signal.SIGINT, self._old_sigint)
                self._old_sigint = None
            if self._timer is not None:
                self._timer.stop()
                self._timer = None
        return 0

    # -- overlay panel -------------------------------------------------------
    def _build_panel(self) -> None:
        """Build the tracked-points table and the buttons, in the top left."""
        panel = _OverlayPanel(self._label)
        # A plain QWidget does not paint its stylesheet background unless it
        # is told to, so the panel would be transparent behind the text.
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        panel.setStyleSheet(
            "QWidget { background: rgba(0, 0, 0, 190); color: white;"
            "font-family: monospace; font-size: 11px; }"
        )
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(*PANEL_MARGINS)
        outer.setSpacing(PANEL_SPACING)
        self._outer = outer

        grid = QGridLayout()
        grid.setHorizontalSpacing(GRID_H_SPACING)
        grid.setVerticalSpacing(GRID_V_SPACING)
        self._grid = grid
        for column, title in enumerate(("", "now", "min", "max", "avg")):
            heading = QLabel(title)
            heading.setStyleSheet("color: #9fd2ff; background: transparent;")
            grid.addWidget(heading, 0, column)
        grid.addWidget(QLabel(""), 0, 5)  # spacer for the remove buttons

        for index in range(MAX_POINTS):
            name = QLabel(f"P{index + 1}")
            colour = POINT_COLOURS[index]
            name.setStyleSheet(
                f"color: rgb({colour[0]}, {colour[1]}, {colour[2]});"
                "background: transparent;"
            )
            values = []
            for column in range(1, 5):
                cell = QLabel("-")
                cell.setStyleSheet("background: transparent;")
                grid.addWidget(cell, index + 1, column)
                values.append(cell)
            remove = QToolButton()
            remove.setText("\u00d7")
            remove.setToolTip("remove this point")
            remove.setStyleSheet(
                "QToolButton { color: white; background: transparent;"
                "border: none; font-size: 12px; }"
                "QToolButton:hover { color: #ff8080; }"
            )
            remove.clicked.connect(lambda _, i=index: self.remove_point(i))
            grid.addWidget(name, index + 1, 0)
            grid.addWidget(remove, index + 1, 5)
            self._rows.append(
                {"name": name, "values": values, "remove": remove}
            )
        outer.addLayout(grid)

        self._pause_button = _GlyphButton(PAUSE_GLYPH)
        self._pause_button.setCheckable(True)
        self._pause_button.setToolTip("pause (Space)")
        self._pause_button.clicked.connect(self.toggle_pause)
        self._snapshot_button = _GlyphButton(SNAPSHOT_GLYPH)
        self._snapshot_button.setToolTip("save a snapshot (S)")
        self._snapshot_button.clicked.connect(self.save_screenshot)
        control_style = (
            "QPushButton, QToolButton { color: white;"
            "background: rgba(30, 30, 30, 210);"
            "border: 1px solid rgba(120, 120, 120, 210); padding: 0px; }"
            "QPushButton:hover, QToolButton:hover"
            " { background: rgba(80, 80, 80, 230); }"
            "QPushButton:checked { background: rgba(170, 50, 50, 230); }"
            "QToolButton { padding: 0px 4px; }"
        )
        for button in (self._pause_button, self._snapshot_button):
            button.setStyleSheet(control_style)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setFixedSize(BUTTON_SIZE)

        # The controls sit in the top-left corner and stay put whatever the
        # panel does; the panel tucks underneath them.
        toggle = _GlyphButton("\u25be")
        toggle.setToolTip("hide the point table")
        toggle.setStyleSheet(control_style)
        toggle.clicked.connect(self.toggle_table)
        self._collapse_button = toggle

        controls = _OverlayPanel(self._label)
        strip = QHBoxLayout(controls)
        strip.setContentsMargins(0, 0, 0, 0)
        strip.setSpacing(CONTROL_SPACING)
        self._strip = strip
        self._buttons = (toggle, self._pause_button, self._snapshot_button)
        for button in self._buttons:
            strip.addWidget(button)
        controls.adjustSize()
        controls.move(CONTROL_MARGIN, CONTROL_MARGIN)
        controls.show()
        self._controls = controls

        for button in self._buttons:
            effect = QGraphicsOpacityEffect(button)
            effect.setOpacity(BUTTON_OPACITY)
            button.setGraphicsEffect(effect)

        panel.adjustSize()
        panel.move(CONTROL_MARGIN, controls.height() + PANEL_GAP)
        self._panel = panel
        # No points yet, so there is nothing to show; the panel appears as
        # soon as the first point is placed.
        self._table_collapsed = False
        self._refresh_rows()
        self._apply_panel_visibility()
        self._panel_scale = None
        self._apply_panel_scale()

    def _apply_panel_scale(self) -> None:
        """Resize the table and controls with the displayed image."""
        if self._panel is None or self._controls is None:
            return
        scale = self._label.display_scale()
        if self._panel_scale is not None and abs(scale - self._panel_scale) < 0.001:
            return
        self._panel_scale = scale

        panel_px = max(1, round(PANEL_FONT_SIZE * scale))
        self._panel.setStyleSheet(
            "QWidget { background: rgba(0, 0, 0, 190); color: white;"
            f"font-family: monospace; font-size: {panel_px}px; }}"
        )
        remove_px = max(1, round(REMOVE_FONT_SIZE * scale))
        remove_style = (
            "QToolButton { color: white; background: transparent;"
            f"border: none; font-size: {remove_px}px; }}"
            "QToolButton:hover { color: #ff8080; }"
        )
        for row in self._rows:
            row["remove"].setStyleSheet(remove_style)

        self._outer.setContentsMargins(
            *[max(0, round(value * scale)) for value in PANEL_MARGINS]
        )
        self._outer.setSpacing(max(0, round(PANEL_SPACING * scale)))
        self._grid.setHorizontalSpacing(max(0, round(GRID_H_SPACING * scale)))
        self._grid.setVerticalSpacing(max(0, round(GRID_V_SPACING * scale)))
        self._strip.setSpacing(max(0, round(CONTROL_SPACING * scale)))

        border = max(1, round(scale))
        padding = max(0, round(4 * scale))
        control_style = (
            "QPushButton, QToolButton { color: white;"
            "background: rgba(30, 30, 30, 210);"
            f"border: {border}px solid rgba(120, 120, 120, 210);"
            "padding: 0px; }"
            "QPushButton:hover, QToolButton:hover"
            " { background: rgba(80, 80, 80, 230); }"
            "QPushButton:checked { background: rgba(170, 50, 50, 230); }"
            f"QToolButton {{ padding: 0px {padding}px; }}"
        )
        for button in self._buttons:
            button.setStyleSheet(control_style)
            button.set_scale(scale)

        margin = max(0, round(CONTROL_MARGIN * scale))
        gap = max(0, round(PANEL_GAP * scale))
        self._controls.adjustSize()
        self._controls.move(margin, margin)
        self._panel.adjustSize()
        self._panel.move(margin, self._controls.height() + gap)

    def _apply_panel_visibility(self) -> None:
        """Show the panel only when points exist and it is not collapsed."""
        if self._panel is None:
            return
        visible = bool(self._points) and not self._table_collapsed
        self._panel.setVisible(visible)
        if self._collapse_button is not None:
            self._collapse_button.setEnabled(bool(self._points))
            self._collapse_button.setGlyph("\u25be" if visible else "\u25b8")
            self._collapse_button.setToolTip(
                "hide the point table" if visible else "show the point table"
            )
        self._panel.adjustSize()

    def _refresh_rows(self) -> None:
        """Show a row per existing point and hide the rest."""
        for index, row in enumerate(self._rows):
            visible = index < len(self._points)
            for widget in (row["name"], row["remove"], *row["values"]):
                widget.setVisible(visible)
        if self._panel is not None:
            self._panel.adjustSize()

    def _update_rows(self, force: bool = False) -> None:
        if not self._points or self.format_temp is None:
            return
        now = time.monotonic()
        if not force and now - self._rows_updated < TABLE_INTERVAL:
            return
        self._rows_updated = now
        temps = self._frame.temps
        for index, point in enumerate(self._points):
            row = self._rows[index]
            current = float(temps[point.y, point.x])
            for cell, value in zip(
                row["values"],
                (current, point.t_min, point.t_max, point.average),
            ):
                cell.setText(self.format_temp(value))

    # -- points --------------------------------------------------------------
    def add_point(self, x: int, y: int) -> None:
        """Track a new pixel, up to :data:`MAX_POINTS`."""
        if len(self._points) >= MAX_POINTS:
            return
        point = SamplePoint(x, y, POINT_COLOURS[len(self._points)])
        if self._frame is not None:
            point.update(self._frame.temps)
        self._points.append(point)
        self._label.set_points(self._points)
        self._refresh_rows()
        self._apply_panel_visibility()
        self._update_rows(force=True)

    def remove_last_point(self) -> None:
        """Undo the most recently added point."""
        if self._points:
            self.remove_point(len(self._points) - 1)

    def remove_point(self, index: int) -> None:
        if 0 <= index < len(self._points):
            del self._points[index]
            # Keep the colours aligned with the P1..Pn labels.
            for position, point in enumerate(self._points):
                point.colour = POINT_COLOURS[position]
            self._sync_colours()
            self._label.set_points(self._points)
            self._refresh_rows()
            self._apply_panel_visibility()
            # The remaining points have shifted up a row.
            self._update_rows(force=True)

    def _sync_colours(self) -> None:
        for index, row in enumerate(self._rows):
            colour = POINT_COLOURS[index]
            row["name"].setStyleSheet(
                f"color: rgb({colour[0]}, {colour[1]}, {colour[2]});"
                "background: transparent;"
            )

    # -- actions -------------------------------------------------------------
    def toggle_table(self) -> None:
        """Show or hide the whole overlay panel, leaving only the controls."""
        self._table_collapsed = not self._table_collapsed
        self._apply_panel_visibility()

    def toggle_pause(self) -> None:
        self._paused = not self._paused
        self._pause_button.setChecked(self._paused)
        # Show the action the button performs next.
        self._pause_button.setGlyph(PLAY_GLYPH if self._paused else PAUSE_GLYPH)
        self._pause_button.setToolTip(
            "resume (Space)" if self._paused else "pause (Space)"
        )

    def save_screenshot(self) -> None:
        """Write the current frame and sample points to a PNG."""
        if self._frame is None:
            return
        from PIL import Image

        rgb = self._frame.rgb
        if self._points and self.point_renderer is not None:
            rgb = self.point_renderer(rgb, self._points, self._frame.temps)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = Path(f"p2pro-{stamp}.png").resolve()
        Image.fromarray(rgb).save(path)
        print(f"saved {path}", file=sys.stderr)

    # -- frames --------------------------------------------------------------
    def _set_frame(self, frame: Frame) -> None:
        self._frame = frame
        for point in self._points:
            point.update(frame.temps)
        self._label.set_frame(frame)
        self._apply_panel_scale()
        self._update_rows()

    # -- events --------------------------------------------------------------
    def _on_key(self, key) -> bool:
        """Handle a key press, returning True if it was used."""
        if key == Qt.Key.Key_Escape:
            self._request_close()
            return True
        if key == Qt.Key.Key_Space:
            self.toggle_pause()
            return True
        if key == Qt.Key.Key_S:
            self.save_screenshot()
            return True
        return False

    def _on_sigint(self, signum, frame) -> None:
        self._interrupted = True

    def _on_close_event(self, event) -> None:
        self._request_close(event)

    def _request_close(self, event=None) -> None:
        self._closed = True
        if self._timer is not None:
            self._timer.stop()
        if event is not None:
            event.accept()
        if self._label is not None:
            self._label.close()
            self._label.hide()
        if self.app is not None:
            self.app.quit()

    @property
    def interrupted(self) -> bool:
        """True if the loop stopped because of Ctrl-C."""
        return self._interrupted

    @property
    def points(self) -> list[SamplePoint]:
        """The sample points being tracked, in P1..Pn order."""
        return self._points

    def close(self) -> None:
        self._closed = True
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self.app is not None:
            self.app.quit()
        if self._label is not None:
            self._label.close()
            self._label = None
