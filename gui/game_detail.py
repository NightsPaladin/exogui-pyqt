"""
game_detail.py — Right-side detail panel: box art, metadata, screenshots carousel,
                  videos, and documents.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter, QColor, QFont, QLinearGradient
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QSizePolicy, QGridLayout, QStackedWidget,
    QSplitter, QTextEdit,
)

from core.game_library import Game, Extra
from core.image_cache import ImageCache
from gui.flow_layout import FlowLayout
from gui import themes


def _open_path(path: str) -> None:
    """Open a file or directory with the platform's default application."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# Thumbnail dimensions for video cards
_VT_W, _VT_H = 152, 86   # 16:9-ish


def _label(text: str, color: str | None = None, size: int = 12, bold: bool = False) -> QLabel:
    if color is None:
        color = themes.current().text_hi
    lbl = QLabel(text)
    weight = "bold" if bold else "normal"
    lbl.setStyleSheet(f"color:{color}; font-size:{size}px; font-weight:{weight};")
    lbl.setWordWrap(True)
    return lbl


def _button(text: str, color: str | None = None, fg: str = "#fff") -> QPushButton:
    if color is None:
        color = themes.current().accent
    btn = QPushButton(text)
    btn.setStyleSheet(
        f"QPushButton {{ background:{color}; color:{fg}; border:none; border-radius:6px;"
        f"padding:8px 18px; font-size:14px; font-weight:bold; }}"
        f"QPushButton:hover {{ background:{color}dd; }}"
        f"QPushButton:pressed {{ background:{color}99; }}"
        f"QPushButton:disabled {{ background:#555; color:#888; }}"
    )
    return btn


# ── video thumbnail card ──────────────────────────────────────────────────────

class VideoCard(QWidget):
    """
    Small preview card for a video Extra.
    Shows a thumbnail frame (extracted via ffmpeg) plus the title below.
    Clicking the card opens the video with macOS 'open'.
    """

    def __init__(self, extra: Extra, cache: ImageCache, parent=None):
        super().__init__(parent)
        self._extra = extra
        self.setFixedWidth(_VT_W + 4)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(extra.name)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Thumbnail area
        self._thumb_lbl = QLabel()
        self._thumb_lbl.setFixedSize(_VT_W, _VT_H)
        self._thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t = themes.current()
        self._thumb_lbl.setStyleSheet(
            f"background:{t.bg_window}; border-radius:4px 4px 0 0;"
            f" color:{t.text_lo}; font-size:22px;"
        )
        self._thumb_lbl.setText("▶")    # placeholder until thumb loads
        layout.addWidget(self._thumb_lbl)

        # Title strip
        title_max = 22
        short_title = extra.name if len(extra.name) <= title_max else extra.name[:title_max - 1] + "…"
        title_lbl = QLabel(short_title)
        title_lbl.setFixedWidth(_VT_W)
        title_lbl.setStyleSheet(
            f"background:{t.bg_card}; color:{t.text_med}; font-size:10px;"
            f" padding: 3px 4px; border-radius: 0 0 4px 4px;"
        )
        layout.addWidget(title_lbl)

        # Request thumbnail asynchronously
        cache.get_video_thumb(
            extra.path,
            callback=lambda _k, pm: self._set_thumb(pm),
            scaled_to=(_VT_W, _VT_H),
        )

    def _set_thumb(self, pm: QPixmap) -> None:
        if not pm.isNull():
            self._thumb_lbl.setText("")
            self._thumb_lbl.setPixmap(pm)

    def mousePressEvent(self, event) -> None:
        _open_path(self._extra.path)
        super().mousePressEvent(event)


# ── screenshot carousel ───────────────────────────────────────────────────────

