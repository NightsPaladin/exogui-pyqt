"""
game_list.py — Left-panel game list with thumbnail, search, and filtering.
"""

from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtCore import (
    Qt, QSortFilterProxyModel, QModelIndex, pyqtSignal,
    QAbstractTableModel, QVariant, QSize, QRect, QItemSelectionModel,
)
from PyQt6.QtGui import QPixmap, QIcon, QColor, QPainter, QFont, QPalette, QPen
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
    QListView, QComboBox, QLabel, QSizePolicy,
    QStyledItemDelegate, QStyleOptionViewItem, QApplication, QStyle,
    QFrame, QStackedWidget, QPushButton,
    QTreeView, QHeaderView, QAbstractItemView,
)

from core.game_library import Game, GameLibrary
from core.image_cache import ImageCache
from gui import themes


THUMB_W, THUMB_H = 60, 80
ROW_HEIGHT = 90
PLACEHOLDER_COLOR = QColor("#2a2a3a")   # fallback image bg — fixed
INSTALLED_DOT     = QColor("#4caf50")
NOT_INSTALLED_DOT = QColor("#666666")

# Grid view dimensions
# TODO: future — expose these as user-adjustable zoom level (like the reference eXoGUI's
#   bottom-right percentage slider) so tile size, image size, and font size all scale
#   together.  The ImageCache scaled_to size and CenteredGridView._adjust_grid() would
#   need to derive from the zoom factor instead of these fixed constants.
GRID_IMG_H    = 140                             # image area height inside the tile
GRID_TOP_PAD  = 16                              # space above image (dot lives here; dot ends at y+13 → 3px gap)
GRID_TITLE_H  = 34                              # title bar height below image (fits ~2 lines at 11pt)
GRID_CELL_W   = GRID_TOP_PAD + GRID_IMG_H + GRID_TITLE_H   # 190 minimum
GRID_CELL_H   = GRID_CELL_W                    # square tile
GRID_MIN_SPACING = 4                            # minimum px of space on each side of a tile

# Cache at cell-width × 1.5× so that 2:3 portrait art is already GRID_CELL_W px wide
# in the cache — paint only needs to scale DOWN, which is sharp, never blurry.
GRID_THUMB_W  = GRID_CELL_W                    # 190
GRID_THUMB_H  = int(GRID_CELL_W * 1.5)        # 285  (portrait 2:3 cached at full display width)

# View mode indices
VIEW_LIST  = 0
VIEW_GRID  = 1
VIEW_TABLE = 2


# ── preset category definitions ───────────────────────────────────────────────

# (display_name, field, value_fragment)
# field: 'series' → series contains value; 'source' → source equals value
PRESETS: list[tuple[str, str, str]] = [
    ("All Games",                          "",        ""),
    ("Quality Freeware",                   "source",  "Freeware"),
    ("eXoDOS 3dfx Games",                  "series",  "Playlist: 3DFX"),
    ("eXoDOS Games with CGA Composite",    "series",  "Playlist: CGA Composite"),
    ("eXoDOS Games with Gravis UltraSound","series",  "Playlist: Gravis Ultrasound"),
    ("eXoDOS Games with IBM Feature Card", "series",  "Playlist: IBM Music Feature Card"),
    ("eXoDOS Games with MT-32",            "series",  "Playlist: Roland MT-32"),
    ("eXoDOS Games with Printer Support",  "series",  "Playlist: Printer Support"),
    ("eXoDOS Games with Sound Canvas",     "series",  "Playlist: Sound Canvas"),
    ("eXoDOS PCjr Games",                  "series",  "Playlist: PCjr"),
    ("eXoDOS REEL Magic Games",            "series",  "Playlist: REEL Magic"),
    ("eXoDOS Remote Multiplayer",          "series",  "Playlist: Remote Multiplayer"),
]

# Lookup: display_name → (field, value)
_PRESET_MAP: dict[str, tuple[str, str]] = {
    name: (field, value) for name, field, value in PRESETS
}


