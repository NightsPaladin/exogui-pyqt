"""
main_window.py — Main application window (multi-project aware).
"""

from __future__ import annotations

import json
import os
import shutil
import sys

from PyQt6.QtCore import Qt, QThread, QTimer, QSettings, QByteArray, pyqtSlot, pyqtSignal
from PyQt6.QtGui import QKeySequence, QAction, QActionGroup
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QSplitter, QDialog, QDialogButtonBox, QFormLayout, QComboBox,
    QLineEdit, QLabel, QPushButton, QApplication, QMessageBox,
    QProgressBar, QFileDialog, QScrollArea,
    QGroupBox, QTabBar, QFrame, QCheckBox,
)

from core.game_library import GameLibrary, Game
from core.launcher import Launcher
from core.image_cache import ImageCache
from core.project import ProjectConfig, ALL_PROJECTS, get_project
from core.favorites import FavoritesStore
import core.native_launch as _native_launch
from gui.media_pack_dialog import MediaPackImportDialog
from gui.game_list import GameListPanel
from gui.game_detail import GameDetailPanel
from gui.launch_variant_dialog import LaunchVariantDialog
from gui import themes
from gui.pin_dialog import (
    has_pin, set_pin, clear_pin,
    is_session_unlocked, unlock_session,
    is_first_launch, mark_first_launch_done,
    PinEntryDialog, PinSetupDialog,
)


APP_NAME     = "eXoGUI"
APP_VERSION  = "0.3.0"
WINDOW_W     = 1280
WINDOW_H     = 800

# Directory that contains the exogui-pyqt/ package (i.e. the drive root layout).
# All project/ZIP-source paths are stored relative to this so the app is
# portable when the entire drive is remounted at a different path.
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HINT_TEXT_STYLE = "color: gray; font-size: 11px;"
_STATUS_DOT_STYLES = {
    "missing": "color: gray;",
    "ok": "color: green;",
    "error": "color: red;",
}


