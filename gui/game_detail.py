"""
game_detail.py — Right-side detail panel: box art, metadata, media gallery,
                  videos, music, and documents.
"""

from __future__ import annotations

import os
import subprocess
import sys

from PyQt6.QtCore import Qt, QSize, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter, QColor, QFont, QLinearGradient
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QSizePolicy, QGridLayout, QStackedWidget,
    QSplitter, QTextEdit,
)

try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaDevices
    _HAS_MULTIMEDIA = True
except ImportError:
    _HAS_MULTIMEDIA = False

from core import debug
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


def _restyle_button(btn: QPushButton, color: str, fg: str = "#fff") -> None:
    """Apply a new background colour to an existing button."""
    btn.setStyleSheet(
        f"QPushButton {{ background:{color}; color:{fg}; border:none; border-radius:6px;"
        f"padding:8px 18px; font-size:14px; font-weight:bold; }}"
        f"QPushButton:hover {{ background:{color}dd; }}"
        f"QPushButton:pressed {{ background:{color}99; }}"
        f"QPushButton:disabled {{ background:#555; color:#888; }}"
    )


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
        self._current_pm: QPixmap | None = None

        self._main_label = QLabel()
        self._main_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._main_label.setFixedHeight(200)   # updated dynamically when image loads
        t = themes.current()
        self._main_label.setStyleSheet(f"background:{t.bg_window}; border-radius:6px;")
        self._main_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )

        self._thumb_scroll = QScrollArea()
        # 80px thumbnail + 4px top margin + 4px bottom margin + 6px scrollbar
        self._thumb_scroll.setFixedHeight(94)
        self._thumb_scroll.setWidgetResizable(True)
        self._thumb_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._thumb_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._thumb_scroll.setStyleSheet(
            f"QScrollArea {{ background:{t.bg_window}; border:none; }}"
            f"QScrollBar:horizontal {{ background:transparent; height:6px; border:none; margin:0; }}"
            f"QScrollBar::handle:horizontal {{ background:{t.handle}; border-radius:3px; min-width:20px; }}"
            "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; }"
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
        layout.addWidget(self._main_label)
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
            self._current_pm = None
            self._main_label.setText("No images available")
            t = themes.current()
            self._main_label.setStyleSheet(
                f"background:{t.bg_window}; color:{t.text_lo}; font-size:12px; border-radius:6px;"
            )
            return

        t = themes.current()
        self._main_label.setStyleSheet(f"background:{t.bg_window}; border-radius:6px;")

        for i, path in enumerate(paths):
            thumb = QLabel()
            thumb.setFixedSize(120, 80)
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
                      scaled_to=(120, 80))

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
            scaled_to=(1280, 800),
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

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale_main()

    def _set_main(self, pm: QPixmap) -> None:
        if not pm.isNull():
            self._current_pm = pm
            self._rescale_main()

    def _rescale_main(self) -> None:
        """Keep the display area at 4:3 relative to the label width, then fit the image within it."""
        w = self._main_label.width()
        if w <= 0:
            return
        # Fixed 4:3 display area — typical DOS screenshots fill it perfectly;
        # wider images get small top/bottom gaps; square images get small side gaps.
        h = max(200, w * 3 // 4)
        self._main_label.setFixedHeight(h)
        pm = self._current_pm
        if pm is None or pm.isNull():
            return
        self._main_label.setPixmap(
            pm.scaled(QSize(w, h),
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
      - Media gallery
      - Play / Install buttons
    """

    play_requested      = pyqtSignal(object)    # Game
    install_requested   = pyqtSignal(object)    # Game
    uninstall_requested = pyqtSignal(object)    # Game
    cancel_requested    = pyqtSignal()

    def __init__(self, image_cache: ImageCache, exodos_root: str, parent=None):
        super().__init__(parent)
        self._cache   = image_cache
        self._fallback = os.path.join(exodos_root, "eXo", "util", "exodos.png")
        self._game: Game | None = None
        self._autoplay: bool = False
        self._playing_path: str = ""
        self._audio_rows: list[tuple[str, QPushButton]] = []

        if _HAS_MULTIMEDIA:
            default_dev = QMediaDevices.defaultAudioOutput()
            if default_dev.isNull():
                # No specific device found; let Qt choose (may use a system default)
                if debug.enabled:
                    print("[audio] defaultAudioOutput() is null — using Qt default device",
                          file=sys.stderr)
                self._audio_output = QAudioOutput(self)
            else:
                if debug.enabled:
                    print(f"[audio] device: {default_dev.description()!r} "
                          f"({default_dev.id().data().decode(errors='replace')!r})",
                          file=sys.stderr)
                self._audio_output = QAudioOutput(default_dev, self)
            self._audio_output.setVolume(0.7)
            self._player: QMediaPlayer | None = QMediaPlayer(self)
            self._player.setAudioOutput(self._audio_output)
            self._player.playbackStateChanged.connect(self._on_playback_state_changed)
            self._player.errorOccurred.connect(self._on_player_error)
        else:
            self._player = None

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

        # Extra metadata: ESRB rating, series, play mode, max players
        self._extra_meta_label = _label("", t.text_lo, 11)
        self._extra_meta_label.hide()
        meta_col.addWidget(self._extra_meta_label)

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

        # ── media ─────────────────────────────────────────────────────────
        self._media_wrap = QWidget()
        media_inner = QVBoxLayout(self._media_wrap)
        media_inner.setContentsMargins(12, 8, 12, 12)
        media_inner.setSpacing(6)

        media_hdr = _label("Media", t.text_med, 11, bold=True)
        media_inner.addWidget(media_hdr)

        self._images_wrap = QWidget()
        images_v = QVBoxLayout(self._images_wrap)
        images_v.setContentsMargins(0, 0, 0, 0)
        images_v.setSpacing(2)
        images_v.addWidget(_label("🖼  Images", t.text_lo, 10, bold=True))

        self._carousel = ScreenshotCarousel()
        self._carousel.setMinimumHeight(300)
        images_v.addWidget(self._carousel)
        media_inner.addWidget(self._images_wrap)

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
        media_inner.addWidget(self._videos_wrap)

        # ── Music: vertical full-width list ───────────────────────────────
        self._music_wrap = QWidget()
        self._music_wrap.hide()
        music_v = QVBoxLayout(self._music_wrap)
        music_v.setContentsMargins(0, 0, 0, 0)
        music_v.setSpacing(2)
        music_v.addWidget(_label("♪  Music", t.text_lo, 10, bold=True))

        self._music_container = QWidget()
        self._music_container.setStyleSheet("background:transparent;")
        self._music_list = QVBoxLayout(self._music_container)
        self._music_list.setContentsMargins(0, 0, 4, 0)
        self._music_list.setSpacing(3)
        self._music_list.addStretch()
        music_v.addWidget(self._music_container)
        media_inner.addWidget(self._music_wrap)

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
        self._docs_list.addStretch()
        docs_v.addWidget(self._docs_container)
        media_inner.addWidget(self._docs_wrap)

        root.addWidget(self._media_wrap)

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

    # ── audio playback ────────────────────────────────────────────────────────

    def set_autoplay(self, enabled: bool) -> None:
        """Enable or disable auto-play of music when a game is selected."""
        self._autoplay = enabled
        if not enabled:
            self.stop_audio()

    def stop_audio(self) -> None:
        """Stop any currently playing audio and clear the playing state."""
        if self._player:
            self._player.stop()
        self._playing_path = ""
        self._refresh_play_buttons()

    def _on_player_error(self, error, error_string: str) -> None:
        """Log audio playback errors to stderr (helps diagnose missing backends)."""
        if error_string and debug.enabled:
            print(f"[audio] {error_string}", file=sys.stderr)

    def _play_audio(self, path: str) -> None:
        """Toggle playback for *path*; stops current track if a different one is chosen."""
        if not self._player:
            return
        if self._playing_path == path:
            state = self._player.playbackState()
            if _HAS_MULTIMEDIA and state == QMediaPlayer.PlaybackState.PlayingState:
                self._player.pause()
            else:
                self._player.play()
            return
        self._playing_path = path
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()

    def _on_playback_state_changed(self, _state) -> None:
        self._refresh_play_buttons()

    def _refresh_play_buttons(self) -> None:
        is_playing = (
            _HAS_MULTIMEDIA and self._player is not None
            and self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        )
        for path, btn in self._audio_rows:
            if not btn:
                continue
            btn.setText("⏸" if (is_playing and path == self._playing_path) else "▶")



    # ── public ────────────────────────────────────────────────────────────────

    def show_game(self, game: Game) -> None:
        # Stop any audio from the previously selected game immediately.
        self.stop_audio()

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

        # Extra metadata: ESRB, series, play mode, max players
        extra_parts = []
        if game.rating:
            extra_parts.append(f"ESRB: {game.rating}")
        if game.series:
            extra_parts.append(f"Series: {game.series}")
        play_modes = [m.strip() for m in game.play_mode.split(";") if m.strip()]
        if play_modes:
            extra_parts.append(" / ".join(play_modes))
        if game.max_players > 1:
            extra_parts.append(f"Max: {game.max_players} players")
        if extra_parts:
            self._extra_meta_label.setText("  ·  ".join(extra_parts))
            self._extra_meta_label.show()
        else:
            self._extra_meta_label.hide()

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

        if game.installed:
            self._install_btn.setText("✖  Uninstall")
            _restyle_button(self._install_btn, "#8b2222")
            self._install_btn.setEnabled(True)
            self._download_size_label.hide()
        else:
            _restyle_button(self._install_btn, "#2e7d32")
            self._install_btn.setEnabled(True)
            # Lite mode: ZIP not yet acquired
            zip_present = getattr(game, "zip_present", True)
            if not zip_present:
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

        # Box art — fall back to best available cover art, then eXoDOS icon if nothing available
        gallery = game.image_paths.get("gallery", [])
        cover_path = game.primary_cover_path or None
        if cover_path:
            self._set_box_art_fallback()  # show icon immediately; replaced on load
            self._cache.get(cover_path,
                            callback=lambda p, pm: self._set_box_art(pm),
                            scaled_to=(160, 210))
        else:
            self._set_box_art_fallback()

        # Description
        self._desc_text.setPlainText(game.notes or "No description available.")

        # Media gallery
        self._carousel.set_screenshots(gallery, self._cache)

        # Collection media + Extras
        self._populate_media(game.extras)

        # Auto-play first audio track if the feature is enabled.
        if self._autoplay and self._player:
            audio_extras = [e for e in game.extras if e.kind == "audio"]
            if audio_extras:
                self._play_audio(audio_extras[0].path)

    def _populate_media(self, extras: list) -> None:
        """Rebuild Media subsections from collection media files and the game's Extras/ folder."""
        # Clear video flow
        while self._videos_flow.count():
            item = self._videos_flow.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        while self._music_list.count() > 1:
            item = self._music_list.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        # Clear docs list (keep trailing stretch)
        while self._docs_list.count() > 1:
            item = self._docs_list.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        # Reset audio-row tracking (widgets above were just deleted).
        self._audio_rows = []

        videos = [e for e in extras if e.kind == "video"]
        music  = [e for e in extras if e.kind == "audio"]
        docs   = [e for e in extras if e.kind in ("pdf", "document", "image")]

        for extra in videos:
            self._videos_flow.addWidget(VideoCard(extra, self._cache))

        for extra in music:
            row, play_btn = self._make_audio_row(extra)
            self._audio_rows.append((extra.path, play_btn))
            self._music_list.insertWidget(self._music_list.count() - 1, row)

        for extra in docs:
            self._docs_list.insertWidget(
                self._docs_list.count() - 1, self._make_doc_row(extra)
            )

        self._images_wrap.setVisible(bool(self._game and self._game.image_paths.get("gallery")))
        self._videos_wrap.setVisible(bool(videos))
        self._music_wrap.setVisible(bool(music))
        self._docs_wrap.setVisible(bool(docs))
        self._media_wrap.setVisible(bool(self._game and self._game.image_paths.get("gallery")) or bool(videos or music or docs))

    def _make_audio_row(self, extra: Extra) -> tuple[QWidget, QPushButton]:
        """Audio row: name | ▶/⏸ play button | Open button."""
        t = themes.current()

        row = QWidget()
        row.setStyleSheet(
            f"QWidget {{ background:{t.bg_card}; border-left:2px solid {t.border}; }}"
            f"QWidget:hover {{ background:{t.bg_input}; border-left:2px solid {t.accent}; }}"
        )
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(10, 4, 6, 4)
        row_layout.setSpacing(8)

        name_lbl = QLabel(f"♪  {extra.name}")
        name_lbl.setStyleSheet(
            f"color:{t.text_med}; font-size:12px; background:transparent; border:none;"
        )
        name_lbl.setWordWrap(False)
        row_layout.addWidget(name_lbl, 1)

        btn_style = (
            f"QPushButton {{ background:{t.accent}33; color:{t.accent}; border:1px solid {t.accent}55;"
            f" border-radius:4px; padding:2px 6px; font-size:11px; }}"
            f"QPushButton:hover {{ background:{t.accent}; color:#fff; }}"
            f"QPushButton:disabled {{ background:#33333366; color:#666; border-color:#444; }}"
        )

        play_btn = QPushButton("▶")
        play_btn.setFixedWidth(32)
        play_btn.setEnabled(bool(self._player))
        play_btn.setToolTip("Play / Pause")
        play_btn.setStyleSheet(btn_style)
        play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        path = extra.path
        play_btn.clicked.connect(lambda: self._play_audio(path))
        row_layout.addWidget(play_btn)

        open_btn = QPushButton("Open")
        open_btn.setFixedWidth(54)
        open_btn.setToolTip("Open with system player")
        open_btn.setStyleSheet(btn_style)
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.clicked.connect(lambda: _open_path(path))
        row_layout.addWidget(open_btn)

        return row, play_btn

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
        self._media_wrap.hide()
        self._images_wrap.show()
        self._videos_wrap.hide()
        self._music_wrap.hide()
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
            if self._game.installed:
                self.uninstall_requested.emit(self._game)
            else:
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
            self._install_btn.setText("✖  Uninstall")
            _restyle_button(self._install_btn, "#8b2222")
            self._install_btn.setEnabled(True)
            self._download_size_label.hide()
        else:
            self._install_btn.setText("⬇  Retry")
            self._install_btn.setEnabled(True)

    def set_uninstall_done(self, success: bool, message: str = "") -> None:
        if success:
            self._installed_label.setText("○ Not installed")
            self._installed_label.setStyleSheet(
                f"color:{themes.current().text_lo}; font-size:11px;")
            self._play_btn.setEnabled(False)
            self._install_btn.setText("⬇  Install")
            _restyle_button(self._install_btn, "#2e7d32")
            self._install_btn.setEnabled(True)
        else:
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
