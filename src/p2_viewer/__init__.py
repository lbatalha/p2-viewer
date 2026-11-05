"""InfiRay P2 Pro thermal camera viewer for Linux."""

from __future__ import annotations

__version__ = "0.1.0"


def main() -> None:
    """Console-script entry point."""
    import sys

    from .viewer import main as _main

    sys.exit(_main())


__all__ = ["main", "__version__"]