def _read_bool_setting(settings: QSettings, key: str, default: bool = False) -> bool:
    """Read a QSettings boolean while tolerating older string-backed values."""
    value = settings.value(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default if value is None else bool(value)


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
                 zip_source_path: str = "", torrent_path: str = "",
                 show_mature: bool = False,
                 settings: QSettings | None = None,
                 parent=None):
        super().__init__(parent)
        self.project_id = project_id
        self._settings = settings

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(4)

        def _sub_label(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setFixedWidth(96)
            lbl.setStyleSheet(_HINT_TEXT_STYLE)
            return lbl

        def _spacer() -> QWidget:
            sp = QWidget()
            sp.setFixedWidth(28)
            return sp

        # ── Row 1: project name + root path ──────────────────────────────────
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

        # ── Row 2: optional game source path ─────────────────────────────────
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        self._zip_source_edit = QLineEdit(zip_source_path)
        self._zip_source_edit.setPlaceholderText(
            "Optional: collection root or game ZIP folder (local drive or NAS)…"
        )
        self._zip_source_edit.setToolTip(
            "Lite mode only. Point this to an eXo collection root directory\n"
            "(e.g. /Volumes/BigDrive/eXoDOS) or directly to a folder of game ZIPs.\n"
            "When a collection root is given, both game ZIPs and GameData extras\n"
            "are sourced from it. The GUI copies from here before trying the torrent."
        )

        browse_zip_btn = QPushButton("Browse…")
        browse_zip_btn.setFixedWidth(80)
        browse_zip_btn.clicked.connect(self._browse_zip_source)

        row2.addWidget(_sub_label("Game Source"))
        row2.addWidget(self._zip_source_edit, 1)
        row2.addWidget(browse_zip_btn)
        row2.addWidget(_spacer())

        # ── Row 3: optional torrent file path ────────────────────────────────
        row3 = QHBoxLayout()
        row3.setSpacing(8)

        self._torrent_edit = QLineEdit(torrent_path)
        self._torrent_edit.setPlaceholderText(
            "Optional: path to .torrent file for Lite download…"
        )
        self._torrent_edit.setToolTip(
            "Point to the project's .torrent file (e.g. eXoDOS.torrent).\n"
            "Used by the Lite download to fetch only the metadata subset\n"
            "(images, XML database, launch scripts) without the game ZIPs."
        )

        browse_torrent_btn = QPushButton("Browse…")
        browse_torrent_btn.setFixedWidth(80)
        browse_torrent_btn.clicked.connect(self._browse_torrent)

        row3.addWidget(_sub_label("Torrent"))
        row3.addWidget(self._torrent_edit, 1)
        row3.addWidget(browse_torrent_btn)
        row3.addWidget(_spacer())

        # ── Row 4: mature-content override (intentionally low-profile) ─────────
        row4 = QHBoxLayout()
        row4.setSpacing(8)
        self._mature_cb = QCheckBox("Include adult/mature titles")
        self._mature_cb.setChecked(show_mature)
        self._mature_cb.setStyleSheet(_HINT_TEXT_STYLE)
        self._mature_cb.setToolTip(
            "When unchecked (default), only family-friendly titles are shown\n"
            "if this collection provides a family-friendly game list.\n"
            "Check to show all titles including adult/mature content."
        )
        row4.addWidget(_sub_label(""))          # indent to align with other rows
        row4.addWidget(self._mature_cb)
        row4.addStretch()

        self._mature_cb.clicked.connect(self._on_mature_clicked)

        layout.addLayout(row1)
        layout.addLayout(row2)
        layout.addLayout(row3)
        layout.addLayout(row4)

    def _on_mature_clicked(self, checked: bool) -> None:
        if not checked:
            return  # disabling is always allowed
        if is_session_unlocked():
            return  # already verified PIN this session
        if not self._settings or not has_pin(self._settings):
            return  # no PIN set — no restriction
        # Revert the visual tick until PIN is verified
        self._mature_cb.blockSignals(True)
        self._mature_cb.setChecked(False)
        self._mature_cb.blockSignals(False)
        dlg = PinEntryDialog(self._settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            unlock_session()
            self._mature_cb.blockSignals(True)
            self._mature_cb.setChecked(True)
            self._mature_cb.blockSignals(False)

    def _browse_root(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select project root", self._root_edit.text())
        if d:
            self._root_edit.setText(d)

    def _browse_zip_source(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Select game source folder", self._zip_source_edit.text()
        )
        if d:
            self._zip_source_edit.setText(d)

    def _browse_torrent(self) -> None:
        current = self._torrent_edit.text().strip()
        start = os.path.dirname(current) if current else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Select torrent file", start, "Torrent files (*.torrent)"
        )
        if path:
            self._torrent_edit.setText(path)

    @property
    def root(self) -> str:
        return self._root_edit.text().strip()

    @property
    def show_mature(self) -> bool:
        return self._mature_cb.isChecked()

    @property
    def zip_source_path(self) -> str:
        return self._zip_source_edit.text().strip()

    @property
    def torrent_path(self) -> str:
        return self._torrent_edit.text().strip()

    @property
    def remove_button(self) -> QPushButton:
        return self._remove_btn


# ── emulator helpers ─────────────────────────────────────────────────────────

def _brew_path() -> str:
    """Return PATH with Homebrew prefixes prepended (macOS only)."""
    parts = list(os.environ.get("PATH", "").split(os.pathsep))
    if sys.platform == "darwin":
        for prefix in ("/opt/homebrew", "/usr/local"):
            for sub in ("bin", "sbin"):
                p = f"{prefix}/{sub}"
                if os.path.isdir(p) and p not in parts:
                    parts.insert(0, p)
    return os.pathsep.join(parts)


def _resolve_app_bundle(path: str) -> str:
    """Resolve a macOS .app bundle to its inner executable.

    Reads Contents/Info.plist → CFBundleExecutable; falls back to the first
    executable found in Contents/MacOS/.  Non-.app paths pass through unchanged,
    so bare commands (e.g. 'dosbox-staging') are returned as-is.
    """
    if not path or not path.endswith(".app") or not os.path.isdir(path):
        return path
    try:
        import plistlib
        plist = os.path.join(path, "Contents", "Info.plist")
        if os.path.isfile(plist):
            with open(plist, "rb") as f:
                info = plistlib.load(f)
            exe_name = info.get("CFBundleExecutable", "")
            if exe_name:
                exe_path = os.path.join(path, "Contents", "MacOS", exe_name)
                if os.path.isfile(exe_path):
                    return exe_path
    except Exception:
        pass
    # Fallback: first executable found in Contents/MacOS/
    macos_dir = os.path.join(path, "Contents", "MacOS")
    if os.path.isdir(macos_dir):
        candidates = sorted(
            p for p in (os.path.join(macos_dir, n) for n in os.listdir(macos_dir))
            if os.path.isfile(p) and os.access(p, os.X_OK)
        )
        if candidates:
            return candidates[0]
    return path


def _load_emulators_from_settings(settings: QSettings) -> dict[str, str]:
    """Return {family_name: resolved_command} from settings (macOS only).

    .app bundle paths are resolved to their inner executable so the launcher
    receives a plain executable path.  Bare commands pass through unchanged.
    On Linux dosbox_linux.txt already contains full commands, so no overrides
    are needed and an empty dict is returned.
    """
    if sys.platform.startswith("linux"):
        return {}
    raw = settings.value("emulators", None)
    if raw is None:
        # Migrate from legacy individual keys if present
        old = {
            "dosbox-staging": settings.value("dosbox_staging", ""),
            "dosbox-x":       settings.value("dosbox_x", ""),
            "dosbox-ece":     settings.value("dosbox_ece", ""),
            "scummvm":        settings.value("scummvm", ""),
        }
        entries = [{"name": k, "command": v} for k, v in old.items() if v]
        if not entries:
            return {}
    else:
        try:
            entries = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            entries = []
    return {
        e["name"]: _resolve_app_bundle(e.get("command", ""))
        for e in entries if e.get("name")
    }


# ── per-emulator config row (structured, fixed name) ─────────────────────────

class _EmulatorConfigRow(QWidget):
    """
    One row in the Settings > Emulators section.

    Displays a fixed emulator name and key, an editable command/path field,
    a Browse button (for .app bundles), and a live status dot.
    """

    def __init__(self, spec, command: str = "", parent=None):
        super().__init__(parent)
        self._spec = spec

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)
        layout.setSpacing(8)

        # Fixed name block
        name_w = QWidget()
        name_w.setFixedWidth(148)
        name_layout = QVBoxLayout(name_w)
        name_layout.setContentsMargins(0, 0, 0, 0)
        name_layout.setSpacing(0)
        disp = QLabel(f"<b>{spec.display_name}</b>")
        key = QLabel(f"<span style='color:gray;font-size:10px;'>{spec.name}</span>")
        name_layout.addWidget(disp)
        name_layout.addWidget(key)

        # Command / path field
        hint = (spec.check_commands[0] if spec.check_commands else spec.name)
        self._cmd = QLineEdit(command)
        self._cmd.setPlaceholderText(f".app bundle or bare command  (e.g. {hint})")
        self._cmd.setToolTip(
            "Path to a .app bundle (e.g. /Applications/DOSBox-Staging.app)\n"
            "or a bare command name if the emulator is on your PATH\n"
            "(e.g. dosbox-staging installed via Homebrew)."
        )
        self._cmd.textChanged.connect(self._refresh_status)

        # Browse button — opens dir picker so .app bundles can be selected
        browse = QPushButton("Browse…")
        browse.setFixedWidth(80)
        browse.clicked.connect(self._browse)

        # Status dot
        self._dot = QLabel("●")
        self._dot.setFixedWidth(20)
        self._dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._refresh_status()

        layout.addWidget(name_w)
        layout.addWidget(self._cmd, 1)
        layout.addWidget(browse)
        layout.addWidget(self._dot)

    def _refresh_status(self) -> None:
        cmd = self._cmd.text().strip()
        if not cmd:
            self._dot.setStyleSheet(_STATUS_DOT_STYLES["missing"])
            self._dot.setToolTip("Not configured")
            return
        if os.path.exists(cmd):
            self._dot.setStyleSheet(_STATUS_DOT_STYLES["ok"])
            self._dot.setToolTip("Found")
            return

        which_result = shutil.which(cmd, path=_brew_path())
        if which_result:
            self._dot.setStyleSheet(_STATUS_DOT_STYLES["ok"])
            self._dot.setToolTip(f"Found on PATH: {which_result}")
            return

        self._dot.setStyleSheet(_STATUS_DOT_STYLES["error"])
        self._dot.setToolTip("Not found — check the path or install the emulator")

    def _browse(self) -> None:
        current = self._cmd.text().strip()
        start = (
            os.path.dirname(current) if current and os.path.exists(current)
            else "/Applications" if sys.platform == "darwin"
            else os.path.expanduser("~")
        )
        path = QFileDialog.getExistingDirectory(
            self, f"Select {self._spec.display_name}",
            start,
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontUseNativeDialog,
        )
        if path:
            self._cmd.setText(path)

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def command(self) -> str:
        return self._cmd.text().strip()


# ── add-project dialog ────────────────────────────────────────────────────────

class _AddProjectDialog(QDialog):
    """Pick a known eXo project, choose where to install it, and optionally
    configure the torrent and ZIP-source paths up front."""

    def __init__(self, existing_ids: list[str], default_parent: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add eXo Project")
        self.setMinimumWidth(540)

        self._available = [p for p in ALL_PROJECTS if p.id not in existing_ids]

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ── Main form ─────────────────────────────────────────────────────────
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setSpacing(8)

        self._combo = QComboBox()
        for p in self._available:
            self._combo.addItem(p.display_name, p.id)
        form.addRow("Project:", self._combo)

        parent_w = QWidget()
        parent_row = QHBoxLayout(parent_w)
        parent_row.setContentsMargins(0, 0, 0, 0)
        parent_row.setSpacing(6)
        self._parent_edit = QLineEdit(default_parent)
        self._parent_edit.setPlaceholderText("Directory to install into…")
        browse_parent = QPushButton("Browse…")
        browse_parent.setFixedWidth(80)
        browse_parent.clicked.connect(self._browse_parent)
        parent_row.addWidget(self._parent_edit, 1)
        parent_row.addWidget(browse_parent)
        form.addRow("Install in:", parent_w)

        self._folder_edit = QLineEdit()
        self._folder_edit.setToolTip(
            "Folder name that will be created inside the install location.\n"
            "Pre-filled from the project's torrent name — edit if you need."
        )
        form.addRow("Folder name:", self._folder_edit)

        self._path_label = QLabel()
        self._path_label.setStyleSheet(_HINT_TEXT_STYLE)
        self._path_label.setWordWrap(True)
        form.addRow("Full path:", self._path_label)

        layout.addLayout(form)

        # ── Optional section ──────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        opt_label = QLabel("Optional — can be configured later in Settings:")
        opt_label.setStyleSheet(_HINT_TEXT_STYLE)
        layout.addWidget(opt_label)

        opt_form = QFormLayout()
        opt_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        opt_form.setSpacing(8)

        torrent_w = QWidget()
        torrent_row = QHBoxLayout(torrent_w)
        torrent_row.setContentsMargins(0, 0, 0, 0)
        torrent_row.setSpacing(6)
        self._torrent_edit = QLineEdit()
        self._torrent_edit.setPlaceholderText("Path to .torrent file…")
        browse_torrent = QPushButton("Browse…")
        browse_torrent.setFixedWidth(80)
        browse_torrent.clicked.connect(self._browse_torrent)
        torrent_row.addWidget(self._torrent_edit, 1)
        torrent_row.addWidget(browse_torrent)
        opt_form.addRow("Torrent:", torrent_w)

        zip_w = QWidget()
        zip_row = QHBoxLayout(zip_w)
        zip_row.setContentsMargins(0, 0, 0, 0)
        zip_row.setSpacing(6)
        self._zip_edit = QLineEdit()
        self._zip_edit.setPlaceholderText("Collection root or game ZIP folder (Lite mode)…")
        self._zip_edit.setToolTip(
            "Point to an eXo collection root (e.g. /Volumes/BigDrive/eXoDOS)\n"
            "or directly to a folder of game ZIPs.\n"
            "A collection root also sources GameData extras automatically."
        )
        browse_zip = QPushButton("Browse…")
        browse_zip.setFixedWidth(80)
        browse_zip.clicked.connect(self._browse_zip)
        zip_row.addWidget(self._zip_edit, 1)
        zip_row.addWidget(browse_zip)
        opt_form.addRow("Game source:", zip_w)

        layout.addLayout(opt_form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Wire live updates
        self._combo.currentIndexChanged.connect(self._on_project_changed)
        self._parent_edit.textChanged.connect(self._update_path)
        self._folder_edit.textChanged.connect(self._update_path)

        self._on_project_changed(0)

    # ── internal ──────────────────────────────────────────────────────────────

    def _on_project_changed(self, idx: int) -> None:
        if 0 <= idx < len(self._available):
            cfg = self._available[idx]
            folder = cfg.torrent_name.removesuffix(".torrent") or cfg.display_name
            self._folder_edit.setText(folder)
        self._update_path()

    def _update_path(self) -> None:
        parent = self._parent_edit.text().strip()
        folder = self._folder_edit.text().strip()
        self._path_label.setText(os.path.join(parent, folder) if parent and folder
                                 else parent or folder)

    def _browse_parent(self) -> None:
        current = self._parent_edit.text().strip()
        start = current if current and os.path.isdir(current) else os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "Select install location", start)
        if d:
            self._parent_edit.setText(d)

    def _browse_torrent(self) -> None:
        current = self._torrent_edit.text().strip()
        start = os.path.dirname(current) if current else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Select torrent file", start, "Torrent files (*.torrent)"
        )
        if path:
            self._torrent_edit.setText(path)

    def _browse_zip(self) -> None:
        current = self._zip_edit.text().strip()
        start = current if current and os.path.isdir(current) else os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "Select game source folder", start)
        if d:
            self._zip_edit.setText(d)

    def _on_accept(self) -> None:
        parent = self._parent_edit.text().strip()
        folder = self._folder_edit.text().strip()
        if not parent or not folder:
            QMessageBox.warning(
                self, "Missing path",
                "Please specify both an install location and a folder name."
            )
            return
        full = os.path.join(parent, folder)
        try:
            os.makedirs(full, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(
                self, "Cannot create directory",
                f"Could not create:\n{full}\n\n{exc}"
            )
            return
        self.accept()

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def selected_project_id(self) -> str:
        idx = self._combo.currentIndex()
        return self._available[idx].id if 0 <= idx < len(self._available) else ""

    @property
    def root_path(self) -> str:
        return os.path.join(self._parent_edit.text().strip(), self._folder_edit.text().strip())

    @property
    def torrent_path(self) -> str:
        return self._torrent_edit.text().strip()

    @property
    def zip_source_path(self) -> str:
        return self._zip_edit.text().strip()


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
            tp  = settings.value(f"project_{p['id']}/torrent_path", "")
            mature = _read_bool_setting(settings, f"project_{p['id']}/show_mature")
            self._add_row(
                p["id"],
                _from_stored_path(p.get("root", "")),
                _from_stored_path(zsp),
                _from_stored_path(tp),
                show_mature=mature,
            )

        add_btn = QPushButton("＋  Add Project…")
        add_btn.setFixedWidth(160)
        add_btn.clicked.connect(self._add_project)
        projects_layout.addWidget(add_btn, 0, Qt.AlignmentFlag.AlignLeft)

        outer.addWidget(projects_box)

        # ── Emulators (macOS only) ────────────────────────────────────────────
        # On Linux, dosbox_linux.txt already contains full flatpak commands so
        # no overrides are needed and this section is omitted.
        self._emu_config_rows: list[_EmulatorConfigRow] = []
        if sys.platform == "darwin":
            from core.emulator_deps import EMULATOR_GROUPS

            emu_box = QGroupBox("Emulators")
            emu_layout = QVBoxLayout(emu_box)

            # Top bar: note + "Get Emulators" link
            top_bar = QHBoxLayout()
            top_note = QLabel(
                "<span style='color:gray;font-size:11px;'>"
                "Point each emulator to its .app bundle or bare command name. "
                "● = found  ● = not found</span>"
            )
            top_note.setWordWrap(True)
            get_btn = QPushButton("Get Emulators…")
            get_btn.setToolTip("Show download links for all emulator backends")
            get_btn.setFixedWidth(130)
            get_btn.clicked.connect(self._open_get_emulators)
            top_bar.addWidget(top_note, 1)
            top_bar.addWidget(get_btn)
            emu_layout.addLayout(top_bar)

            emu_sep = QFrame()
            emu_sep.setFrameShape(QFrame.Shape.HLine)
            emu_layout.addWidget(emu_sep)

            emu_scroll = QScrollArea()
            emu_scroll.setWidgetResizable(True)
            emu_scroll.setFrameShape(QFrame.Shape.NoFrame)
            emu_scroll.setMinimumHeight(200)

            emu_content = QWidget()
            emu_content_layout = QVBoxLayout(emu_content)
            emu_content_layout.setContentsMargins(4, 4, 4, 4)
            emu_content_layout.setSpacing(6)

            # Build saved-command lookup from settings
            saved: dict[str, str] = {}
            raw_emu = settings.value("emulators", None)
            if raw_emu is None:
                # Migrate from legacy individual keys
                for old_name, old_key in [
                    ("dosbox-staging", "dosbox_staging"),
                    ("dosbox-x",       "dosbox_x"),
                    ("dosbox-ece",     "dosbox_ece"),
                    ("scummvm",        "scummvm"),
                ]:
                    v = settings.value(old_key, "")
                    if v:
                        saved[old_name] = v
            else:
                try:
                    for e in json.loads(raw_emu):
                        if e.get("name"):
                            saved[e["name"]] = e.get("command", "")
                except (json.JSONDecodeError, TypeError):
                    pass

            # Render grouped rows
            for group in EMULATOR_GROUPS:
                specs = group.specs()
                if not specs:
                    continue
                grp_lbl = QLabel(
                    f"<span style='color:gray;font-size:11px;'><b>{group.label}</b></span>"
                )
                emu_content_layout.addWidget(grp_lbl)
                for spec in specs:
                    row = _EmulatorConfigRow(spec, saved.get(spec.name, ""))
                    emu_content_layout.addWidget(row)
                    self._emu_config_rows.append(row)
                grp_div = QFrame()
                grp_div.setFrameShape(QFrame.Shape.HLine)
                emu_content_layout.addWidget(grp_div)

            emu_content_layout.addStretch()
            emu_scroll.setWidget(emu_content)
            emu_layout.addWidget(emu_scroll)
            outer.addWidget(emu_box)

        # ── Parental Controls ─────────────────────────────────────────────────
        pc_box = QGroupBox("Parental Controls")
        pc_layout = QHBoxLayout(pc_box)
        self._pin_status_lbl = QLabel()
        pc_layout.addWidget(self._pin_status_lbl)
        pc_layout.addStretch()
        self._set_pin_btn    = QPushButton("Set PIN…")
        self._change_pin_btn = QPushButton("Change PIN…")
        self._remove_pin_btn = QPushButton("Remove PIN…")
        self._set_pin_btn.clicked.connect(self._do_set_pin)
        self._change_pin_btn.clicked.connect(self._do_change_pin)
        self._remove_pin_btn.clicked.connect(self._do_remove_pin)
        pc_layout.addWidget(self._set_pin_btn)
        pc_layout.addWidget(self._change_pin_btn)
        pc_layout.addWidget(self._remove_pin_btn)
        outer.addWidget(pc_box)
        self._refresh_pin_ui()

        # ── Playback Options ──────────────────────────────────────────────────
        playback_box = QGroupBox("Playback")
        playback_layout = QVBoxLayout(playback_box)
        self._autoplay_check = QCheckBox("Auto-play music when a game is selected")
        self._autoplay_check.setChecked(_read_bool_setting(settings, "music_autoplay"))
        playback_layout.addWidget(self._autoplay_check)
        outer.addWidget(playback_box)

        # ── Buttons ───────────────────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ── internal helpers ──────────────────────────────────────────────────────

    def _open_get_emulators(self) -> None:
        from gui.emulator_check_dialog import EmulatorCheckDialog
        dlg = EmulatorCheckDialog(self)
        dlg.exec()

    def _add_row(self, project_id: str, root: str,
                 zip_source_path: str = "", torrent_path: str = "",
                 show_mature: bool = False) -> None:
        row = _ProjectRow(project_id, root, zip_source_path, torrent_path,
                          show_mature, self._settings, self)
        row.remove_button.clicked.connect(lambda: self._remove_row(row))
        idx = self._rows_layout.count() - 1   # insert before stretch
        self._rows_layout.insertWidget(idx, row)
        self._project_rows.append(row)

    def _remove_row(self, row: _ProjectRow) -> None:
        self._rows_layout.removeWidget(row)
        row.deleteLater()
        self._project_rows.remove(row)

    def _add_project(self) -> None:
        existing_ids = [row.project_id for row in self._project_rows]
        available = [p for p in ALL_PROJECTS if p.id not in existing_ids]
        if not available:
            QMessageBox.information(
                self, "All projects added",
                "All known eXo projects are already in the list."
            )
            return

        # Default parent: parent dir of the first configured project row
        default_parent = ""
        for row in self._project_rows:
            if row.root:
                candidate = os.path.dirname(row.root)
                if os.path.isdir(candidate):
                    default_parent = candidate
                    break
        if not default_parent:
            default_parent = os.path.expanduser("~")

        dlg = _AddProjectDialog(existing_ids, default_parent, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._add_row(dlg.selected_project_id, dlg.root_path, dlg.zip_source_path, dlg.torrent_path)

    def _refresh_pin_ui(self) -> None:
        if has_pin(self._settings):
            self._pin_status_lbl.setText("PIN protection: <b>Enabled</b>")
            self._set_pin_btn.setVisible(False)
            self._change_pin_btn.setVisible(True)
            self._remove_pin_btn.setVisible(True)
        else:
            self._pin_status_lbl.setText("PIN protection: <b>Not set</b>  — adult-content setting is unprotected")
            self._set_pin_btn.setVisible(True)
            self._change_pin_btn.setVisible(False)
            self._remove_pin_btn.setVisible(False)

    def _do_set_pin(self) -> None:
        dlg = PinSetupDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            set_pin(self._settings, dlg.pin)
            unlock_session()
            self._refresh_pin_ui()

    def _do_change_pin(self) -> None:
        verify = PinEntryDialog(self._settings, self)
        if verify.exec() != QDialog.DialogCode.Accepted:
            return
        unlock_session()
        dlg = PinSetupDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            set_pin(self._settings, dlg.pin)
            self._refresh_pin_ui()

    def _do_remove_pin(self) -> None:
        verify = PinEntryDialog(self._settings, self)
        if verify.exec() != QDialog.DialogCode.Accepted:
            return
        ans = QMessageBox.question(
            self, "Remove PIN",
            "Remove the parental control PIN?\n"
            "The adult-content setting will be unprotected.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            clear_pin(self._settings)
            self._refresh_pin_ui()

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
            self._settings.setValue(
                f"project_{row.project_id}/torrent_path",
                _to_stored_path(row.torrent_path),
            )
            self._settings.setValue(
                f"project_{row.project_id}/show_mature",
                row.show_mature,
            )
        emulators = [
            {"name": r.name, "command": r.command}
            for r in self._emu_config_rows
        ]
        self._settings.setValue("emulators", json.dumps(emulators))
        # Remove legacy individual keys (migration clean-up)
        for _old_key in ("dosbox_staging", "dosbox_x", "dosbox_ece", "scummvm"):
            self._settings.remove(_old_key)
        self._settings.setValue("music_autoplay", self._autoplay_check.isChecked())
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
            f" border-radius:4px; color:{t.text_on_accent}; }}"
            f"QProgressBar::chunk {{ background:{t.accent}; border-radius:4px; }}"
        )

    def set_label(self, text: str) -> None:
        self._label.setText(text)

    def set_status(self, text: str) -> None:
        self._sub.setText(text)


# ── background library loader ─────────────────────────────────────────────────

class _LibraryLoaderThread(QThread):
    """
    Runs GameLibrary.load() on a worker thread so the UI stays responsive.

    Emits finished(library, project_id) on success or error(message, project_id)
    on failure.  parent should be the MainWindow so Qt owns the lifetime.
    """

    loaded:     pyqtSignal = pyqtSignal(object, str)   # (GameLibrary, project_id)
    load_error: pyqtSignal = pyqtSignal(str,    str)    # (message,     project_id)

    def __init__(self, library, project_id: str, force_reload: bool = False, parent=None):
        super().__init__(parent)
        self._library      = library
        self._project_id   = project_id
        self._force_reload = force_reload

    def run(self) -> None:
        try:
            self._library.load(force_reload=self._force_reload)
            self.loaded.emit(self._library, self._project_id)
        except Exception as exc:
            self.load_error.emit(str(exc), self._project_id)


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

        try:
            _fav_raw = self._settings.value("favorites", "{}")
            _fav_data = json.loads(_fav_raw) if _fav_raw else {}
        except (json.JSONDecodeError, TypeError):
            _fav_data = {}
        self._favorites_store = FavoritesStore(_fav_data)
        self._libraries: dict[str, GameLibrary] = {}
        self._launchers: dict[str, Launcher]    = {}
        self._setup_already_offered: set[str]   = set()

        self.setWindowTitle(f"{APP_NAME} (Qt)")
        self.resize(WINDOW_W, WINDOW_H)

        saved_theme = self._settings.value("theme", "System")
        app = QApplication.instance()
        assert isinstance(app, QApplication)
        themes.set_theme(saved_theme, app)

        geom: QByteArray | None = self._settings.value("window/geometry")
        if geom:
            self.restoreGeometry(geom)

        self._cache = ImageCache(max_size=600)

        self._loading = LoadingWidget(self)
        self.setCentralWidget(self._loading)

        self._build_menu()
        self._build_status_bar()

        QTimer.singleShot(0,   self._check_first_launch)
        QTimer.singleShot(100, self._load_active_project)

    def _check_first_launch(self) -> None:
        if not is_first_launch(self._settings):
            return
        mark_first_launch_done(self._settings)
        ans = QMessageBox.question(
            self, "Set Up Parental Controls",
            "eXoGUI defaults to family-friendly titles only.\n\n"
            "Would you like to set a 4-digit PIN to protect the\n"
            "adult-content setting? Children won't be able to enable\n"
            "adult titles without this PIN.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            dlg = PinSetupDialog(self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                set_pin(self._settings, dlg.pin)
                unlock_session()

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
        """Return ``(ProjectConfig, root)`` for a project id, or ``(None, "")``."""
        for p in self._projects:
            if p["id"] == project_id:
                return get_project(project_id), _from_stored_path(p.get("root", ""))
        return None, ""

    def _require_project_entry(self, project_id: str) -> tuple[ProjectConfig, str] | None:
        """Return a fully configured project entry, or ``None`` when incomplete."""
        cfg, root = self._project_entry(project_id)
        if not cfg or not root:
            return None
        return cfg, root

    def _make_library(self, project_id: str) -> GameLibrary | None:
        entry = self._require_project_entry(project_id)
        if entry is None:
            return None
        cfg, root = entry
        show_mature = _read_bool_setting(
            self._settings, f"project_{project_id}/show_mature"
        )
        if show_mature or "family" not in cfg.xml_variants:
            xml_mode = "auto"
        elif os.path.exists(cfg.xml_path(root, "family")):
            xml_mode = "family"
        else:
            xml_mode = "auto"
        return GameLibrary(root, xml_mode=xml_mode, config=cfg)

    def _make_launcher(self, project_id: str) -> Launcher | None:
        entry = self._require_project_entry(project_id)
        if entry is None:
            return None
        cfg, root = entry
        zip_source = _from_stored_path(
            self._settings.value(f"project_{project_id}/zip_source_path", "")
        )
        torrent_path = _from_stored_path(
            self._settings.value(f"project_{project_id}/torrent_path", "")
        )
        emulators = _load_emulators_from_settings(self._settings)
        return Launcher(root, config=cfg, zip_source_path=zip_source,
                        torrent_path=torrent_path,
                        emulators=emulators, parent=self)

    def _connect_launcher(self, launcher: Launcher) -> None:
        launcher.launch_started.connect(self._on_launch_started)
        launcher.launch_finished.connect(self._on_launch_finished)
        launcher.launch_error.connect(self._on_launch_error)
        launcher.install_progress.connect(self._on_install_progress)
        launcher.install_finished.connect(self._on_install_finished)
        launcher.uninstall_finished.connect(self._on_uninstall_finished)
        launcher.fetch_phase.connect(self._on_fetch_phase)
        launcher.fetch_progress.connect(self._on_fetch_progress)
        launcher.fetch_finished.connect(self._on_fetch_finished)
        launcher.fetch_cancelled.connect(self._on_fetch_cancelled)
        launcher.lite_phase.connect(self._on_lite_phase)
        launcher.lite_progress.connect(self._on_lite_progress)
        launcher.lite_finished.connect(self._on_lite_finished)
        launcher.lite_cancelled.connect(self._on_lite_cancelled)

    def _torrent_path_for(self, project_id: str) -> str:
        """Return the configured torrent file path for a project, resolved to absolute."""
        return _from_stored_path(
            self._settings.value(f"project_{project_id}/torrent_path", "")
        )

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

        cfg, _ = self._project_entry(self._active_id)
        display = cfg.display_name if cfg else self._active_id
        self._loading.set_label(f"Loading {display}…")
        self._loading.set_status("Parsing catalogue…")

        lib = self._make_library(self._active_id)
        if lib is None:
            QMessageBox.critical(
                self, "No project configured",
                "Could not find a valid project root. Open Settings to configure."
            )
            self._show_no_project_ui()
            return

        # Offer to extract Content/*.zip if present but not yet unpacked.
        # _setup_already_offered prevents re-prompting if setup fails (e.g. all
        # ZIPs are partial downloads); once offered we skip the dialog on retry.
        entry = self._require_project_entry(self._active_id)
        if (entry is not None
                and self._active_id not in self._setup_already_offered
                and Launcher.needs_content_setup(entry[1], entry[0])):
            cfg, root = entry
            self._setup_already_offered.add(self._active_id)
            ans = QMessageBox.question(
                self,
                f"Set up {cfg.display_name}?",
                f"{cfg.display_name} has content archives that haven't been extracted yet.\n\n"
                "Extract them now? This sets up images, the game catalogue, and "
                "launch scripts (equivalent to the Windows setup script).",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans == QMessageBox.StandardButton.Yes:
                launcher = self._make_launcher(self._active_id)
                if launcher:
                    self._connect_launcher(launcher)
                    self._launchers[self._active_id] = launcher
                    launcher.setup_collection()
                    # After setup completes, _on_lite_finished will reload.
                    return

        self._loader = _LibraryLoaderThread(lib, self._active_id, parent=self)
        self._loader.loaded.connect(self._on_library_loaded)
        self._loader.load_error.connect(self._on_library_load_error)
        self._loader.loaded.connect(self._loader.deleteLater)
        self._loader.load_error.connect(self._loader.deleteLater)
        self._loader.start()

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

    # ── library load callbacks ────────────────────────────────────────────────

    @pyqtSlot(object, str)
    def _on_library_loaded(self, lib, project_id: str) -> None:
        self._libraries[project_id] = lib
        launcher = self._make_launcher(project_id)
        if launcher:
            self._connect_launcher(launcher)
            self._launchers[project_id] = launcher

        if project_id != self._active_id:
            return  # user switched away while loading; result is cached for later

        if getattr(self, "_list_panel", None) is not None:
            self._activate_project(project_id)
            return

        self._loading.set_status(f"Loaded {len(lib.games):,} games.")
        self._build_main_ui()

    @pyqtSlot(str, str)
    def _on_library_load_error(self, msg: str, project_id: str) -> None:
        if project_id != self._active_id:
            return
        self._ensure_main_skeleton()
        self._show_no_library_ui(project_id, msg)

    def _show_no_library_ui(self, project_id: str, msg: str = "") -> None:
        """Show the 'no library' state with options to fix or run a Lite download."""
        entry = self._require_project_entry(project_id)
        cfg, root = entry if entry is not None else (None, "")
        display = cfg.display_name if cfg else project_id

        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(12)

        if msg:
            err_text = (
                f"Could not load {display} catalogue:\n{msg}\n\n"
                f"Root path: {root or '(not set)'}"
            )
        else:
            err_text = f"No library found for {display}.\nRoot path: {root or '(not set)'}"
        err_lbl = QLabel(err_text)
        err_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        err_lbl.setStyleSheet("font-size:14px;")
        err_lbl.setWordWrap(True)
        layout.addWidget(err_lbl)

        settings_btn = QPushButton("Open Settings…")
        settings_btn.setFixedWidth(180)
        settings_btn.clicked.connect(self._open_settings)
        layout.addWidget(settings_btn, 0, Qt.AlignmentFlag.AlignCenter)

        torrent_path = self._torrent_path_for(project_id)
        if torrent_path and os.path.isfile(torrent_path):
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setFixedWidth(320)
            layout.addWidget(sep, 0, Qt.AlignmentFlag.AlignCenter)

            lite_lbl = QLabel(
                "Download the metadata subset (images, XML database, launch scripts)\n"
                "without the full game ZIPs using the configured torrent file:"
            )
            lite_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lite_lbl.setStyleSheet("font-size:13px; color:gray;")
            lite_lbl.setWordWrap(True)
            layout.addWidget(lite_lbl)

            lite_btn = QPushButton(f"Download Lite metadata for {display}…")
            lite_btn.setFixedWidth(320)
            lite_btn.clicked.connect(lambda: self._start_lite_download(project_id))
            layout.addWidget(lite_btn, 0, Qt.AlignmentFlag.AlignCenter)

        self._set_content_page(w)

    def _start_lite_download(self, project_id: str) -> None:
        """Begin a Lite metadata download for the given project."""
        torrent_path = self._torrent_path_for(project_id)
        if not torrent_path or not os.path.isfile(torrent_path):
            QMessageBox.warning(
                self, "No torrent configured",
                "No torrent file found for this project.\n"
                "Open Settings and set the Torrent path.",
            )
            return

        if project_id not in self._launchers:
            launcher = self._make_launcher(project_id)
            if launcher is None:
                QMessageBox.critical(
                    self, "Error",
                    "Could not create a launcher for this project.\n"
                    "Check that the project root path is configured.",
                )
                return
            self._connect_launcher(launcher)
            self._launchers[project_id] = launcher

        self._lite_project_id = project_id
        self._show_lite_progress_ui(project_id)
        if not self._launchers[project_id].download_lite(torrent_path):
            QMessageBox.warning(
                self, "Already running",
                "A Lite download is already in progress.",
            )

    def _show_lite_progress_ui(self, project_id: str) -> None:
        """Replace the central widget with the Lite download progress panel."""
        cfg = get_project(project_id)
        display = cfg.display_name if cfg else project_id

        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(10)

        title = QLabel(f"Downloading {display} Lite metadata…")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size:16px; font-weight:bold;")
        layout.addWidget(title)

        self._lite_phase_label = QLabel("Starting…")
        self._lite_phase_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lite_phase_label.setStyleSheet("font-size:13px; color:gray;")
        layout.addWidget(self._lite_phase_label)

        self._lite_bar = QProgressBar()
        self._lite_bar.setRange(0, 0)
        self._lite_bar.setFixedWidth(420)
        layout.addWidget(self._lite_bar, 0, Qt.AlignmentFlag.AlignCenter)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedWidth(120)
        cancel_btn.clicked.connect(self._cancel_lite_download)
        layout.addWidget(cancel_btn, 0, Qt.AlignmentFlag.AlignCenter)

        self._set_content_page(w)

    def _cancel_lite_download(self) -> None:
        launcher = self._launchers.get(getattr(self, "_lite_project_id", ""))
        if launcher:
            launcher.cancel_lite()
        self._set_status("Cancelling Lite download…")

    def _set_content_page(self, widget: QWidget) -> None:
        """Swap the content area (below the tab bar) without touching the tab bar.

        If _page_container exists (main UI built), replaces its contents in-place
        so the tab bar is preserved.  Before the main UI is built, falls back to
        setCentralWidget so the initial loading/error states still work.
        """
        page_container = getattr(self, "_page_container", None)
        if page_container is not None:
            layout = page_container.layout()
            assert layout is not None
            while layout.count():
                item = layout.takeAt(0)
                if item is None:
                    break
                old = item.widget()
                if old is not None:
                    old.hide()
                    old.setParent(None)   # orphan without deleting
            # Clear refs to child widgets of the outgoing page so signal
            # callbacks don't call into deleted C++ objects.
            for _attr in ("_lite_bar", "_lite_phase_label"):
                if getattr(self, _attr, None) is not None:
                    delattr(self, _attr)
            layout.addWidget(widget)
            widget.show()
        else:
            self.setCentralWidget(widget)

    def _activate_project(self, project_id: str) -> None:
        """Update the existing UI to display a newly loaded (or cached) project."""
        lib = self._libraries[project_id]
        _, root = self._project_entry(project_id)
        self._detail_panel._fallback = os.path.join(root, "eXo", "util", "exodos.png")
        self._list_panel.set_library(lib)
        self._list_panel.set_favorites(self._favorites_store.get_set(project_id))
        self._restore_project_ui_state(project_id)
        total     = len(lib.games)
        installed = len(lib.filter_installed())
        cfg  = get_project(project_id)
        name = cfg.display_name if cfg else project_id
        self._set_status(f"{name}: {total:,} games  ·  {installed:,} installed")
        self._set_content_page(self._splitter)



    def _ensure_main_skeleton(self) -> None:
        """Build tab bar + _page_container if not yet built.

        Safe to call multiple times; subsequent calls are no-ops.
        Separated from _build_main_ui so the skeleton exists even when the
        first library load fails — otherwise the tab bar never appears.
        """
        if hasattr(self, "_page_container"):
            return

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

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

        # _page_container holds the swappable content area (splitter,
        # no-library message, progress panel, etc.).  It sits below the tab
        # bar so that swapping pages never destroys the tab bar.
        self._page_container = QWidget()
        pc_layout = QVBoxLayout(self._page_container)
        pc_layout.setContentsMargins(0, 0, 0, 0)
        pc_layout.setSpacing(0)
        outer.addWidget(self._page_container)
        self.setCentralWidget(central)

    def _build_main_ui(self) -> None:
        lib = self._library
        if lib is None:
            self._show_no_project_ui()
            return
        _, root = self._project_entry(self._active_id)

        self._ensure_main_skeleton()

        # ── Splitter with list + detail ───────────────────────────────────
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setHandleWidth(2)

        self._list_panel   = GameListPanel(lib, self._cache, root)
        self._detail_panel = GameDetailPanel(self._cache, root)
        self._detail_panel.set_autoplay(
            _read_bool_setting(self._settings, "music_autoplay")
        )

        self._list_panel.game_selected.connect(self._on_game_selected)
        self._list_panel.favorite_toggled.connect(self._on_favorite_toggled)
        self._detail_panel.play_requested.connect(self._on_play_requested)
        self._detail_panel.install_requested.connect(self._on_install_requested)
        self._detail_panel.uninstall_requested.connect(self._on_uninstall_requested)
        self._detail_panel.cancel_requested.connect(self._on_cancel_requested)
        self._detail_panel.favorite_toggled.connect(self._on_favorite_toggled)

        self._list_panel.set_favorites(
            self._favorites_store.get_set(self._active_id)
        )

        self._splitter.addWidget(self._list_panel)
        self._splitter.addWidget(self._detail_panel)

        saved_split: QByteArray | None = self._settings.value("window/splitter")
        if saved_split:
            self._splitter.restoreState(saved_split)
        else:
            w = self.width() or WINDOW_W
            self._splitter.setSizes([w // 2, w // 2])

        self._set_content_page(self._splitter)

        if lib:
            total = len(lib.games)
            installed = len(lib.filter_installed())
            cfg = get_project(self._active_id)
            name = cfg.display_name if cfg else self._active_id
            self._set_status(f"{name}: {total:,} games  ·  {installed:,} installed")

        self._restore_project_ui_state(self._active_id)

    # ── project switching ─────────────────────────────────────────────────────

    def _save_project_ui_state(self, project_id: str) -> None:
        if not project_id or not hasattr(self, "_list_panel"):
            return
        state = self._list_panel.get_state()
        self._settings.setValue(f"project_{project_id}/ui_state", json.dumps(state))

    def _restore_project_ui_state(self, project_id: str) -> None:
        if not hasattr(self, "_list_panel"):
            return
        raw = self._settings.value(f"project_{project_id}/ui_state", "{}")
        try:
            state = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            state = {}
        self._list_panel.restore_state(state)

    @pyqtSlot(int)
    def _on_tab_changed(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._projects):
            return
        project_id = self._projects[idx]["id"]
        if project_id != self._active_id:
            self._save_project_ui_state(self._active_id)
            self._switch_project(project_id)

    def _switch_project(self, project_id: str) -> None:
        self._active_id = project_id
        self._settings.setValue("active_project", project_id)

        # Use cached library immediately if already loaded
        if project_id in self._libraries:
            self._activate_project(project_id)
            return

        # If a Lite download is already running for this project, restore the
        # progress UI rather than re-triggering a library load that will fail.
        launcher = self._launchers.get(project_id)
        if launcher and launcher.is_lite_downloading:
            self._lite_project_id = project_id
            self._show_lite_progress_ui(project_id)
            return

        cfg = get_project(project_id)
        display = cfg.display_name if cfg else project_id
        self._set_status(f"Loading {display}…")

        lib = self._make_library(project_id)
        if lib is None:
            QMessageBox.warning(
                self, "Project error",
                f"No root path configured for: {project_id}"
            )
            return

        self._loader = _LibraryLoaderThread(lib, project_id, parent=self)
        self._loader.loaded.connect(self._on_library_loaded)
        self._loader.load_error.connect(self._on_library_load_error)
        self._loader.loaded.connect(self._loader.deleteLater)
        self._loader.load_error.connect(self._loader.deleteLater)
        self._loader.start()

    def _import_media_pack(self) -> None:
        """Open the Import Media Pack dialog (Tools menu)."""
        collections = []
        for p in self._projects:
            pid  = p["id"]
            root = _from_stored_path(p.get("root", ""))
            cfg  = get_project(pid)
            if cfg and root and os.path.isdir(root):
                collections.append((cfg.display_name, pid, root))

        if not collections:
            QMessageBox.warning(
                self, "No collections configured",
                "Configure at least one collection root in Settings before importing a pack.",
            )
            return

        # Default to eXoDOS if present, otherwise whatever is first.
        default_id = next(
            (pid for _, pid, _ in collections if pid == "exodos"),
            collections[0][1],
        )
        dlg = MediaPackImportDialog(collections, default_id=default_id, parent=self)
        dlg.exec()

    def _redownload_lite(self) -> None:
        """Re-download Lite files for the active collection (Tools menu)."""
        if not self._active_id:
            QMessageBox.warning(self, "No collection", "No active collection is configured.")
            return
        self._start_lite_download(self._active_id)

    def _rebuild_index(self) -> None:
        """Copy torrent into collection and regenerate index.txt (Tools menu)."""
        if not self._active_id:
            QMessageBox.warning(self, "No collection", "No active collection is configured.")
            return
        torrent_path = self._torrent_path_for(self._active_id)
        if not torrent_path or not os.path.isfile(torrent_path):
            QMessageBox.warning(
                self, "No torrent configured",
                "No torrent file found for this collection.\n"
                "Open Settings and set the Torrent path.",
            )
            return
        entry = self._require_project_entry(self._active_id)
        if entry is None:
            return
        cfg, root = entry
        aria_dir = os.path.join(root, "eXo", "util", "aria")
        os.makedirs(aria_dir, exist_ok=True)
        torrent_fname = os.path.basename(torrent_path)
        dest_torrent = os.path.join(aria_dir, torrent_fname)
        if not (os.path.exists(dest_torrent) and os.path.samefile(torrent_path, dest_torrent)):
            shutil.copy2(torrent_path, dest_torrent)
        index_path = os.path.join(aria_dir, "index.txt")
        try:
            from core import aria_index as _ai
            count = _ai.build_index(dest_torrent, index_path)
            display = cfg.display_name if cfg else self._active_id
            QMessageBox.information(
                self, "Index rebuilt",
                f"Indexed {count:,} files for {display}.\n{index_path}",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Index failed", str(exc))

    def _rerun_setup(self) -> None:
        """Re-extract Content archives for the active collection (Tools menu)."""
        cfg, root = self._project_entry(self._active_id)
        if not cfg or not root:
            QMessageBox.warning(self, "No collection", "No active collection is configured.")
            return
        ans = QMessageBox.question(
            self,
            f"Re-setup {cfg.display_name}?",
            f"Re-extract all Content archives for {cfg.display_name}?\n\n"
            "This overwrites existing images, catalogue, and launch scripts "
            "with the versions from the Content archives.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        launcher = self._launchers.get(self._active_id) or self._make_launcher(self._active_id)
        if not launcher:
            return
        if self._active_id not in self._launchers:
            self._connect_launcher(launcher)
            self._launchers[self._active_id] = launcher
        launcher.setup_collection()

    # ── menu build ────────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        mb = self.menuBar()
        assert mb is not None

        file_menu = mb.addMenu("File")
        assert file_menu is not None
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
        assert view_menu is not None

        act_refresh = QAction("Refresh library", self)
        act_refresh.setShortcut(QKeySequence("F5"))
        act_refresh.triggered.connect(self._refresh_library)
        view_menu.addAction(act_refresh)

        view_menu.addSeparator()

        theme_menu = view_menu.addMenu("Theme")
        assert theme_menu is not None
        self._theme_actions: dict[str, QAction] = {}
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        for tname in themes.THEME_NAMES:
            act = QAction(tname, self)
            act.setCheckable(True)
            act.setChecked(tname == themes.current_name())
            act.triggered.connect(lambda _, n=tname: self._switch_theme(n))
            theme_group.addAction(act)
            theme_menu.addAction(act)
            self._theme_actions[tname] = act

        view_menu.addSeparator()

        act_reset_filters = QAction("Reset filters", self)
        act_reset_filters.setShortcut(QKeySequence("Ctrl+Shift+F"))
        act_reset_filters.triggered.connect(self._reset_filters)
        view_menu.addAction(act_reset_filters)

        act_reset_splitter = QAction("Reset split to 50/50", self)
        act_reset_splitter.triggered.connect(self._reset_splitter)
        view_menu.addAction(act_reset_splitter)

        act_reset_win = QAction("Reset window size and position", self)
        act_reset_win.triggered.connect(self._reset_window)
        view_menu.addAction(act_reset_win)

        tools_menu = mb.addMenu("Tools")
        assert tools_menu is not None
        self._act_setup = QAction("Re-setup Collection…", self)
        self._act_setup.setToolTip(
            "Extract Content archives for the active collection,\n"
            "updating images, game catalogue, and launch scripts."
        )
        self._act_setup.triggered.connect(self._rerun_setup)
        tools_menu.addAction(self._act_setup)

        self._act_lite_dl = QAction("Re-download Lite Files…", self)
        self._act_lite_dl.setToolTip(
            "Re-download the Lite metadata subset (images, XML database,\n"
            "launch scripts) for the active collection. Use this to restore\n"
            "missing Content zips or fetch updated Lite files."
        )
        self._act_lite_dl.triggered.connect(self._redownload_lite)
        tools_menu.addAction(self._act_lite_dl)

        self._act_rebuild_index = QAction("Rebuild Torrent Index…", self)
        self._act_rebuild_index.setToolTip(
            "Copy the configured torrent into the collection and regenerate\n"
            "index.txt so individual game ZIPs can be downloaded on demand."
        )
        self._act_rebuild_index.triggered.connect(self._rebuild_index)
        tools_menu.addAction(self._act_rebuild_index)

        tools_menu.addSeparator()
        act_import_pack = QAction("Import eXoDOS Media Pack…", self)
        act_import_pack.setToolTip(
            "Download and extract a supplemental torrent pack\n"
            "(e.g. the eXoDOS Media Pack with books, magazines,\n"
            "soundtracks, and videos) into an existing collection."
        )
        act_import_pack.triggered.connect(self._import_media_pack)
        tools_menu.addAction(act_import_pack)

        tools_menu.addSeparator()
        act_check_deps = QAction("Check Emulator Dependencies…", self)
        act_check_deps.setToolTip(
            "Check which emulator backends are installed and offer\n"
            "to install any missing ones via Homebrew."
        )
        act_check_deps.triggered.connect(self._open_emulator_deps)
        tools_menu.addAction(act_check_deps)

        help_menu = mb.addMenu("Help")
        assert help_menu is not None
        act_about = QAction("About", self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

    def _build_status_bar(self) -> None:
        self._status = self.statusBar()
        assert self._status is not None
        self._status_label = QLabel("Ready")
        self._status.addWidget(self._status_label)

    # ── slots ─────────────────────────────────────────────────────────────────

    @pyqtSlot(object)
    def _on_game_selected(self, game: Game) -> None:
        self._detail_panel.show_game(game)
        self._detail_panel.set_favorite(
            self._favorites_store.is_favorite(self._active_id, game.id or "")
        )
        self._set_status(f"{game.title}  [{game.emulator_display}]")

    @pyqtSlot(str)
    def _on_favorite_toggled(self, game_id: str) -> None:
        self._favorites_store.toggle(self._active_id, game_id)
        self._settings.setValue("favorites", json.dumps(self._favorites_store.to_dict()))
        new_favs = self._favorites_store.get_set(self._active_id)
        list_panel = getattr(self, "_list_panel", None)
        if list_panel is not None:
            list_panel.set_favorites(new_favs)
        detail_panel = getattr(self, "_detail_panel", None)
        if detail_panel is not None:
            detail_panel.set_favorite(game_id in new_favs)

    @pyqtSlot(object)
    def _on_play_requested(self, game: Game) -> None:
        if not self._launcher:
            return
        self._detail_panel.stop_audio()   # release audio device before handing off to emulator

        # ── exception.bsh multi-engine / multi-conf variants ──────────────────
        launch_variants = getattr(game, "launch_variants", [])
        if len(launch_variants) > 1:
            dlg = LaunchVariantDialog(game.title, launch_variants, parent=self)
            if dlg.exec() != LaunchVariantDialog.DialogCode.Accepted:
                return
            chosen = launch_variants[dlg.selected_index]
            self._set_status(f"Launching {game.title}…")
            self._launcher.launch(game, launch_variant=chosen)
            return

        # ── eXoScummVM multi-platform variants (directory choice) ─────────────
        cfg, root = self._project_entry(self._active_id)
        if cfg and root:
            scummvm_dirs = _native_launch.get_scummvm_variants(cfg, root, game)
            if len(scummvm_dirs) > 1:
                from core.game_library import LaunchVariant as _LV
                platform_variants = [
                    _LV(label=d, engine="scummvm", scummvm_path=d)
                    for d in scummvm_dirs
                ]
                dlg = LaunchVariantDialog(game.title, platform_variants, parent=self)
                if dlg.exec() != LaunchVariantDialog.DialogCode.Accepted:
                    return
                variant = scummvm_dirs[dlg.selected_index]
                self._set_status(f"Launching {game.title}…")
                self._launcher.launch(game, variant=variant)
                return

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

    @pyqtSlot(object)
    def _on_uninstall_requested(self, game: Game) -> None:
        reply = QMessageBox.question(
            self, "Uninstall game",
            f"Remove installed files for '{game.title}'?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes and self._launcher:
            self._set_status(f"Uninstalling {game.title}…")
            self._launcher.uninstall(game)

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
    def _on_install_progress(self, _game_id: str, current: int, total: int) -> None:
        self._detail_panel.set_installing(current, total)

    @pyqtSlot(str, bool, str)
    def _on_install_finished(self, game_id: str, success: bool, msg: str) -> None:
        lib = self._library
        game = lib.get_by_id(game_id) if lib else None
        if game:
            game.installed = success
        if success and game and lib:
            lib.refresh_game_extras(game)
            self._detail_panel.show_game(game)
        else:
            self._detail_panel.set_install_done(success, msg)
        if hasattr(self, "_list_panel"):
            self._list_panel.refresh()
        if success:
            name = game.title if game else game_id
            self._set_status(f"Installed: {name}")
        else:
            QMessageBox.warning(self, "Install error", msg)

    @pyqtSlot(str, bool, str)
    def _on_uninstall_finished(self, game_id: str, success: bool, msg: str) -> None:
        lib = self._library
        game = lib.get_by_id(game_id) if lib else None
        if game and success:
            game.installed = False
        self._detail_panel.set_uninstall_done(success, msg)
        if hasattr(self, "_list_panel"):
            self._list_panel.refresh()
        if success:
            name = game.title if game else game_id
            self._set_status(f"Uninstalled: {name}")
        else:
            QMessageBox.warning(self, "Uninstall error", msg)

    @pyqtSlot(str, str)
    def _on_fetch_phase(self, _game_id: str, phase: str) -> None:
        self._detail_panel.set_fetch_phase(phase)
        self._set_status(phase)

    @pyqtSlot(str, int, int)
    def _on_fetch_progress(self, _game_id: str, current: int, total: int) -> None:
        self._detail_panel.set_installing(current, total)

    @pyqtSlot(str, bool, str)
    def _on_fetch_finished(self, game_id: str, success: bool, msg: str) -> None:
        lib = self._library
        game = lib.get_by_id(game_id) if lib else None
        if game and success:
            game.installed = True
            game.zip_present = True
        if success and game and lib:
            lib.refresh_game_extras(game)
            self._detail_panel.show_game(game)
        else:
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

    @pyqtSlot(str)
    def _on_lite_phase(self, phase: str) -> None:
        if hasattr(self, "_lite_phase_label"):
            self._lite_phase_label.setText(phase)
        self._set_status(phase)

    @pyqtSlot(int, int)
    def _on_lite_progress(self, current: int, total: int) -> None:
        if hasattr(self, "_lite_bar"):
            if total > 0:
                self._lite_bar.setRange(0, total)
                self._lite_bar.setValue(current)
            else:
                self._lite_bar.setRange(0, 0)

    @pyqtSlot(bool, str)
    def _on_lite_finished(self, success: bool, msg: str) -> None:
        project_id = getattr(self, "_lite_project_id", self._active_id)
        if success:
            self._set_status(f"{msg} Loading library…")
            self._active_id = project_id
            self._loading = LoadingWidget(self)
            self._set_content_page(self._loading)
            QTimer.singleShot(100, self._load_active_project)
        else:
            QMessageBox.critical(self, "Lite download failed", msg)
            self._show_no_library_ui(project_id, msg)

    @pyqtSlot()
    def _on_lite_cancelled(self) -> None:
        project_id = getattr(self, "_lite_project_id", self._active_id)
        self._set_status("Lite download cancelled.")
        self._show_no_library_ui(project_id)

    # ── menu actions ──────────────────────────────────────────────────────────

    def _switch_theme(self, name: str) -> None:
        app = QApplication.instance()
        assert isinstance(app, QApplication)
        themes.set_theme(name, app)
        self._settings.setValue("theme", name)
        for tname, act in self._theme_actions.items():
            act.setChecked(tname == name)
        if hasattr(self, "_list_panel"):
            self._list_panel.apply_theme()
        if hasattr(self, "_detail_panel"):
            self._detail_panel.rebuild_ui()

    def _reset_filters(self) -> None:
        if hasattr(self, "_list_panel"):
            self._list_panel.reset_filters()

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
            # Drop all panel references. _splitter may be orphaned (setParent(None)
            # inside _set_content_page), in which case setCentralWidget won't delete
            # it — explicit deleteLater ensures it's cleaned up regardless.
            for _attr in ("_list_panel", "_detail_panel", "_splitter",
                          "_page_container", "_project_tabs"):
                if hasattr(self, _attr):
                    widget_ref = getattr(self, _attr)
                    widget_ref.deleteLater()
                    delattr(self, _attr)
            self._loading = LoadingWidget(self)
            self.setCentralWidget(self._loading)
            QTimer.singleShot(100, self._load_active_project)
        else:
            self._show_no_project_ui()

    def _refresh_library(self) -> None:
        if not self._active_id:
            return
        lib = self._make_library(self._active_id)
        if lib is None:
            return
        self._set_status("Refreshing…")
        project_id = self._active_id

        def _on_refresh_done(refreshed_lib, pid: str) -> None:
            if pid != self._active_id:
                return
            self._libraries[pid] = refreshed_lib
            if hasattr(self, "_list_panel"):
                self._list_panel.set_library(refreshed_lib)
            self._set_status(f"Refreshed: {len(refreshed_lib.games):,} games")

        self._refresh_loader = _LibraryLoaderThread(lib, project_id, force_reload=True, parent=self)
        self._refresh_loader.loaded.connect(_on_refresh_done)
        self._refresh_loader.loaded.connect(self._refresh_loader.deleteLater)
        self._refresh_loader.load_error.connect(self._refresh_loader.deleteLater)
        self._refresh_loader.start()

    def _open_emulator_deps(self) -> None:
        from gui.emulator_check_dialog import EmulatorCheckDialog
        dlg = EmulatorCheckDialog(self)
        dlg.exec()

    def _show_about(self) -> None:
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME} (Qt)</b><br>"
            f"v{APP_VERSION} (alpha)<br><br>"
            "A Python/PyQt6 GUI launcher for eXo retro-game collections.<br>"
            "Supports all eXo projects independently — no merging required.<br>"
            "Runs on macOS and Linux.<br><br>"
            "Based on the eXo projects by The eXo Team (retro-exo.com)."
        )

    # ── window lifecycle ──────────────────────────────────────────────────────

    def closeEvent(self, a0) -> None:  # noqa: N802
        self._save_project_ui_state(self._active_id)
        self._settings.setValue("window/geometry", self.saveGeometry())
        if hasattr(self, "_splitter"):
            self._settings.setValue("window/splitter", self._splitter.saveState())
        super().closeEvent(a0)

    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)
