"""
widgets.py - small custom-painted widgets for the NetMic dashboard.

Deliberately dependency-free (no pyqtgraph/matplotlib): both widgets are a
few dozen lines of QPainter code, which keeps CPU overhead low and avoids
pulling in a heavy plotting stack for two simple visuals.
"""

from __future__ import annotations

import collections

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget


class VUMeterWidget(QWidget):
    """Horizontal RMS bar with a peak marker. set_level() is cheap to call often."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._rms = 0.0
        self._peak = 0.0
        self._peak_hold = 0.0
        self._peak_hold_frames = 0
        self.setMinimumHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_level(self, rms: float, peak: float) -> None:
        self._rms = max(0.0, min(1.0, rms))
        self._peak = max(0.0, min(1.0, peak))
        if self._peak >= self._peak_hold:
            self._peak_hold = self._peak
            self._peak_hold_frames = 0
        else:
            self._peak_hold_frames += 1
            if self._peak_hold_frames > 20:  # hold the peak marker briefly, then let it fall
                self._peak_hold = max(self._peak, self._peak_hold - 0.02)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())

        painter.fillRect(rect, QColor(30, 30, 34))

        gradient = QLinearGradient(rect.left(), 0, rect.right(), 0)
        gradient.setColorAt(0.0, QColor(60, 200, 120))
        gradient.setColorAt(0.70, QColor(230, 200, 60))
        gradient.setColorAt(0.90, QColor(220, 70, 60))
        bar_rect = QRectF(rect.left(), rect.top(), rect.width() * self._rms, rect.height())
        painter.fillRect(bar_rect, gradient)

        if self._peak_hold > 0.0:
            x = rect.left() + rect.width() * self._peak_hold
            painter.setPen(QPen(QColor(255, 255, 255), 2))
            painter.drawLine(int(x), 0, int(x), int(rect.height()))

        painter.setPen(QPen(QColor(70, 70, 76), 1))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))


class LineGraphWidget(QWidget):
    """Scrolling line graph for a single numeric series (RTT, throughput, ...)."""

    def __init__(self, title: str, unit: str, max_points: int = 120,
                 color: QColor = QColor(90, 170, 250), parent: QWidget | None = None):
        super().__init__(parent)
        self._title = title
        self._unit = unit
        self._color = color
        self._points: collections.deque = collections.deque(maxlen=max_points)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def add_point(self, value: float | None) -> None:
        self._points.append(value)
        self.update()

    def clear(self) -> None:
        self._points.clear()
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        painter.fillRect(0, 0, w, h, QColor(24, 24, 28))

        values = [v for v in self._points if v is not None]
        top_margin, bottom_margin, left_margin = 18, 18, 4
        plot_h = max(h - top_margin - bottom_margin, 1)
        vmax = max(values) if values else 1.0
        vmax = max(vmax * 1.15, 1e-6)

        painter.setPen(QColor(180, 180, 188))
        current = f"{values[-1]:.1f}" if values else "--"
        painter.drawText(6, 14, f"{self._title}: {current} {self._unit}")

        if len(self._points) >= 2:
            painter.setPen(QPen(self._color, 2))
            step = (w - left_margin) / max(len(self._points) - 1, 1)
            prev_point = None
            x = left_margin
            for v in self._points:
                if v is None:
                    prev_point = None
                    x += step
                    continue
                y = top_margin + plot_h - (v / vmax) * plot_h
                if prev_point is not None:
                    painter.drawLine(int(prev_point[0]), int(prev_point[1]), int(x), int(y))
                prev_point = (x, y)
                x += step

        painter.setPen(QPen(QColor(60, 60, 66), 1))
        painter.drawRect(0, 0, w - 1, h - 1)