"""
main_window.py — Main application window (multi-project aware).
"""

from __future__ import annotations

import json
import os

from PyQt6.QtCore import Qt, QTimer, QSettings, QByteArray, pyqtSlot
from PyQt6.QtGui import QKeySequence, QAction, QActionGroup
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QSplitter, QDialog, QDialogButtonBox, QFormLayout,
    QLineEdit, QLabel, QPushButton, QApplication, QMessageBox,
    QProgressBar, QComboBox, QFileDialog, QScrollArea,
    QGroupBox, QTabBar, QFrame,
)

from core.game_library import GameLibrary, Game
from core.launcher import Launcher
from core.image_cache import ImageCache
from core.project import ProjectConfig, ALL_PROJECTS, detect_project, get_project
from gui.game_list import GameListPanel
from gui.game_detail import GameDetailPanel
from gui import themes


APP_NAME     = "eXoGUI"
APP_VERSION  = "0.2.0"
WINDOW_W     = 1280
WINDOW_H     = 800

# Directory that contains the exogui-pyqt/ package (i.e. the drive root layout).
# All project/ZIP-source paths are stored relative to this so the app is
# portable when the entire drive is remounted at a different path.
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _to_stored_path(path: str) -> str:
    """Convert an absolute path to a stored (relative-to-_APP_DIR) path.

    Relative paths and empty strings pass through unchanged.
    Storing relative paths makes the QSettings portable across drive remounts.
    """
    if not path:
        return path
    try:
        return os.path.relpath(path, _APP_DIR)
    except ValueError:
        # os.path.relpath raises on Windows when paths span different drives;
        # fall back to storing the absolute path in that case.
        return path


def _from_stored_path(path: str) -> str:
    """Resolve a stored path (possibly relative) to an absolute path.

    Absolute paths pass through unchanged (backward-compat with older settings).
    Empty strings pass through unchanged.
    """
    if not path:
        return path
    return os.path.abspath(os.path.join(_APP_DIR, path))


# ── per-project row widget ────────────────────────────────────────────────────

