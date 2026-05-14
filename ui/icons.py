"""
Programmatic icon generator for battery states.

All icons are rendered via QPainter/QPixmap — no image files needed.
Icons are cached after first render to avoid repeated painting.

Icon anatomy (22×22 px):
  - Battery body: rounded rectangle, outline only
  - Battery terminal (nub): small rectangle on right
  - Fill level: coloured rectangle inside the body, width ∝ percentage
  - Charging bolt: white lightning bolt drawn over fill when charging
  - Error cross: red X drawn when sensor is in error state

Colour palette:
  - Green  (#4caf50) — > 40 %
  - Yellow (#ffc107) — 20–40 %
  - Red    (#f44336) — < 20 %
"""

from __future__ import annotations

import math
from functools import lru_cache

from PyQt6.QtCore import Qt, QRect, QPoint
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap, QIcon, QPolygon


_W, _H = 22, 22          # icon canvas size
_BX, _BY = 2, 6          # battery body top-left
_BW, _BH = 15, 10        # battery body width/height
_NW, _NH = 2, 4          # terminal nub width/height
_PAD = 1                  # inner padding for fill rect

_GREEN  = QColor("#4caf50")
_YELLOW = QColor("#ffc107")
_RED    = QColor("#f44336")
_WHITE  = QColor("#ffffff")
_OUTLINE = QColor("#cccccc")
_CHARGING_BLUE = QColor("#2196f3")
_ERROR_RED = QColor("#f44336")


def _level_color(pct: float) -> QColor:
    if pct > 40:
        return _GREEN
    if pct > 20:
        return _YELLOW
    return _RED


@lru_cache(maxsize=64)
def battery_icon(percentage: float, charging: bool, error: bool = False) -> QIcon:
    """
    Return a QIcon for the given battery state.

    percentage — 0.0–100.0
    charging   — draws a lightning bolt overlay
    error      — draws a red X (sensor failure)
    """
    pct   = max(0.0, min(100.0, percentage))
    px    = QPixmap(_W, _H)
    px.fill(Qt.GlobalColor.transparent)

    painter = QPainter(px)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    body_rect  = QRect(_BX, _BY, _BW, _BH)
    nub_rect   = QRect(_BX + _BW, _BY + (_BH - _NH) // 2, _NW, _NH)
    inner_rect = QRect(
        _BX + _PAD + 1,
        _BY + _PAD + 1,
        _BW - 2 * _PAD - 2,
        _BH - 2 * _PAD - 2,
    )

    # Battery body outline
    pen = QPen(_OUTLINE)
    pen.setWidth(1)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(body_rect)
    painter.fillRect(nub_rect, _OUTLINE)

    # Fill level
    if not error:
        fill_w = max(0, int(round(inner_rect.width() * pct / 100.0)))
        if fill_w > 0:
            fill_rect = QRect(
                inner_rect.left(), inner_rect.top(),
                fill_w, inner_rect.height(),
            )
            fill_color = _CHARGING_BLUE if charging else _level_color(pct)
            painter.fillRect(fill_rect, fill_color)

    # Charging bolt
    if charging and not error:
        _draw_bolt(painter, body_rect)

    # Error indicator
    if error:
        _draw_error_x(painter, body_rect)

    painter.end()
    return QIcon(px)


def _draw_bolt(painter: QPainter, body: QRect) -> None:
    cx   = body.center().x()
    cy   = body.center().y()
    pts  = QPolygon([
        QPoint(cx - 1, body.top() + 1),
        QPoint(cx - 3, cy),
        QPoint(cx,     cy),
        QPoint(cx + 1, body.bottom() - 1),
        QPoint(cx + 3, cy + 1),
        QPoint(cx,     cy + 1),
    ])
    pen = QPen(_WHITE)
    pen.setWidth(1)
    painter.setPen(pen)
    painter.setBrush(_WHITE)
    painter.drawPolygon(pts)


def _draw_error_x(painter: QPainter, body: QRect) -> None:
    pen = QPen(_ERROR_RED)
    pen.setWidth(2)
    painter.setPen(pen)
    x1, y1 = body.left() + 3, body.top() + 2
    x2, y2 = body.right() - 2, body.bottom() - 2
    painter.drawLine(x1, y1, x2, y2)
    painter.drawLine(x2, y1, x1, y2)
