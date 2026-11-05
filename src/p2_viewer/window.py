"""Qt display window, driven by a QTimer on Qt's own event loop.

PySide6 provides native Wayland support, avoiding XWayland on Wayland desktops."""

from __future__ import annotations

import signal
from typing import Protocol

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
    from PySide6.QtWidgets import QApplication, QLabel
except ImportError:  # pragma: no cover - only without the qt extra
    QApplication = None


class DisplayError(RuntimeError):
    """Raised when no display window can be created."""


class FrameProducer(Protocol):
    def grab(self) -> "object | None": ...


class _AspectLabel(QLabel):
    """QLabel that scales its pixmap to fit while keeping aspect ratio.

    ``setScaledContents(True)`` stretches to fill, which distorts the image as
    soon as the window is resized or fullscreened. Painting the pixmap into a
    centred, aspect-correct rectangle instead keeps the thermal image square
    and adds black letterbox bars.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(1, 1)
        self._pixmap = None

    def set_frame(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QPainter

        painter = QPainter(self)
        painter.fillRect(self.rect(), _Qt.GlobalColor.black)
        if self._pixmap is None or self._pixmap.isNull():
            return
        area = self.rect()
        scaled = self._pixmap.size().scaled(
            area.size(), _Qt.AspectRatioMode.KeepAspectRatio
        )
        # Centre the scaled image in the available area.
        x = area.x() + (area.width() - scaled.width()) // 2
        y = area.y() + (area.height() - scaled.height()) // 2
        painter.drawPixmap(
            x, y, scaled.width(), scaled.height(), self._pixmap
        )


class Window:
    """A Qt window showing annotated thermal frames from a producer."""

    def __init__(
        self,
        title: str = "P2 Pro",
    ) -> None:
        if QApplication is None:
            raise DisplayError(
                "PySide6 is not installed; install the qt extra "
                "(`uv sync --extra qt`) or run with --no-window"
            )
        self.title = title
        self.app = None
        self._label = None
        self._timer = None
        self._producer = None
        self._closed = False
        self._interrupted = False
        self._old_sigint = None

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
        QShortcut(QKeySequence(Qt.Key_Escape), self._label, self._request_close)
        self._label.closeEvent = self._on_close_event
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
            rgb = self._producer.grab()
            if rgb is not None:
                self._set_frame(rgb)
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

    # -- events --------------------------------------------------------------
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
    def closed(self) -> bool:
        return self._closed

    # -- drawing -------------------------------------------------------------
    def _set_frame(self, rgb) -> None:
        height, width, _ = rgb.shape
        image = QImage(
            rgb.data, width, height, width * 3, QImage.Format.Format_RGB888
        )
        # QImage does not own the buffer, so hand QPixmap a copy before `rgb`
        # goes out of scope at the end of this call.
        self._label.set_frame(QPixmap.fromImage(image))

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
