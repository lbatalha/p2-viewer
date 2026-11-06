"""Painter-independent layout for the annotations drawn over a frame.

The layout stores frame-space primitives only: positions, colours, text, and
the rendered legend bar. The PIL renderer uses it for published frames, and
the Qt window uses the same layout at display resolution, so both paths show
the same overlay.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

Colour = tuple[int, int, int]


@dataclass(frozen=True)
class TextItem:
    """A text label in frame coordinates."""

    text: str
    x: int
    y: int
    colour: Colour
    small: bool = False


@dataclass(frozen=True)
class LineItem:
    """A one-pixel line in frame coordinates."""

    x0: int
    y0: int
    x1: int
    y1: int
    colour: Colour
    width: int = 1


@dataclass(frozen=True)
class RectItem:
    """An outlined rectangle in frame coordinates."""

    x0: int
    y0: int
    x1: int
    y1: int
    colour: Colour
    width: int = 1


@dataclass(frozen=True)
class LegendItem:
    """The colour bar plus its ticks and labels."""

    x: int
    y: int
    w: int
    h: int
    pixels: np.ndarray
    border: RectItem
    ticks: tuple[LineItem, ...]
    labels: tuple[TextItem, ...]
    caption: TextItem


@dataclass(frozen=True)
class OverlayLayout:
    """Everything the window and the baked renderer need to draw."""

    legend: LegendItem
    lines: tuple[LineItem, ...]
    rects: tuple[RectItem, ...]
    labels: tuple[TextItem, ...]