# ── list model ────────────────────────────────────────────────────────────────

class GameListModel(QAbstractTableModel):
    """Table model backed by a list of Game objects.

    Columns: Title | Year | Developer | Publisher | Genre
    Column 0 also carries a small installed-status dot via DecorationRole.
    """

    GAME_ROLE = Qt.ItemDataRole.UserRole + 1

    _HEADERS = ("Title", "Year", "Developer", "Publisher", "Genre")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._games: list[Game] = []
        self._dot_installed     = self._make_dot(QColor("#4caf50"))
        self._dot_not_installed = self._make_dot(QColor("#666666"))

    @staticmethod
    def _make_dot(color: QColor) -> QPixmap:
        pm = QPixmap(10, 10)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(1, 1, 8, 8)
        p.end()
        return pm

    def set_games(self, games: list[Game]) -> None:
        self.beginResetModel()
        self._games = games
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._games)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self._HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            if 0 <= section < len(self._HEADERS):
                return self._HEADERS[section]
        return QVariant()

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._games):
            return QVariant()
        game = self._games[index.row()]
        if role == self.GAME_ROLE:
            return game
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (game.title, game.display_year, game.developer,
                    game.publisher, game.first_genre)[col]
        if role == Qt.ItemDataRole.DecorationRole and col == 0:
            return self._dot_installed if game.installed else self._dot_not_installed
        return QVariant()

    def game_at(self, row: int) -> Optional[Game]:
        if 0 <= row < len(self._games):
            return self._games[row]
        return None


# ── delegate ─────────────────────────────────────────────────────────────────

class GameItemDelegate(QStyledItemDelegate):
    """Custom painter: thumbnail | title + year + genre | installed dot."""

    def __init__(self, image_cache: ImageCache, fallback_path: str, parent=None):
        super().__init__(parent)
        self._cache = image_cache
        self._placeholder = self._make_placeholder(fallback_path)

    def _make_placeholder(self, fallback_path: str) -> QPixmap:
        pm = QPixmap(fallback_path)
        if pm.isNull():
            pm = QPixmap(THUMB_W, THUMB_H)
            pm.fill(PLACEHOLDER_COLOR)
        return pm.scaled(THUMB_W, THUMB_H,
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex) -> None:
        game: Optional[Game] = index.data(GameListModel.GAME_ROLE)
        if not game:
            super().paint(painter, option, index)
            return

        painter.save()
        rect = option.rect
        pal  = option.palette
        R, G = QPalette.ColorRole, QPalette.ColorGroup
        is_sel = bool(option.state & QStyle.StateFlag.State_Selected)

        # Background
        if is_sel:
            painter.fillRect(rect, pal.color(G.Active, R.Highlight))
        elif index.row() % 2 == 0:
            painter.fillRect(rect, pal.color(G.Normal, R.Base))
        else:
            painter.fillRect(rect, pal.color(G.Normal, R.AlternateBase))

        pad = 6

        # Thumbnail
        thumb_rect = rect.adjusted(pad, pad, 0, -pad)
        thumb_rect.setWidth(THUMB_W)
        thumb_rect.setHeight(THUMB_H)

        box_front = game.image_paths.get("box_front")
        shots = game.image_paths.get("screenshots", [])
        cover_path = box_front or (shots[0] if shots else None)
        pm = None
        if cover_path:
            pm = self._cache.get(
                cover_path,
                callback=lambda *_: None,
                scaled_to=(THUMB_W, THUMB_H),
            )
        painter.drawPixmap(thumb_rect, pm if pm and not pm.isNull() else self._placeholder)

        # Text area
        text_x = rect.left() + THUMB_W + pad * 2
        text_w = rect.width() - THUMB_W - pad * 3 - 16
        text_top = rect.top() + pad

        fg_primary   = pal.color(G.Active, R.HighlightedText) if is_sel else pal.color(G.Normal, R.Text)
        fg_secondary = pal.color(G.Normal, R.PlaceholderText)

        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(fg_primary)
        painter.drawText(text_x, text_top, text_w, 20,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         game.title)

        # Warning indicator for games with limited platform support
        if game.compat_note:
            warn_font = QFont()
            warn_font.setPointSize(9)
            painter.setFont(warn_font)
            painter.setPen(QColor("#ffcc44"))
            painter.drawText(text_x, text_top + 20, text_w, 14,
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             "⚠ Limited platform support")

        meta_font = QFont()
        meta_font.setPointSize(9)
        painter.setFont(meta_font)
        painter.setPen(fg_secondary)
        meta = " · ".join(filter(None, [game.display_year, game.first_genre]))
        meta_offset = 36 if game.compat_note else 22
        painter.drawText(text_x, text_top + meta_offset, text_w, 18,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, meta)

        emu_text = game.emulator_display
        painter.drawText(text_x, text_top + meta_offset + 18, text_w, 16,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         emu_text)

        # Installed indicator dot
        dot_color = INSTALLED_DOT if game.installed else NOT_INSTALLED_DOT
        painter.setBrush(dot_color)
        painter.setPen(Qt.PenStyle.NoPen)
        dot_x = rect.right() - 14
        dot_y = rect.top() + (rect.height() - 8) // 2
        painter.drawEllipse(dot_x, dot_y, 8, 8)

        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), ROW_HEIGHT)


