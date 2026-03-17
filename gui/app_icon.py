"""
app_icon.py — Procedurally generated application icon for eXoGUI.

Produces a QIcon at multiple resolutions without requiring any external image files.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QRect, QRectF
from PyQt6.QtGui import (
    QColor, QFont, QIcon, QLinearGradient, QPainter, QPixmap,
)


def make_app_icon() -> QIcon:
    """Return a multi-resolution QIcon for the application."""
    icon = QIcon()
    for size in (16, 32, 64, 128, 256, 512):
        icon.addPixmap(_render(size))
    return icon


def make_app_pixmap(size: int) -> QPixmap:
    """Return a single QPixmap of the app icon at *size* × *size* pixels."""
    return _render(size)


def _render(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    radius = size * 0.18

    # ── background gradient ───────────────────────────────────────────────────
    grad = QLinearGradient(0, 0, 0, size)
    grad.setColorAt(0.0, QColor("#1e1e3a"))
    grad.setColorAt(1.0, QColor("#0a0a18"))
    p.setBrush(grad)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    # ── subtle inner glow border ──────────────────────────────────────────────
    if size >= 32:
        from PyQt6.QtGui import QPen
        border_pen = QPen(QColor("#4a90d944"), max(1, size // 80))
        p.setPen(border_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        inset = size * 0.025
        p.drawRoundedRect(
            QRectF(inset, inset, size - inset * 2, size - inset * 2),
            radius * 0.9, radius * 0.9
        )

    # ── "eXo" text (accent blue) ──────────────────────────────────────────────
    exo_size = max(8, int(size * 0.38))
    f_exo = QFont()
    f_exo.setFamilies(["Helvetica Neue", "Helvetica", "DejaVu Sans", "Liberation Sans", "Arial", "sans-serif"])
    f_exo.setPixelSize(exo_size)
    f_exo.setBold(True)
    p.setFont(f_exo)
    p.setPen(QColor("#5aa8f0"))
    exo_rect = QRect(0, int(size * 0.08), size, int(size * 0.46))
    p.drawText(exo_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, "eXo")

    # ── "GUI" text (magenta/pink) ─────────────────────────────────────────────
    gui_size = max(6, int(size * 0.26))
    f_gui = QFont()
    f_gui.setFamilies(["Helvetica Neue", "Helvetica", "DejaVu Sans", "Liberation Sans", "Arial", "sans-serif"])
    f_gui.setPixelSize(gui_size)
    f_gui.setBold(True)
    p.setFont(f_gui)
    p.setPen(QColor("#cc44cc"))
    gui_rect = QRect(0, int(size * 0.52), size, int(size * 0.30))
    p.drawText(gui_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, "GUI")

    # ── ">_" prompt (subtle, bottom-right) ────────────────────────────────────
    if size >= 32:
        prompt_size = max(5, int(size * 0.13))
        f_prompt = QFont()
        f_prompt.setFamilies(["Courier New", "Courier", "DejaVu Sans Mono", "Liberation Mono", "monospace"])
        f_prompt.setPixelSize(prompt_size)
        f_prompt.setBold(True)
        p.setFont(f_prompt)
        p.setPen(QColor("#cc44cc80"))
        prompt_rect = QRect(
            0, int(size * 0.80), size - int(size * 0.08), int(size * 0.16)
        )
        p.drawText(
            prompt_rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            ">_"
        )

    p.end()
    return pm