class ScreenshotCarousel(QWidget):
    """Horizontal strip of small screenshot thumbnails; click advances the main image."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._main_label = QLabel()
        self._main_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._main_label.setMinimumHeight(200)
        t = themes.current()
        self._main_label.setStyleSheet(f"background:{t.bg_window}; border-radius:6px;")
        self._main_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        self._thumb_scroll = QScrollArea()
        self._thumb_scroll.setFixedHeight(68)
        self._thumb_scroll.setWidgetResizable(True)
        self._thumb_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._thumb_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._thumb_scroll.setStyleSheet(
            f"QScrollArea {{ background:{t.bg_window}; border:none; }}"
        )

        self._thumb_container = QWidget()
        self._thumb_layout = QHBoxLayout(self._thumb_container)
        self._thumb_layout.setContentsMargins(4, 4, 4, 4)
        self._thumb_layout.setSpacing(4)
        self._thumb_layout.addStretch()
        self._thumb_scroll.setWidget(self._thumb_container)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._main_label, 1)
        layout.addWidget(self._thumb_scroll)

        self._paths: list[str] = []
        self._current = 0

    def set_screenshots(self, paths: list[str], cache: ImageCache) -> None:
        self._paths = paths
        self._current = 0
        self._cache = cache

        # Clear thumbs
        for i in reversed(range(self._thumb_layout.count())):
            item = self._thumb_layout.itemAt(i)
            if item and item.widget():
                item.widget().deleteLater()
                self._thumb_layout.removeItem(item)

        if not paths:
            self._main_label.setText("No screenshots available")
            t = themes.current()
            self._main_label.setStyleSheet(
                f"background:{t.bg_window}; color:{t.text_lo}; font-size:12px; border-radius:6px;"
            )
            return

        t = themes.current()
        self._main_label.setStyleSheet(f"background:{t.bg_window}; border-radius:6px;")

        for i, path in enumerate(paths):
            thumb = QLabel()
            thumb.setFixedSize(88, 60)
            thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            thumb.setStyleSheet(
                f"background:{t.bg_card}; border:2px solid {t.accent if i == 0 else t.border};"
                "border-radius:4px;"
            )
            thumb.setCursor(Qt.CursorShape.PointingHandCursor)
            idx = i  # capture for lambda
            thumb.mousePressEvent = lambda _e, n=idx: self._show(n)
            self._thumb_layout.insertWidget(i, thumb)
            cache.get(path, callback=lambda p, pm, lbl=thumb: self._set_thumb(lbl, pm),
                      scaled_to=(88, 60))

        self._show(0)

    def _set_thumb(self, label: QLabel, pm: QPixmap) -> None:
        if not pm.isNull():
            label.setPixmap(pm)

    def _show(self, idx: int) -> None:
        if not self._paths or idx >= len(self._paths):
            return
        self._current = idx
        self._cache.get(
            self._paths[idx],
            callback=lambda p, pm: self._set_main(pm),
            scaled_to=(640, 400),
        )
        # Update thumbnail borders
        for i in range(self._thumb_layout.count() - 1):
            w = self._thumb_layout.itemAt(i).widget()
            if w:
                border = "#4a90d9" if i == idx else "#333"
                current_style = w.styleSheet()
                w.setStyleSheet(
                    current_style.replace("border:2px solid #4a90d9", f"border:2px solid {border}")
                                 .replace("border:2px solid #333", f"border:2px solid {border}")
                )

    def _set_main(self, pm: QPixmap) -> None:
        if not pm.isNull():
            self._main_label.setPixmap(
                pm.scaled(self._main_label.size(),
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )


# ── detail panel ─────────────────────────────────────────────────────────────

class GameDetailPanel(QWidget):
    """
    Right-side panel showing:
      - Box art (large)
      - Title + metadata grid
      - Description
      - Screenshots carousel
      - Play / Install buttons
    """

    play_requested    = pyqtSignal(object)    # Game
    install_requested = pyqtSignal(object)    # Game
    cancel_requested  = pyqtSignal()

    def __init__(self, image_cache: ImageCache, exodos_root: str, parent=None):
        super().__init__(parent)
        self._cache   = image_cache
        self._fallback = os.path.join(exodos_root, "eXo", "util", "exodos.png")
        self._game: Optional[Game] = None
        self._build_ui()
        self._show_placeholder()

    # ── ui construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        t = themes.current()
        self.setStyleSheet(f"background:{t.bg_panel};")

        # Reuse or create the outer layout
        outer = self.layout()
        if outer is None:
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)
        else:
            # Clear existing content (scroll area) for a theme rebuild
            while outer.count():
                item = outer.takeAt(0)
                if item and item.widget():
                    item.widget().deleteLater()

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background:{t.bg_panel}; border:none; }}"
            f"QScrollBar:vertical {{ background:{t.bg_panel}; width:8px; border:none; }}"
            f"QScrollBar::handle:vertical {{ background:{t.handle}; border-radius:4px; min-height:20px; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
        )

        _inner = QWidget()
        _inner.setStyleSheet(f"background:{t.bg_panel};")
        root = QVBoxLayout(_inner)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── top strip: box art + metadata ─────────────────────────────────
        top_strip = QHBoxLayout()
        top_strip.setContentsMargins(12, 12, 12, 8)
        top_strip.setSpacing(14)

        # Box art
        self._box_art = QLabel()
        self._box_art.setFixedSize(160, 210)
        self._box_art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._box_art.setStyleSheet(f"background:{t.bg_window}; border-radius:6px;")
        top_strip.addWidget(self._box_art, 0, Qt.AlignmentFlag.AlignTop)

        # Right of box art: title + metadata
        meta_col = QVBoxLayout()
        meta_col.setSpacing(4)

        self._title_label = _label("", t.text_hi, 22, bold=True)
        self._title_label.setWordWrap(True)
        meta_col.addWidget(self._title_label)

        self._year_genre_label = _label("", t.text_med, 13)
        meta_col.addWidget(self._year_genre_label)

        self._dev_pub_label = _label("", t.text_med, 12)
        meta_col.addWidget(self._dev_pub_label)

        # Rating + emulator badges
        badge_row = QHBoxLayout()
        badge_row.setSpacing(6)
        self._rating_label = _label("", t.accent, 12)
        badge_row.addWidget(self._rating_label)
        self._emu_label = _label("", "#ffa040", 11)
        badge_row.addWidget(self._emu_label)
        self._installed_label = _label("", t.green, 11)
        badge_row.addWidget(self._installed_label)
        badge_row.addStretch()
        meta_col.addLayout(badge_row)

        meta_col.addSpacing(4)

        # Compatibility warning (hidden by default)
        self._warning_banner = QLabel()
        self._warning_banner.setWordWrap(True)
        self._warning_banner.setStyleSheet(
            "QLabel { background: #3a2800; color: #ffcc44; border: 1px solid #7a5500;"
            " border-radius: 4px; padding: 5px 8px; font-size: 11px; }"
        )
        self._warning_banner.hide()
        meta_col.addWidget(self._warning_banner)

        meta_col.addSpacing(4)

        # Play/Install buttons + phase label
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._play_btn = _button("▶  Play")
        self._play_btn.clicked.connect(self._on_play)
        self._install_btn = _button("⬇  Install", "#2e7d32")
        self._install_btn.clicked.connect(self._on_install)
        self._cancel_btn = _button("✕  Cancel", "#c62828")
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._cancel_btn.hide()
        btn_row.addWidget(self._play_btn)
        btn_row.addWidget(self._install_btn)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addStretch()
        meta_col.addLayout(btn_row)

        self._fetch_phase_label = QLabel("")
        self._fetch_phase_label.setStyleSheet(
            f"color:{t.text_med}; font-size:11px; font-style:italic;"
        )
        self._fetch_phase_label.hide()
        meta_col.addWidget(self._fetch_phase_label)

        self._download_size_label = QLabel("")
        self._download_size_label.setStyleSheet(
            f"color:{t.text_lo}; font-size:11px;"
        )
        self._download_size_label.hide()
        meta_col.addWidget(self._download_size_label)

        meta_col.addStretch()

        top_strip.addLayout(meta_col, 1)
        root.addLayout(top_strip)

        # Thin separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color:{t.border};")
        root.addWidget(sep)

        # ── description ───────────────────────────────────────────────────
        desc_wrap = QWidget()
        desc_wrap.setStyleSheet(f"background:{t.bg_window};")
        desc_inner = QVBoxLayout(desc_wrap)
        desc_inner.setContentsMargins(12, 8, 12, 8)

        desc_hdr = _label("Description", t.text_med, 11, bold=True)
        desc_inner.addWidget(desc_hdr)

        self._desc_text = QTextEdit()
        self._desc_text.setReadOnly(True)
        self._desc_text.setMaximumHeight(100)
        self._desc_text.setStyleSheet(
            f"QTextEdit {{ background: {t.bg_window}; color: {t.text_med};"
            f" border: none; font-size: 13px; }}"
        )
        desc_inner.addWidget(self._desc_text)

        root.addWidget(desc_wrap)

        # ── screenshots ───────────────────────────────────────────────────
        scr_wrap = QWidget()
        scr_inner = QVBoxLayout(scr_wrap)
        scr_inner.setContentsMargins(12, 8, 12, 8)

        scr_hdr = _label("Screenshots", t.text_med, 11, bold=True)
        scr_inner.addWidget(scr_hdr)

        self._carousel = ScreenshotCarousel()
        self._carousel.setMinimumHeight(300)
        scr_inner.addWidget(self._carousel)

        root.addWidget(scr_wrap)

        # ── extras (videos + documents) ───────────────────────────────────
        self._extras_wrap = QWidget()
        self._extras_wrap.hide()
        extras_inner = QVBoxLayout(self._extras_wrap)
        extras_inner.setContentsMargins(12, 4, 12, 12)
        extras_inner.setSpacing(6)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color:{t.border};")
        extras_inner.addWidget(sep2)

        extras_hdr = _label("Videos & Documents", t.text_med, 11, bold=True)
        extras_inner.addWidget(extras_hdr)

        # ── Videos: horizontal flow (wraps to next line) ──────────────────
        self._videos_wrap = QWidget()
        self._videos_wrap.hide()
        videos_v = QVBoxLayout(self._videos_wrap)
        videos_v.setContentsMargins(0, 0, 0, 0)
        videos_v.setSpacing(2)
        videos_v.addWidget(_label("🎬  Videos", t.text_lo, 10, bold=True))

        self._videos_container = QWidget()
        self._videos_container.setStyleSheet("background:transparent;")
        self._videos_flow = FlowLayout(self._videos_container, h_spacing=6, v_spacing=4)
        videos_v.addWidget(self._videos_container)
        extras_inner.addWidget(self._videos_wrap)

        # ── Documents: vertical full-width list ───────────────────────────
        self._docs_wrap = QWidget()
        self._docs_wrap.hide()
        docs_v = QVBoxLayout(self._docs_wrap)
        docs_v.setContentsMargins(0, 0, 0, 0)
        docs_v.setSpacing(2)
        docs_v.addWidget(_label("📄  Documents", t.text_lo, 10, bold=True))

        self._docs_container = QWidget()
        self._docs_container.setStyleSheet("background:transparent;")
        self._docs_list = QVBoxLayout(self._docs_container)
        self._docs_list.setContentsMargins(0, 0, 4, 0)
        self._docs_list.setSpacing(3)
        docs_v.addWidget(self._docs_container)
        extras_inner.addWidget(self._docs_wrap)

        root.addWidget(self._extras_wrap)

        root.addStretch()   # push all content toward the top

        self._scroll.setWidget(_inner)
        outer.addWidget(self._scroll)

    def rebuild_ui(self) -> None:
        """Rebuild the entire detail panel with the current theme (call after theme change)."""
        saved_game = self._game
        self._game = None
        self._build_ui()
        self._show_placeholder()
        if saved_game is not None:
            self.show_game(saved_game)

    # ── public ────────────────────────────────────────────────────────────────

    def show_game(self, game: Game) -> None:
        self._game = game

        self._title_label.setText(game.title)

        year_genre = " · ".join(filter(None, [game.display_year] + game.genres[:2]))
        self._year_genre_label.setText(year_genre)

        dev_pub_parts = []
        if game.developer:
            dev_pub_parts.append(f"Dev: {game.developer}")
        if game.publisher and game.publisher != game.developer:
            dev_pub_parts.append(f"Pub: {game.publisher}")
        self._dev_pub_label.setText("  ·  ".join(dev_pub_parts))

        if game.community_rating:
            stars = "★" * round(game.community_rating) + "☆" * (5 - round(game.community_rating))
            self._rating_label.setText(stars)
        else:
            self._rating_label.setText("")

        self._emu_label.setText(f"[{game.emulator_display}]")

        if game.installed:
            self._installed_label.setText("● Installed")
            self._installed_label.setStyleSheet(
                f"color:{themes.current().green}; font-size:11px;")
        else:
            self._installed_label.setText("○ Not installed")
            self._installed_label.setStyleSheet(
                f"color:{themes.current().text_lo}; font-size:11px;")

        self._play_btn.setEnabled(game.installed)
        self._install_btn.setEnabled(not game.installed)

        # Lite mode: ZIP not yet acquired
        zip_present = getattr(game, "zip_present", True)
        if not zip_present and not game.installed:
            self._install_btn.setText("⬇  Download & Install")
            dl_size = getattr(game, "download_size_str", "")
            if dl_size:
                self._download_size_label.setText(f"Download: ~{dl_size}")
                self._download_size_label.show()
            else:
                self._download_size_label.hide()
        else:
            self._install_btn.setText("⬇  Install")
            self._download_size_label.hide()

        self._fetch_phase_label.hide()
        self._fetch_phase_label.setText("")
        self._cancel_btn.hide()

        # macOS compatibility warning
        if game.compat_note:
            self._warning_banner.setText(f"⚠  {game.compat_note}")
            self._warning_banner.show()
        else:
            self._warning_banner.hide()

        # Box art — fall back to first screenshot, then eXoDOS icon if nothing available
        box_path = game.image_paths.get("box_front")
        shots = game.image_paths.get("screenshots", [])
        cover_path = box_path or (shots[0] if shots else None)
        if cover_path:
            self._set_box_art_fallback()  # show icon immediately; replaced on load
            self._cache.get(cover_path,
                            callback=lambda p, pm: self._set_box_art(pm),
                            scaled_to=(160, 210))
        else:
            self._set_box_art_fallback()

        # Description
        self._desc_text.setPlainText(game.notes or "No description available.")

        # Screenshots
        self._carousel.set_screenshots(shots, self._cache)

        # Extras (videos + documents)
        self._populate_extras(game.extras)

    def _populate_extras(self, extras: list) -> None:
        """Rebuild Videos (flow chips) and Documents (vertical list) from the game's extras."""
        # Clear video flow
        while self._videos_flow.count():
            item = self._videos_flow.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        # Clear docs list (keep trailing stretch)
        while self._docs_list.count() > 1:
            item = self._docs_list.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        videos = [e for e in extras if e.kind == "video"]
        docs   = [e for e in extras if e.kind in ("pdf", "document", "audio", "image")]

        for extra in videos:
            self._videos_flow.addWidget(VideoCard(extra, self._cache))

        for extra in docs:
            self._docs_list.insertWidget(
                self._docs_list.count() - 1, self._make_doc_row(extra)
            )

        self._videos_wrap.setVisible(bool(videos))
        self._docs_wrap.setVisible(bool(docs))
        self._extras_wrap.setVisible(bool(videos or docs))

    def _make_doc_row(self, extra: Extra) -> QWidget:
        """Full-width row with icon+name on the left and an Open button on the right."""
        icons = {"pdf": "◉", "document": "≡", "audio": "♪", "image": "⬛", "other": "•"}
        icon  = icons.get(extra.kind, "•")
        t = themes.current()

        row = QWidget()
        row.setStyleSheet(
            f"QWidget {{ background:{t.bg_card}; border-left:2px solid {t.border}; }}"
            f"QWidget:hover {{ background:{t.bg_input}; border-left:2px solid {t.accent}; }}"
        )
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(10, 4, 6, 4)
        row_layout.setSpacing(8)

        name_lbl = QLabel(f"{icon}  {extra.name}")
        name_lbl.setStyleSheet(
            f"color:{t.text_med}; font-size:12px; background:transparent; border:none;"
        )
        name_lbl.setWordWrap(False)
        row_layout.addWidget(name_lbl, 1)

        open_btn = QPushButton("Open")
        open_btn.setFixedWidth(54)
        open_btn.setStyleSheet(
            f"QPushButton {{ background:{t.accent}33; color:{t.accent}; border:1px solid {t.accent}55;"
            f" border-radius:4px; padding:2px 6px; font-size:11px; }}"
            f"QPushButton:hover {{ background:{t.accent}; color:#fff; }}"
        )
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        path = extra.path
        open_btn.clicked.connect(lambda: _open_path(path))
        row_layout.addWidget(open_btn)

        return row

    def _show_placeholder(self) -> None:
        self._title_label.setText("Select a game")
        self._year_genre_label.setText("")
        self._dev_pub_label.setText("")
        self._rating_label.setText("")
        self._emu_label.setText("")
        self._installed_label.setText("")
        self._play_btn.setEnabled(False)
        self._install_btn.setEnabled(False)
        self._install_btn.setText("⬇  Install")
        self._cancel_btn.hide()
        self._fetch_phase_label.hide()
        self._fetch_phase_label.setText("")
        self._download_size_label.hide()
        self._warning_banner.hide()
        self._desc_text.setPlainText("")
        self._box_art.setText("")
        self._carousel.set_screenshots([], self._cache)
        self._extras_wrap.hide()
        self._videos_wrap.hide()
        self._docs_wrap.hide()

    # ── slots ─────────────────────────────────────────────────────────────────

    def _set_box_art_fallback(self) -> None:
        """Display the eXoDOS logo when no game art is available."""
        pm = QPixmap(self._fallback)
        if pm.isNull():
            self._box_art.setText("No art")
            return
        self._box_art.setPixmap(
            pm.scaled(160, 210,
                      Qt.AspectRatioMode.KeepAspectRatio,
                      Qt.TransformationMode.SmoothTransformation)
        )
        t = themes.current()
        self._box_art.setStyleSheet(f"background:{t.bg_window}; border-radius:6px;")

    def _set_box_art(self, pm: QPixmap) -> None:
        if not pm.isNull():
            self._box_art.setPixmap(
                pm.scaled(160, 210,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )
            t = themes.current()
            self._box_art.setStyleSheet(f"background:{t.bg_window}; border-radius:6px;")
        else:
            self._set_box_art_fallback()

    def _on_play(self) -> None:
        if self._game:
            self.play_requested.emit(self._game)

    def _on_install(self) -> None:
        if self._game:
            self.install_requested.emit(self._game)

    def _on_cancel(self) -> None:
        self.cancel_requested.emit()

    def set_fetch_phase(self, phase: str) -> None:
        """Update the phase label shown during a two-phase fetch operation."""
        self._fetch_phase_label.setText(phase)
        self._fetch_phase_label.show()
        self._download_size_label.hide()
        self._cancel_btn.show()

    def set_installing(self, progress: int, total: int) -> None:
        if total > 0:
            pct = int(progress / total * 100)
            # Show percentage for torrent/copy phase (0-100 range) or
            # file count for extraction phase (larger totals)
            if total <= 100:
                self._install_btn.setText(f"Working… {pct}%")
            else:
                self._install_btn.setText(f"Extracting… {pct}%")
        self._install_btn.setEnabled(False)

    def set_install_done(self, success: bool, message: str = "") -> None:
        self._fetch_phase_label.hide()
        self._fetch_phase_label.setText("")
        self._cancel_btn.hide()
        if success:
            self._installed_label.setText("● Installed")
            self._installed_label.setStyleSheet(
                f"color:{themes.current().green}; font-size:11px;")
            self._play_btn.setEnabled(True)
            self._install_btn.setEnabled(False)
            self._install_btn.setText("⬇  Install")
            self._download_size_label.hide()
        else:
            self._install_btn.setText("⬇  Retry")
            self._install_btn.setEnabled(True)

    def set_fetch_cancelled(self) -> None:
        """Restore the detail panel after the user cancels a fetch."""
        self._cancel_btn.hide()
        self._fetch_phase_label.hide()
        self._fetch_phase_label.setText("")
        # Restore the install button to its pre-download state
        if self._game:
            zip_present = getattr(self._game, "zip_present", True)
            if not zip_present and not self._game.installed:
                self._install_btn.setText("⬇  Download & Install")
                dl_size = getattr(self._game, "download_size_str", "")
                if dl_size:
                    self._download_size_label.setText(f"Download: ~{dl_size}")
                    self._download_size_label.show()
                else:
                    self._download_size_label.hide()
            else:
                self._install_btn.setText("⬇  Install")
                self._download_size_label.hide()
        else:
            self._install_btn.setText("⬇  Install")
            self._download_size_label.hide()
        self._install_btn.setEnabled(True)