# ── grid delegate ─────────────────────────────────────────────────────────────

class CenteredGridView(QListView):
    """QListView (IconMode) that distributes horizontal space evenly so tiles
    fill the full width and reflow naturally when the window resizes.

    Rather than fighting with setSpacing() (which Qt only applies on the
    left/between items, leaving a ragged right edge), we expand the grid cell
    width so that cols × cell_w == viewport_width.  The delegate already
    centres art and title within whatever rect it receives, so wider cells
    look correct without any other changes.
    """

    _FIXED_SPACING = GRID_MIN_SPACING   # constant px gap on each side

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._adjust_grid()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._adjust_grid()

    def _adjust_grid(self) -> None:
        avail = self.viewport().width()
        if avail <= 0:
            return
        s = self._FIXED_SPACING
        # How many columns fit at the minimum cell width?
        cols = max(1, avail // (GRID_CELL_W + s * 2))
        # Expand cell width so the row fills the viewport exactly; cell is square
        cell_w = max(GRID_CELL_W, (avail - s * 2 * cols) // cols)
        new_size = QSize(cell_w, cell_w)   # square
        if self.gridSize() != new_size:
            self.setGridSize(new_size)
        if self.spacing() != s:
            self.setSpacing(s)


class GridItemDelegate(QStyledItemDelegate):
    """Painter for icon/grid mode: large cover art + title below."""

    def __init__(self, image_cache: ImageCache, fallback_path: str, parent=None):
        super().__init__(parent)
        self._cache = image_cache
        self._placeholder = self._make_placeholder(fallback_path)

    def _make_placeholder(self, fallback_path: str) -> QPixmap:
        pm = QPixmap(fallback_path)
        if pm.isNull():
            pm = QPixmap(GRID_THUMB_W, GRID_THUMB_H)
            pm.fill(PLACEHOLDER_COLOR)
        return pm.scaled(GRID_THUMB_W, GRID_THUMB_H,
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex) -> None:
        game: Optional[Game] = index.data(GameListModel.GAME_ROLE)
        if not game:
            super().paint(painter, option, index)
            return

        painter.save()
        rect = option.rect
        pal  = option.palette
        R, G = QPalette.ColorRole, QPalette.ColorGroup
        is_sel = bool(option.state & QStyle.StateFlag.State_Selected)

        # ── Background ───────────────────────────────────────────────────────
        if is_sel:
            painter.fillRect(rect, pal.color(G.Active, R.Highlight))
        else:
            painter.fillRect(rect, pal.color(G.Normal, R.Base))

        # ── Cover art — fills img_area (crops to fit, no blank bars) ───────────
        img_area = QRect(rect.left() + 2, rect.top() + GRID_TOP_PAD,
                         rect.width() - 4, GRID_IMG_H)

        box_front = game.image_paths.get("box_front")
        shots = game.image_paths.get("screenshots", [])
        cover_path = box_front or (shots[0] if shots else None)
        pm = None
        if cover_path:
            pm = self._cache.get(
                cover_path,
                callback=lambda *_: None,
                scaled_to=(GRID_THUMB_W, GRID_THUMB_H),
            )

        src = pm if (pm and not pm.isNull()) else self._placeholder
        # Scale to fill the entire img_area (KeepAspectRatioByExpanding crops
        # the excess rather than leaving blank bars on portrait or landscape art)
        scaled = src.scaled(img_area.size(),
                            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation)
        painter.save()
        painter.setClipRect(img_area)
        painter.drawPixmap(
            img_area.left() + (img_area.width()  - scaled.width())  // 2,
            img_area.top()  + (img_area.height() - scaled.height()) // 2,
            scaled,
        )
        painter.restore()

        # ── Title bar below image (tinted background, always readable) ───────
        title_rect = QRect(rect.left() + 2,
                           rect.top() + GRID_TOP_PAD + GRID_IMG_H,
                           rect.width() - 4,
                           GRID_TITLE_H - 2)
        title_bg = QColor(0, 0, 0, 160) if not is_sel else QColor(0, 0, 0, 70)
        painter.fillRect(title_rect, title_bg)
        title_font = QFont()
        title_font.setPointSize(11)
        painter.setFont(title_font)
        painter.setPen(QColor(255, 255, 255, 230))
        painter.drawText(title_rect,
                         Qt.AlignmentFlag.AlignHCenter
                         | Qt.AlignmentFlag.AlignVCenter
                         | Qt.TextFlag.TextWordWrap,
                         game.title)

        # ── Border — drawn last so it always sits on top of all content ──────
        border_color = QColor(themes.current().accent if is_sel else themes.current().border)
        pen = QPen(border_color)
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 4, 4)

        # ── Installed dot (top-right, inside the header pad) ─────────────────
        dot_color = INSTALLED_DOT if game.installed else NOT_INSTALLED_DOT
        painter.setBrush(dot_color)
        painter.setPen(QPen(QColor(themes.current().bg_panel), 1.5))
        painter.drawEllipse(rect.right() - 13, rect.top() + 5, 8, 8)

        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(GRID_CELL_W, GRID_CELL_W)   # square


class GameFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._genre_filter    = ""
        self._year_filter     = 0
        self._installed_only  = False
        self._preset_field    = ""   # "series" | "source" | ""
        self._preset_value    = ""   # substring to match
        self._rating_filter   = ""
        self._play_mode_filter= ""

    def set_genre_filter(self, genre: str) -> None:
        self._genre_filter = genre.lower()
        self.invalidateFilter()

    def set_year_filter(self, year: int) -> None:
        self._year_filter = year
        self.invalidateFilter()

    def set_installed_only(self, installed_only: bool) -> None:
        self._installed_only = installed_only
        self.invalidateFilter()

    def set_preset(self, name: str) -> None:
        field, value = _PRESET_MAP.get(name, ("", ""))
        self._preset_field = field
        self._preset_value = value
        self.invalidateFilter()

    def set_rating_filter(self, rating: str) -> None:
        self._rating_filter = rating
        self.invalidateFilter()

    def set_play_mode_filter(self, mode: str) -> None:
        self._play_mode_filter = mode.lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model: GameListModel = self.sourceModel()
        game = model.game_at(source_row)
        if not game:
            return False
        if self._installed_only and not game.installed:
            return False
        if self._genre_filter and self._genre_filter not in game.genre.lower():
            return False
        if self._year_filter and game.release_year != self._year_filter:
            return False
        if self._rating_filter and game.rating != self._rating_filter:
            return False
        if self._play_mode_filter and self._play_mode_filter not in game.play_mode.lower():
            return False
        if self._preset_field:
            if self._preset_field == "series":
                if self._preset_value.lower() not in game.series.lower():
                    return False
            elif self._preset_field == "source":
                if game.source != self._preset_value:
                    return False
        # Text filter from parent
        return super().filterAcceptsRow(source_row, source_parent)


# ── panel widget ─────────────────────────────────────────────────────────────

class GameListPanel(QWidget):
    """
    Left-side panel: search bar + filter combos + the game list view.
    Emits *game_selected* when the user clicks a game.
    """

    game_selected = pyqtSignal(object)  # Game

    def __init__(self, library: GameLibrary, image_cache: ImageCache,
                 exodos_root: str, parent=None):
        super().__init__(parent)
        self._library    = library
        self._cache      = image_cache
        self._fallback   = os.path.join(exodos_root, "eXo", "util", "exodos.png")

        self._source_model = GameListModel()
        self._proxy_model  = GameFilterProxyModel()
        self._proxy_model.setSourceModel(self._source_model)
        self._proxy_model.setFilterRole(Qt.ItemDataRole.DisplayRole)
        self._proxy_model.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        self._build_ui()
        self._populate()

        # Refresh thumbnails when an image finishes loading
        self._cache.image_ready.connect(self._on_image_ready)

    # ── ui ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        # Search bar + view mode buttons
        search_row = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍  Search games…")
        self._search.textChanged.connect(self._on_search)
        self._search.setStyleSheet(
            "QLineEdit { border-radius:4px; padding:4px 8px; font-size:13px; }"
        )
        search_row.addWidget(self._search)

        self._view_btns: list[QPushButton] = []
        for icon, mode, tip in [("≡", VIEW_LIST, "List view"),
                                 ("⊞", VIEW_GRID,  "Grid view"),
                                 ("☰", VIEW_TABLE, "Table view")]:
            btn = QPushButton(icon)
            btn.setFixedSize(28, 28)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda checked, m=mode: self._switch_view(m))
            self._view_btns.append(btn)
            search_row.addWidget(btn)
        self._view_btns[VIEW_LIST].setChecked(True)

        layout.addLayout(search_row)

        # ── Preset categories ──────────────────────────────────────────────
        self._preset_combo = QComboBox()
        self._preset_combo.addItems([name for name, _, _ in PRESETS])
        self._preset_combo.currentTextChanged.connect(self._on_preset_filter)
        self._preset_combo.setToolTip("Filter by preset category")
        layout.addWidget(self._preset_combo)

        # Thin divider
        self._filter_sep = QFrame()
        self._filter_sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(self._filter_sep)

        # ── Genre + Year ───────────────────────────────────────────────────
        row1 = QHBoxLayout()
        self._genre_combo = QComboBox()
        self._genre_combo.addItem("All genres")
        self._genre_combo.currentTextChanged.connect(self._on_genre_filter)
        self._genre_combo.setToolTip("Filter by genre")
        row1.addWidget(self._genre_combo, 3)

        self._year_combo = QComboBox()
        self._year_combo.addItem("All years")
        self._year_combo.currentTextChanged.connect(self._on_year_filter)
        self._year_combo.setToolTip("Filter by release year")
        row1.addWidget(self._year_combo, 2)
        layout.addLayout(row1)

        # ── Rating + Play Mode ─────────────────────────────────────────────
        row2 = QHBoxLayout()
        self._rating_combo = QComboBox()
        self._rating_combo.addItem("All ratings")
        self._rating_combo.currentTextChanged.connect(self._on_rating_filter)
        self._rating_combo.setToolTip("Filter by age rating (ESRB-style)")
        row2.addWidget(self._rating_combo, 3)

        self._play_mode_combo = QComboBox()
        self._play_mode_combo.addItem("All modes")
        self._play_mode_combo.currentTextChanged.connect(self._on_play_mode_filter)
        self._play_mode_combo.setToolTip("Filter by play mode")
        row2.addWidget(self._play_mode_combo, 2)
        layout.addLayout(row2)

        # ── Installed toggle ───────────────────────────────────────────────
        row3 = QHBoxLayout()
        self._show_label = QLabel("Show:")
        self._show_label.setStyleSheet("font-size:11px;")
        row3.addWidget(self._show_label)
        self._installed_combo = QComboBox()
        self._installed_combo.addItems(["All games", "Installed only"])
        self._installed_combo.currentIndexChanged.connect(self._on_installed_filter)
        row3.addWidget(self._installed_combo, 1)
        layout.addLayout(row3)

        # Count label
        self._count_label = QLabel()
        self._count_label.setStyleSheet("font-size:10px; padding: 2px 4px;")
        layout.addWidget(self._count_label)

        # ── Stacked views (List / Grid / Table) ────────────────────────────
        # All three views share one proxy model and one selection model so that
        # filtering, selection, and keyboard navigation stay in sync.
        self._shared_selection = QItemSelectionModel(self._proxy_model)
        self._shared_selection.currentChanged.connect(self._on_selection_changed)

        self._view_stack = QStackedWidget()

        self._list_view = self._build_list_view()
        self._view_stack.addWidget(self._list_view)          # VIEW_LIST  = 0

        self._grid_view = self._build_grid_view()
        self._view_stack.addWidget(self._grid_view)          # VIEW_GRID  = 1

        table_container = self._build_table_container()
        self._view_stack.addWidget(table_container)          # VIEW_TABLE = 2

        layout.addWidget(self._view_stack, 1)

        # Apply initial theme stylesheets
        self.apply_theme()

    def apply_theme(self) -> None:
        """Re-apply theme-dependent stylesheets to all widgets."""
        t = themes.current()
        self.setStyleSheet(f"background:{t.bg_panel};")

        # Search bar
        self._search.setStyleSheet(
            f"QLineEdit {{ background:{t.bg_input}; color:{t.text_hi};"
            f"border:1px solid {t.border}; border-radius:4px;"
            f"padding:4px 8px; font-size:13px; }}"
        )

        # View toggle buttons
        for btn in self._view_btns:
            btn.setStyleSheet(
                f"QPushButton {{ background:{t.bg_input}; color:{t.text_med};"
                f"border:1px solid {t.border}; border-radius:4px; font-size:14px; }}"
                f"QPushButton:checked {{ background:{t.accent}; color:#fff;"
                f"border-color:{t.accent}; }}"
                f"QPushButton:hover {{ border-color:{t.accent}; }}"
            )

        # Separator
        self._filter_sep.setStyleSheet(f"color:{t.border};")

        # Combo boxes
        combo_qss = self._combo_style(t)
        for combo in (self._preset_combo, self._genre_combo, self._year_combo,
                      self._rating_combo, self._play_mode_combo,
                      self._installed_combo):
            combo.setStyleSheet(combo_qss)

        # "Show:" label
        self._show_label.setStyleSheet(f"color:{t.text_lo}; font-size:11px;")

        # Count label
        self._count_label.setStyleSheet(
            f"color:{t.text_lo}; font-size:10px; padding:2px 4px;"
        )

        # List view
        list_qss = (
            f"QListView {{ background:{t.bg_panel}; border:none; outline:none; }}"
            f"QScrollBar:vertical {{ background:{t.bg_panel}; width:8px; border:none; }}"
            f"QScrollBar::handle:vertical {{ background:{t.handle}; border-radius:4px;"
            f"min-height:20px; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
        )
        self._list_view.setStyleSheet(list_qss)
        self._grid_view.setStyleSheet(list_qss)

        # Table view (QTreeView inside the container)
        table_container = self._view_stack.widget(VIEW_TABLE)
        if table_container:
            tree_qss = (
                f"QTreeView {{ background:{t.bg_panel}; border:none; outline:none;"
                f" alternate-background-color:{t.bg_card}; }}"
                f"QHeaderView::section {{ background:{t.bg_panel}; color:{t.text_lo};"
                f" border:none; border-bottom:1px solid {t.border};"
                f" padding:3px 4px; font-size:9pt; font-weight:bold; }}"
                f"QScrollBar:vertical {{ background:{t.bg_panel}; width:8px; border:none; }}"
                f"QScrollBar::handle:vertical {{ background:{t.handle}; border-radius:4px; min-height:20px; }}"
                "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
                f"QScrollBar:horizontal {{ background:{t.bg_panel}; height:8px; border:none; }}"
                f"QScrollBar::handle:horizontal {{ background:{t.handle}; border-radius:4px; min-width:20px; }}"
                "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; }"
            )
            for child in table_container.findChildren(QTreeView):
                child.setStyleSheet(tree_qss)

        # Trigger repaint of visible view
        self._list_view.viewport().update()
        self._grid_view.viewport().update()
        if self._table_view:
            self._table_view.viewport().update()

    @staticmethod
    def _combo_style(t=None) -> str:
        if t is None:
            from gui import themes as _th
            t = _th.current()
        return (
            f"QComboBox {{ background:{t.bg_input}; color:{t.text_hi}; border:1px solid {t.border};"
            f"border-radius:4px; padding:3px 6px; font-size:11px; }}"
            f"QComboBox::drop-down {{ border:none; }}"
            f"QComboBox QAbstractItemView {{ background:{t.bg_input}; color:{t.text_hi}; }}"
        )

    # ── view builders ────────────────────────────────────────────────────────

    def _build_list_view(self) -> QListView:
        view = QListView()
        view.setModel(self._proxy_model)
        view.setSelectionModel(self._shared_selection)
        view.setItemDelegate(GameItemDelegate(self._cache, self._fallback, view))
        view.setUniformItemSizes(True)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return view

    def _build_grid_view(self) -> CenteredGridView:
        view = CenteredGridView()
        view.setModel(self._proxy_model)
        view.setSelectionModel(self._shared_selection)
        view.setViewMode(QListView.ViewMode.IconMode)
        view.setResizeMode(QListView.ResizeMode.Adjust)
        view.setMovement(QListView.Movement.Static)
        view.setUniformItemSizes(True)
        # Initial grid size — _adjust_grid() will override this on first show
        view.setGridSize(QSize(GRID_CELL_W, GRID_CELL_W))
        view.setSpacing(GRID_MIN_SPACING)
        view.setItemDelegate(GridItemDelegate(self._cache, self._fallback, view))
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return view

    def _build_table_container(self) -> QWidget:
        container = QWidget()
        vbox = QVBoxLayout(container)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        self._table_view = QTreeView()
        self._table_view.setModel(self._proxy_model)
        self._table_view.setSelectionModel(self._shared_selection)
        self._table_view.setRootIsDecorated(False)
        self._table_view.setItemsExpandable(False)
        self._table_view.setUniformRowHeights(True)
        self._table_view.setAlternatingRowColors(True)
        self._table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        hdr = self._table_view.header()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(True)
        # Sensible default column widths
        self._table_view.setColumnWidth(0, 300)   # Title
        self._table_view.setColumnWidth(1, 55)    # Year
        self._table_view.setColumnWidth(2, 160)   # Developer
        self._table_view.setColumnWidth(3, 160)   # Publisher
        # Genre stretches to fill

        vbox.addWidget(self._table_view, 1)
        return container

    def _switch_view(self, mode: int) -> None:
        self._view_stack.setCurrentIndex(mode)
        for i, btn in enumerate(self._view_btns):
            btn.setChecked(i == mode)
        # Scroll the newly visible view to the current selection
        cur = self._shared_selection.currentIndex()
        if cur.isValid():
            views = [self._list_view, self._grid_view, self._table_view]
            views[mode].scrollTo(cur, QAbstractItemView.ScrollHint.EnsureVisible)

    # ── populate ─────────────────────────────────────────────────────────────

    def _populate(self) -> None:
        self._source_model.set_games(self._library.games)
        self._update_count()

        self._genre_combo.blockSignals(True)
        while self._genre_combo.count() > 1:
            self._genre_combo.removeItem(1)
        for genre in self._library.all_genres():
            self._genre_combo.addItem(genre)
        self._genre_combo.blockSignals(False)

        self._year_combo.blockSignals(True)
        while self._year_combo.count() > 1:
            self._year_combo.removeItem(1)
        for year in self._library.all_years():
            self._year_combo.addItem(str(year))
        self._year_combo.blockSignals(False)

        self._rating_combo.blockSignals(True)
        while self._rating_combo.count() > 1:
            self._rating_combo.removeItem(1)
        for rating in self._library.all_ratings():
            self._rating_combo.addItem(rating)
        self._rating_combo.blockSignals(False)

        self._play_mode_combo.blockSignals(True)
        while self._play_mode_combo.count() > 1:
            self._play_mode_combo.removeItem(1)
        for mode in self._library.all_play_modes():
            self._play_mode_combo.addItem(mode)
        self._play_mode_combo.blockSignals(False)

    def _update_count(self) -> None:
        total = self._proxy_model.rowCount()
        self._count_label.setText(f"{total:,} games")

    # ── slots ─────────────────────────────────────────────────────────────────

    def _on_search(self, text: str) -> None:
        self._proxy_model.setFilterFixedString(text)
        self._update_count()

    def _on_preset_filter(self, name: str) -> None:
        self._proxy_model.set_preset(name)
        self._update_count()

    def _on_genre_filter(self, text: str) -> None:
        if text == "All genres":
            self._proxy_model.set_genre_filter("")
        else:
            self._proxy_model.set_genre_filter(text)
        self._update_count()

    def _on_year_filter(self, text: str) -> None:
        try:
            self._proxy_model.set_year_filter(int(text))
        except ValueError:
            self._proxy_model.set_year_filter(0)
        self._update_count()

    def _on_rating_filter(self, text: str) -> None:
        self._proxy_model.set_rating_filter("" if text == "All ratings" else text)
        self._update_count()

    def _on_play_mode_filter(self, text: str) -> None:
        self._proxy_model.set_play_mode_filter("" if text == "All modes" else text)
        self._update_count()

    def _on_installed_filter(self, idx: int) -> None:
        self._proxy_model.set_installed_only(idx == 1)
        self._update_count()

    def _on_selection_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        source_idx = self._proxy_model.mapToSource(current)
        game = self._source_model.game_at(source_idx.row())
        if game:
            self.game_selected.emit(game)

    def _on_image_ready(self, path: str, _pm: QPixmap) -> None:
        views = [self._list_view, self._grid_view, self._table_view]
        views[self._view_stack.currentIndex()].viewport().update()

    # ── public ────────────────────────────────────────────────────────────────

    def select_first(self) -> None:
        if self._proxy_model.rowCount() > 0:
            idx = self._proxy_model.index(0, 0)
            self._shared_selection.setCurrentIndex(
                idx, QItemSelectionModel.SelectionFlag.ClearAndSelect
            )

    def refresh(self) -> None:
        self._source_model.set_games(self._library.games)
        self._update_count()

    def set_library(self, library) -> None:
        """Swap in a new library (e.g. when switching projects) and repopulate."""
        self._library = library
        # Show/hide preset combo based on project type
        is_exodos = getattr(library.config, "id", "exodos") == "exodos"
        self._preset_combo.setVisible(is_exodos)
        self._filter_sep.setVisible(is_exodos)
        self._populate()
        self.select_first()