class _ProjectRow(QWidget):
    """One row in the Settings > Projects section."""

    def __init__(self, project_id: str, root: str,
                 zip_source_path: str = "", parent=None):
        super().__init__(parent)
        self.project_id = project_id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(4)

        # ── Row 1: project name, root path, content filter ───────────────────
        row1 = QHBoxLayout()
        row1.setSpacing(8)

        cfg = get_project(project_id)
        name_label = QLabel(cfg.display_name if cfg else project_id)
        name_label.setFixedWidth(96)
        name_label.setStyleSheet("font-weight:bold;")

        self._root_edit = QLineEdit(root)
        self._root_edit.setPlaceholderText("Path to project root…")

        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse_root)

        self._remove_btn = QPushButton("✕")
        self._remove_btn.setFixedWidth(28)
        self._remove_btn.setToolTip("Remove this project")

        row1.addWidget(name_label)
        row1.addWidget(self._root_edit, 1)
        row1.addWidget(browse_btn)
        row1.addWidget(self._remove_btn)

        # ── Row 2: optional ZIP source path ──────────────────────────────────
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        zip_label = QLabel("ZIP Source")
        zip_label.setFixedWidth(96)
        zip_label.setStyleSheet("color:gray; font-size:11px;")

        self._zip_source_edit = QLineEdit(zip_source_path)
        self._zip_source_edit.setPlaceholderText(
            "Optional: folder containing game ZIPs (local drive or NAS)…"
        )
        self._zip_source_edit.setToolTip(
            "Lite mode only. Point this to a directory that already contains\n"
            "the game ZIP files (e.g. an external hard drive or network share).\n"
            "The GUI will copy from here before trying the torrent."
        )

        browse_zip_btn = QPushButton("Browse…")
        browse_zip_btn.setFixedWidth(80)
        browse_zip_btn.clicked.connect(self._browse_zip_source)

        # Spacer to align with remove_btn column above
        spacer = QWidget()
        spacer.setFixedWidth(28)  # remove_btn width

        row2.addWidget(zip_label)
        row2.addWidget(self._zip_source_edit, 1)
        row2.addWidget(browse_zip_btn)
        row2.addWidget(spacer)

        layout.addLayout(row1)
        layout.addLayout(row2)

    def _browse_root(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select project root", self._root_edit.text())
        if d:
            self._root_edit.setText(d)

    def _browse_zip_source(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Select ZIP source folder", self._zip_source_edit.text()
        )
        if d:
            self._zip_source_edit.setText(d)

    @property
    def root(self) -> str:
        return self._root_edit.text().strip()

    @property
    def xml_mode(self) -> str:
        return "auto"

    @property
    def zip_source_path(self) -> str:
        return self._zip_source_edit.text().strip()

    @property
    def remove_button(self) -> QPushButton:
        return self._remove_btn


# ── settings dialog ───────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(720)
        self.setMinimumHeight(420)
        self._settings = settings
        self._project_rows: list[_ProjectRow] = []

        outer = QVBoxLayout(self)
        outer.setSpacing(14)

        # ── Projects ─────────────────────────────────────────────────────────
        projects_box = QGroupBox("Projects")
        projects_layout = QVBoxLayout(projects_box)

        # Column headers
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        hdr_name = QLabel("Project")
        hdr_name.setFixedWidth(96)
        hdr_name.setStyleSheet("font-weight:bold; color:gray; font-size:11px;")
        hdr_path = QLabel("Root Path  /  ZIP Source (Lite mode)")
        hdr_path.setStyleSheet("font-weight:bold; color:gray; font-size:11px;")
        hdr.addWidget(hdr_name)
        hdr.addWidget(hdr_path, 1)
        hdr.addWidget(QLabel(""), 0)       # placeholder for Browse button
        hdr.addWidget(QLabel(""))          # placeholder for Remove button
        projects_layout.addLayout(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        projects_layout.addWidget(sep)

        # Scrollable rows area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumHeight(130)

        self._rows_widget = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(2)
        self._rows_layout.addStretch()
        scroll.setWidget(self._rows_widget)
        projects_layout.addWidget(scroll)

        # Load existing projects — resolve stored (possibly relative) paths to absolute
        raw = settings.value("projects", "[]")
        try:
            projects = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            projects = []
        for p in projects:
            zsp = settings.value(f"project_{p['id']}/zip_source_path", "")
            self._add_row(
                p["id"],
                _from_stored_path(p.get("root", "")),
                _from_stored_path(zsp),
            )

        add_btn = QPushButton("＋  Add Project…")
        add_btn.setFixedWidth(160)
        add_btn.clicked.connect(self._add_project)
        projects_layout.addWidget(add_btn, 0, Qt.AlignmentFlag.AlignLeft)

        outer.addWidget(projects_box)

        # ── Emulator Commands ─────────────────────────────────────────────────
        emu_box = QGroupBox("Emulator Commands")
        emu_form = QFormLayout(emu_box)
        emu_form.setSpacing(8)

        self._staging_edit = QLineEdit(settings.value("dosbox_staging", "dosbox-staging"))
        emu_form.addRow("dosbox-staging:", self._staging_edit)

        self._x_edit = QLineEdit(settings.value("dosbox_x", "dosbox-x"))
        emu_form.addRow("dosbox-x:", self._x_edit)

        self._ece_edit = QLineEdit(settings.value("dosbox_ece", "dosbox-ece"))
        emu_form.addRow("dosbox-ece:", self._ece_edit)

        self._scumm_edit = QLineEdit(settings.value("scummvm", "scummvm"))
        emu_form.addRow("scummvm:", self._scumm_edit)

        outer.addWidget(emu_box)

        # ── Buttons ───────────────────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ── internal helpers ──────────────────────────────────────────────────────

    def _add_row(self, project_id: str, root: str,
                 zip_source_path: str = "") -> None:
        row = _ProjectRow(project_id, root, zip_source_path, self)
        row.remove_button.clicked.connect(lambda: self._remove_row(row))
        idx = self._rows_layout.count() - 1   # insert before stretch
        self._rows_layout.insertWidget(idx, row)
        self._project_rows.append(row)

    def _remove_row(self, row: _ProjectRow) -> None:
        self._rows_layout.removeWidget(row)
        row.deleteLater()
        self._project_rows.remove(row)

    def _add_project(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select project root")
        if not d:
            return
        cfg = detect_project(d)
        if cfg is None:
            QMessageBox.warning(
                self, "Unknown project",
                f"Could not detect an eXo project at:\n{d}\n\n"
                "Make sure this is the root of an eXoDOS or eXoWin3x installation."
            )
            return
        for row in self._project_rows:
            if row.project_id == cfg.id:
                QMessageBox.information(
                    self, "Already added",
                    f"{cfg.display_name} is already in the projects list."
                )
                return
        self._add_row(cfg.id, d)

    def _save(self) -> None:
        projects = [
            {"id": r.project_id, "root": _to_stored_path(r.root)}
            for r in self._project_rows
        ]
        self._settings.setValue("projects", json.dumps(projects))
        for row in self._project_rows:
            self._settings.setValue(
                f"project_{row.project_id}/zip_source_path",
                _to_stored_path(row.zip_source_path),
            )
        self._settings.setValue("dosbox_staging", self._staging_edit.text())
        self._settings.setValue("dosbox_x",       self._x_edit.text())
        self._settings.setValue("dosbox_ece",      self._ece_edit.text())
        self._settings.setValue("scummvm",         self._scumm_edit.text())
        self.accept()


# ── loading overlay ───────────────────────────────────────────────────────────

class LoadingWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._label = QLabel("Loading…")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._sub = QLabel("")
        self._sub.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._bar = QProgressBar()
        self._bar.setRange(0, 0)
        self._bar.setFixedWidth(300)

        layout.addWidget(self._label)
        layout.addWidget(self._sub)
        layout.addWidget(self._bar, 0, Qt.AlignmentFlag.AlignCenter)

        self._apply_theme()

    def _apply_theme(self) -> None:
        t = themes.current()
        self.setStyleSheet(f"background:{t.bg_window};")
        self._label.setStyleSheet(
            f"color:{t.text_hi}; font-size:18px; font-weight:bold;"
        )
        self._sub.setStyleSheet(f"color:{t.text_med}; font-size:13px;")
        self._bar.setStyleSheet(
            f"QProgressBar {{ background:{t.bg_input}; border:1px solid {t.border};"
            f" border-radius:4px; }}"
            f"QProgressBar::chunk {{ background:{t.accent}; border-radius:4px; }}"
        )

    def set_label(self, text: str) -> None:
        self._label.setText(text)

    def set_status(self, text: str) -> None:
        self._sub.setText(text)


# ── main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, fallback_root: str = ""):
        super().__init__()
        _settings_path = os.path.join(_APP_DIR, "exogui.ini")
        self._settings = QSettings(_settings_path, QSettings.Format.IniFormat)

        # Migrate old single-project settings if needed
        self._migrate_settings(fallback_root)

        # Load projects list
        raw = self._settings.value("projects", "[]")
        try:
            self._projects: list[dict] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self._projects = []

        # Pick active project id
        default_id = self._projects[0]["id"] if self._projects else ""
        self._active_id: str = self._settings.value("active_project", default_id)
        if not any(p["id"] == self._active_id for p in self._projects):
            self._active_id = default_id

        self._libraries: dict[str, GameLibrary] = {}
        self._launchers: dict[str, Launcher]    = {}

        self.setWindowTitle(f"{APP_NAME}  v{APP_VERSION}")
        self.resize(WINDOW_W, WINDOW_H)

        saved_theme = self._settings.value("theme", "System")
        themes.set_theme(saved_theme, QApplication.instance())

        geom: QByteArray | None = self._settings.value("window/geometry")
        if geom:
            self.restoreGeometry(geom)

        self._cache = ImageCache(max_size=600)

        self._loading = LoadingWidget(self)
        self.setCentralWidget(self._loading)

        self._build_menu()
        self._build_status_bar()

        QTimer.singleShot(100, self._load_active_project)

    # ── settings migration ────────────────────────────────────────────────────

    def _migrate_settings(self, fallback_root: str = "") -> None:
        """Migrate old single-project (exodos_root) settings to new format."""
        if self._settings.value("projects") is not None:
            return
        old_root = self._settings.value("exodos_root", fallback_root)
        if old_root:
            projects = [{"id": "exodos", "root": old_root}]
            self._settings.setValue("projects",       json.dumps(projects))
            self._settings.setValue("active_project", "exodos")
            self._settings.remove("exodos_root")
            self._settings.remove("xml_mode")
        else:
            self._settings.setValue("projects", "[]")

    # ── project helpers ───────────────────────────────────────────────────────

    def _project_entry(self, project_id: str) -> tuple[ProjectConfig | None, str]:
        """Return (ProjectConfig, root) for a project id."""
        for p in self._projects:
            if p["id"] == project_id:
                return get_project(project_id), _from_stored_path(p.get("root", ""))
        return None, ""

    def _make_library(self, project_id: str) -> GameLibrary | None:
        cfg, root = self._project_entry(project_id)
        if not cfg or not root:
            return None
        xml_mode = "auto"
        return GameLibrary(root, xml_mode=xml_mode, config=cfg)

    def _make_launcher(self, project_id: str) -> Launcher | None:
        cfg, root = self._project_entry(project_id)
        if not cfg or not root:
            return None
        zip_source = _from_stored_path(
            self._settings.value(f"project_{project_id}/zip_source_path", "")
        )
        return Launcher(root, config=cfg, zip_source_path=zip_source, parent=self)

    def _connect_launcher(self, launcher: Launcher) -> None:
        launcher.launch_started.connect(self._on_launch_started)
        launcher.launch_finished.connect(self._on_launch_finished)
        launcher.launch_error.connect(self._on_launch_error)
        launcher.install_progress.connect(self._on_install_progress)
        launcher.install_finished.connect(self._on_install_finished)
        launcher.fetch_phase.connect(self._on_fetch_phase)
        launcher.fetch_progress.connect(self._on_fetch_progress)
        launcher.fetch_finished.connect(self._on_fetch_finished)
        launcher.fetch_cancelled.connect(self._on_fetch_cancelled)

    @property
    def _library(self) -> GameLibrary | None:
        return self._libraries.get(self._active_id)

    @property
    def _launcher(self) -> Launcher | None:
        return self._launchers.get(self._active_id)

    # ── library loading ───────────────────────────────────────────────────────

    def _load_active_project(self) -> None:
        if not self._projects:
            self._show_no_project_ui()
            return

        if not self._active_id:
            self._active_id = self._projects[0]["id"]

        cfg, root = self._project_entry(self._active_id)
        display = cfg.display_name if cfg else self._active_id
        self._loading.set_label(f"Loading {display}…")
        self._loading.set_status("Parsing catalogue…")
        QApplication.processEvents()

        lib = self._make_library(self._active_id)
        if lib is None:
            QMessageBox.critical(
                self, "No project configured",
                "Could not find a valid project root. Open Settings to configure."
            )
            self._show_no_project_ui()
            return

        try:
            lib.load()
        except Exception as exc:
            QMessageBox.critical(
                self, "Error loading library",
                f"Failed to load {display} catalogue:\n\n{exc}\n\n"
                f"Check that the root path is correct.\nCurrent path: {root}"
            )
            self._show_no_project_ui()
            return

        self._libraries[self._active_id] = lib

        launcher = self._make_launcher(self._active_id)
        if launcher:
            self._connect_launcher(launcher)
            self._launchers[self._active_id] = launcher

        self._loading.set_status(f"Loaded {len(lib.games):,} games.")
        QApplication.processEvents()

        self._build_main_ui()

    def _show_no_project_ui(self) -> None:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl = QLabel("No eXo project configured.\nOpen Settings to add a project.")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("font-size:16px;")
        btn = QPushButton("Open Settings…")
        btn.setFixedWidth(160)
        btn.clicked.connect(self._open_settings)
        layout.addWidget(lbl)
        layout.addWidget(btn, 0, Qt.AlignmentFlag.AlignCenter)
        self.setCentralWidget(w)
        # On first launch auto-open Settings so the user doesn't see a blank screen.
        QTimer.singleShot(200, self._open_settings)

    # ── build main UI ─────────────────────────────────────────────────────────

    def _build_main_ui(self) -> None:
        lib = self._library
        _, root = self._project_entry(self._active_id)

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Project tab bar (only shown when > 1 project) ─────────────────
        self._project_tabs = QTabBar()
        self._project_tabs.setExpanding(False)
        for p in self._projects:
            pcfg = get_project(p["id"])
            label = pcfg.display_name if pcfg else p["id"]
            self._project_tabs.addTab(label)
        active_idx = next(
            (i for i, p in enumerate(self._projects) if p["id"] == self._active_id), 0
        )
        self._project_tabs.setCurrentIndex(active_idx)
        self._project_tabs.currentChanged.connect(self._on_tab_changed)

        if len(self._projects) > 1:
            outer.addWidget(self._project_tabs)

        # ── Splitter with list + detail ───────────────────────────────────
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setHandleWidth(2)

        self._list_panel   = GameListPanel(lib, self._cache, root)
        self._detail_panel = GameDetailPanel(self._cache, root)

        self._list_panel.game_selected.connect(self._on_game_selected)
        self._detail_panel.play_requested.connect(self._on_play_requested)
        self._detail_panel.install_requested.connect(self._on_install_requested)
        self._detail_panel.cancel_requested.connect(self._on_cancel_requested)

        self._splitter.addWidget(self._list_panel)
        self._splitter.addWidget(self._detail_panel)

        saved_split: QByteArray | None = self._settings.value("window/splitter")
        if saved_split:
            self._splitter.restoreState(saved_split)
        else:
            w = self.width() or WINDOW_W
            self._splitter.setSizes([w // 2, w // 2])

        outer.addWidget(self._splitter)
        self.setCentralWidget(central)

        if lib:
            total = len(lib.games)
            installed = len(lib.filter_installed())
            cfg = get_project(self._active_id)
            name = cfg.display_name if cfg else self._active_id
            self._set_status(f"{name}: {total:,} games  ·  {installed:,} installed")

        self._list_panel.select_first()

    # ── project switching ─────────────────────────────────────────────────────

    @pyqtSlot(int)
    def _on_tab_changed(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._projects):
            return
        project_id = self._projects[idx]["id"]
        if project_id != self._active_id:
            self._switch_project(project_id)

    def _switch_project(self, project_id: str) -> None:
        self._active_id = project_id
        self._settings.setValue("active_project", project_id)

        # Load and cache library if not already loaded
        if project_id not in self._libraries:
            cfg = get_project(project_id)
            display = cfg.display_name if cfg else project_id
            self._set_status(f"Loading {display}…")
            QApplication.processEvents()

            lib = self._make_library(project_id)
            if lib is None:
                QMessageBox.warning(
                    self, "Project error",
                    f"No root path configured for: {project_id}"
                )
                return
            try:
                lib.load()
            except Exception as exc:
                QMessageBox.critical(
                    self, "Error loading library",
                    f"Failed to load {display}:\n{exc}"
                )
                return
            self._libraries[project_id] = lib

            launcher = self._make_launcher(project_id)
            if launcher:
                self._connect_launcher(launcher)
                self._launchers[project_id] = launcher

        lib = self._libraries[project_id]
        _, root = self._project_entry(project_id)

        # Update detail panel fallback image path
        self._detail_panel._fallback = os.path.join(root, "eXo", "util", "exodos.png")

        # Swap library in list panel
        self._list_panel.set_library(lib)

        # Update status
        total = len(lib.games)
        installed = len(lib.filter_installed())
        cfg = get_project(project_id)
        name = cfg.display_name if cfg else project_id
        self._set_status(f"{name}: {total:,} games  ·  {installed:,} installed")

    # ── menu build ────────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        mb = self.menuBar()

        file_menu = mb.addMenu("File")
        act_settings = QAction("Settings…", self)
        act_settings.setShortcut(QKeySequence.StandardKey.Preferences)
        act_settings.triggered.connect(self._open_settings)
        file_menu.addAction(act_settings)
        file_menu.addSeparator()
        act_quit = QAction("Quit", self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(QApplication.quit)
        file_menu.addAction(act_quit)

        view_menu = mb.addMenu("View")

        act_refresh = QAction("Refresh library", self)
        act_refresh.setShortcut(QKeySequence("F5"))
        act_refresh.triggered.connect(self._refresh_library)
        view_menu.addAction(act_refresh)

        view_menu.addSeparator()

        theme_menu = view_menu.addMenu("Theme")
        self._theme_actions: dict[str, QAction] = {}
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        for tname in themes.THEME_NAMES:
            act = QAction(tname, self, checkable=True)
            act.setChecked(tname == themes.current_name())
            act.triggered.connect(lambda checked, n=tname: self._switch_theme(n))
            theme_group.addAction(act)
            theme_menu.addAction(act)
            self._theme_actions[tname] = act

        view_menu.addSeparator()

        act_reset_splitter = QAction("Reset split to 50/50", self)
        act_reset_splitter.triggered.connect(self._reset_splitter)
        view_menu.addAction(act_reset_splitter)

        act_reset_win = QAction("Reset window size and position", self)
        act_reset_win.triggered.connect(self._reset_window)
        view_menu.addAction(act_reset_win)

        help_menu = mb.addMenu("Help")
        act_about = QAction("About", self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

    def _build_status_bar(self) -> None:
        self._status = self.statusBar()
        self._status_label = QLabel("Ready")
        self._status.addWidget(self._status_label)

    # ── slots ─────────────────────────────────────────────────────────────────

    @pyqtSlot(object)
    def _on_game_selected(self, game: Game) -> None:
        self._detail_panel.show_game(game)
        self._set_status(f"{game.title}  [{game.emulator_display}]")

    @pyqtSlot(object)
    def _on_play_requested(self, game: Game) -> None:
        if self._launcher:
            self._set_status(f"Launching {game.title}…")
            self._launcher.launch(game)

    @pyqtSlot(object)
    def _on_install_requested(self, game: Game) -> None:
        if self._launcher:
            if not getattr(game, "zip_present", True):
                # Lite mode — ZIP must be acquired first
                self._set_status(f"Fetching {game.title}…")
                self._launcher.fetch(game)
            else:
                self._set_status(f"Installing {game.title}…")
                self._launcher.install(game)

    @pyqtSlot()
    def _on_cancel_requested(self) -> None:
        if self._launcher:
            self._launcher.cancel_fetch()
            self._set_status("Cancelling download…")

    @pyqtSlot(str)
    def _on_launch_started(self, game_id: str) -> None:
        lib = self._library
        game = lib.get_by_id(game_id) if lib else None
        if game:
            self._set_status(f"Running: {game.title}")

    @pyqtSlot(str, int)
    def _on_launch_finished(self, game_id: str, rc: int) -> None:
        lib = self._library
        game = lib.get_by_id(game_id) if lib else None
        name = game.title if game else game_id
        self._set_status(f"{name} exited (code {rc})")

    @pyqtSlot(str, str)
    def _on_launch_error(self, game_id: str, msg: str) -> None:
        lib = self._library
        game = lib.get_by_id(game_id) if lib else None
        name = game.title if game else game_id
        QMessageBox.warning(self, "Launch error", f"Could not launch {name}:\n{msg}")
        self._set_status(f"Launch failed: {name}")

    @pyqtSlot(str, int, int)
    def _on_install_progress(self, game_id: str, current: int, total: int) -> None:
        self._detail_panel.set_installing(current, total)

    @pyqtSlot(str, bool, str)
    def _on_install_finished(self, game_id: str, success: bool, msg: str) -> None:
        lib = self._library
        game = lib.get_by_id(game_id) if lib else None
        if game:
            game.installed = success
        self._detail_panel.set_install_done(success, msg)
        if hasattr(self, "_list_panel"):
            self._list_panel.refresh()
        if success:
            name = game.title if game else game_id
            self._set_status(f"Installed: {name}")
        else:
            QMessageBox.warning(self, "Install error", msg)

    @pyqtSlot(str, str)
    def _on_fetch_phase(self, game_id: str, phase: str) -> None:
        self._detail_panel.set_fetch_phase(phase)
        self._set_status(phase)

    @pyqtSlot(str, int, int)
    def _on_fetch_progress(self, game_id: str, current: int, total: int) -> None:
        self._detail_panel.set_installing(current, total)

    @pyqtSlot(str, bool, str)
    def _on_fetch_finished(self, game_id: str, success: bool, msg: str) -> None:
        lib = self._library
        game = lib.get_by_id(game_id) if lib else None
        if game and success:
            game.installed = True
            game.zip_present = True
        self._detail_panel.set_install_done(success, msg)
        if hasattr(self, "_list_panel"):
            self._list_panel.refresh()
        if success:
            name = game.title if game else game_id
            self._set_status(f"Installed: {name}")
        else:
            QMessageBox.warning(self, "Download error", msg)

    @pyqtSlot(str)
    def _on_fetch_cancelled(self, game_id: str) -> None:
        lib = self._library
        game = lib.get_by_id(game_id) if lib else None
        name = game.title if game else game_id
        self._detail_panel.set_fetch_cancelled()
        self._set_status(f"Download cancelled: {name}")

    # ── menu actions ──────────────────────────────────────────────────────────

    def _switch_theme(self, name: str) -> None:
        themes.set_theme(name, QApplication.instance())
        self._settings.setValue("theme", name)
        for tname, act in self._theme_actions.items():
            act.setChecked(tname == name)
        if hasattr(self, "_list_panel"):
            self._list_panel.apply_theme()
        if hasattr(self, "_detail_panel"):
            self._detail_panel.rebuild_ui()

    def _reset_splitter(self) -> None:
        if hasattr(self, "_splitter"):
            w = self._splitter.width() or WINDOW_W
            self._splitter.setSizes([w // 2, w // 2])
            self._settings.remove("window/splitter")

    def _reset_window(self) -> None:
        self._settings.remove("window/geometry")
        self._settings.remove("window/splitter")
        self.resize(WINDOW_W, WINDOW_H)
        screen = QApplication.primaryScreen()
        if screen:
            sg = screen.availableGeometry()
            self.move(sg.center() - self.rect().center())
        if hasattr(self, "_splitter"):
            w = self._splitter.width() or WINDOW_W
            self._splitter.setSizes([w // 2, w // 2])

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._on_settings_changed()

    def _on_settings_changed(self) -> None:
        """Reload projects list and refresh the active project."""
        self._libraries.clear()
        self._launchers.clear()

        raw = self._settings.value("projects", "[]")
        try:
            self._projects = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self._projects = []

        if not any(p["id"] == self._active_id for p in self._projects):
            self._active_id = self._projects[0]["id"] if self._projects else ""

        if self._projects:
            self._loading = LoadingWidget(self)
            self.setCentralWidget(self._loading)
            QTimer.singleShot(100, self._load_active_project)
        else:
            self._show_no_project_ui()

    def _refresh_library(self) -> None:
        if not self._active_id:
            return
        self._set_status("Refreshing…")
        lib = self._make_library(self._active_id)
        if lib:
            lib.load()
            self._libraries[self._active_id] = lib
            if hasattr(self, "_list_panel"):
                self._list_panel.set_library(lib)
            self._set_status(f"Refreshed: {len(lib.games):,} games")

    def _show_about(self) -> None:
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> v{APP_VERSION}<br><br>"
            "A Python/PyQt6 GUI launcher for eXo DOS/Windows collections.<br>"
            "Supports eXoDOS and eXoWin3x independently — no merging required.<br>"
            "Runs on macOS and Linux with dosbox-staging, dosbox-x, dosbox-ece, "
            "and ScummVM.<br><br>"
            "Based on the eXoDOS and eXoWin3x projects by The eXo Team."
        )

    # ── window lifecycle ──────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:  # noqa: N802
        self._settings.setValue("window/geometry", self.saveGeometry())
        if hasattr(self, "_splitter"):
            self._settings.setValue("window/splitter", self._splitter.saveState())
        super().closeEvent(event)

    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)
