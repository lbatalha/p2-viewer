"""False-colour palettes the viewer renders itself.

Each palette is a list of (position, rgb) stops interpolated to a 256-entry
LUT. These are entirely ours: the camera also has its own preview palettes
(see ``p2_viewer.vendor``), but those only affect the camera's built-in
preview image, which the viewer does not display, so they are a separate,
mostly-useless knob."""

from __future__ import annotations

import numpy as np

# Type alias for readability: a palette is a tuple of (position, rgb) stops.
Palette = tuple[tuple[float, tuple[int, int, int]], ...]

PALETTES: dict[str, Palette] = {
    "ironbow": (
        (0.00, (0, 0, 0)),
        (0.13, (30, 0, 60)),
        (0.28, (90, 0, 140)),
        (0.45, (180, 0, 150)),
        (0.60, (225, 30, 35)),
        (0.75, (248, 150, 10)),
        (0.88, (255, 220, 0)),
        (1.00, (255, 255, 255)),
    ),
    "rainbow": (
        (0.00, (0, 0, 130)),
        (0.20, (0, 90, 255)),
        (0.40, (0, 220, 220)),
        (0.55, (0, 220, 60)),
        (0.70, (255, 230, 0)),
        (0.85, (255, 90, 0)),
        (1.00, (180, 0, 0)),
    ),
    "whitehot": (
        (0.00, (0, 0, 0)),
        (1.00, (255, 255, 255)),
    ),
    "blackhot": (
        (0.00, (255, 255, 255)),
        (1.00, (0, 0, 0)),
    ),
    "hotmetal": (
        (0.00, (0, 0, 0)),
        (0.35, (110, 0, 0)),
        (0.65, (255, 40, 0)),
        (0.85, (255, 170, 0)),
        (1.00, (255, 255, 220)),
    ),
    "arctic": (
        (0.00, (255, 255, 255)),
        (0.40, (130, 200, 255)),
        (0.70, (40, 110, 220)),
        (1.00, (0, 0, 90)),
    ),
}

DEFAULT_PALETTE = "ironbow"

LUT_SIZE = 256


def palette_names() -> list[str]:
    """Return the available palette names, sorted."""
    return sorted(PALETTES)


def build_lut(name: str = DEFAULT_PALETTE, size: int = LUT_SIZE) -> np.ndarray:
    """Build a ``(size, 3)`` uint8 lookup table for the named palette.

    Stops are linearly interpolated per channel, which is enough for these
    purpose-built ramps and keeps the result independent of any plotting
    library.
    """
    try:
        stops = PALETTES[name]
    except KeyError:
        raise KeyError(
            f"unknown palette {name!r}; available: {', '.join(palette_names())}"
        ) from None

    xs = np.linspace(0.0, 1.0, size)
    pos = np.array([s[0] for s in stops], dtype=np.float32)
    cols = np.array([s[1] for s in stops], dtype=np.float32)
    return np.stack(
        [np.interp(xs, pos, cols[:, k]) for k in range(3)], axis=1
    ).astype(np.uint8)
