"""
emulator_check_dialog.py — Emulator status, download links, and Homebrew install.

Opens from Tools → "Check Emulator Dependencies…" and from the "Get Emulators…"
button in Settings.  Shows which backends are installed, lets you install
Homebrew-available ones in-app, and provides download links for the rest.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)
from PyQt6.QtCore import QUrl

from core.emulator_deps import (
    EMULATOR_GROUPS, EmulatorSpec, check_emulator,
)
from core.project import ALL_PROJECTS


_PROJ_NAMES: dict[str, str] = {p.id: p.display_name for p in ALL_PROJECTS}


# ── Homebrew runner ───────────────────────────────────────────────────────────

class _BrewSignals(QObject):
    output   = pyqtSignal(str)  # one line of brew stdout/stderr
    finished = pyqtSignal(int)  # return code


class _BrewRunner(QRunnable):
    """Run `brew install [--cask] …` and stream output via signals."""

    def __init__(self, formulas: list[str], casks: list[str], signals: _BrewSignals):
        super().__init__()
        self._formulas = formulas
        self._casks    = casks
        self._signals  = signals
        self.setAutoDelete(True)

    @staticmethod
    def _brew_env() -> dict[str, str]:
        parts = list(os.environ.get("PATH", "").split(os.pathsep))
        for prefix in ("/opt/homebrew", "/usr/local"):
            for sub in ("bin", "sbin"):
                p = f"{prefix}/{sub}"
                if os.path.isdir(p) and p not in parts:
                    parts.insert(0, p)
        return {**os.environ, "PATH": os.pathsep.join(parts), "HOMEBREW_NO_AUTO_UPDATE": "1"}

    @pyqtSlot()
    def run(self) -> None:
        env = self._brew_env()
        brew = shutil.which("brew", path=env["PATH"])
        if not brew:
            self._signals.output.emit("❌  brew not found — install Homebrew first: https://brew.sh/")
            self._signals.finished.emit(1)
            return

        rc = 0
        if self._formulas:
            rc = rc or self._install(brew, self._formulas, cask=False, env=env)
        if self._casks:
            rc = rc or self._install(brew, self._casks, cask=True, env=env)
        self._signals.finished.emit(rc)

    def _install(self, brew: str, pkgs: list[str], cask: bool, env: dict) -> int:
        cmd = [brew, "install"] + (["--cask"] if cask else []) + pkgs
        self._signals.output.emit("$ " + " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, env=env,
            )
            for line in proc.stdout:  # type: ignore[union-attr]
                self._signals.output.emit(line.rstrip())
            proc.wait()
            return proc.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            self._signals.output.emit(f"Error: {exc}")
            return 1


# ── per-emulator row ──────────────────────────────────────────────────────────

class _Row(QWidget):
    """One entry: status dot | display name + used-by/notes | action button."""

    install_requested = pyqtSignal(object)  # EmulatorSpec

    def __init__(self, spec: EmulatorSpec, parent=None):
        super().__init__(parent)
        self._spec = spec

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(10)

        # Status dot
        self._dot = QLabel("●")
        self._dot.setFixedWidth(18)
        self._dot.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Name / info block
        info = QVBoxLayout()
        info.setSpacing(1)
        info.addWidget(QLabel(
            f"<b>{spec.display_name}</b>  "
            f"<span style='color:gray;font-size:10px;'>{spec.name}</span>"
        ))
        used_by = ", ".join(_PROJ_NAMES.get(pid, pid) for pid in spec.projects)
        sub = QLabel(f"<span style='color:gray;font-size:11px;'>{used_by}</span>")
        sub.setWordWrap(True)
        info.addWidget(sub)
        if spec.notes:
            note = QLabel(f"<span style='color:gray;font-size:10px;'>{spec.notes}</span>")
            note.setWordWrap(True)
            info.addWidget(note)

        info_w = QWidget()
        info_w.setLayout(info)
        info_w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        # Action button
        self._btn = QPushButton()
        self._btn.setFixedWidth(160)
        self._btn.setVisible(False)
        self._btn.clicked.connect(self._on_action)

        layout.addWidget(self._dot)
        layout.addWidget(info_w, 1)
        layout.addWidget(self._btn)

        self.recheck()

    # ── state ─────────────────────────────────────────────────────────────────

    def recheck(self) -> None:
        found = check_emulator(self._spec)
        if found:
            self.set_found(found)
        else:
            self.set_not_found()

    def set_found(self, path: str) -> None:
        self._dot.setStyleSheet("color: green;")
        self._dot.setToolTip(f"Found: {path}")
        self._btn.setVisible(False)

    def set_not_found(self) -> None:
        self._dot.setStyleSheet("color: red;")
        self._dot.setToolTip("Not found")
        spec = self._spec
        if spec.brew_formula or spec.brew_cask:
            pkg  = spec.brew_cask or spec.brew_formula
            flag = "--cask " if spec.brew_cask else ""
            self._btn.setText("Install via Homebrew")
            self._btn.setToolTip(f"brew install {flag}{pkg}")
            self._btn.setEnabled(True)
            self._btn.setVisible(True)
        elif spec.download_url:
            self._btn.setText("Download…")
            self._btn.setToolTip(spec.download_url)
            self._btn.setEnabled(True)
            self._btn.setVisible(True)
        else:
            self._btn.setVisible(False)

    def set_installing(self) -> None:
        self._dot.setStyleSheet("color: darkorange;")
        self._dot.setToolTip("Installing…")
        self._btn.setText("Installing…")
        self._btn.setEnabled(False)

    # ── internal ──────────────────────────────────────────────────────────────

    def _on_action(self) -> None:
        spec = self._spec
        if spec.brew_formula or spec.brew_cask:
            self.install_requested.emit(spec)
        elif spec.download_url:
            QDesktopServices.openUrl(QUrl(spec.download_url))


# ── dialog ────────────────────────────────────────────────────────────────────

class EmulatorCheckDialog(QDialog):
    """
    Emulator status overview with in-app Homebrew installation.

    Brew-available emulators can be installed with one click; others get a
    download link.  A live log area appears at the bottom during installation.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Emulator Backends")
        self.setMinimumSize(680, 520)

        pool = QThreadPool.globalInstance()
        assert pool is not None
        self._pool: QThreadPool = pool

        self._brew_sigs = _BrewSignals(self)
        self._brew_sigs.output.connect(self._append_log)
        self._brew_sigs.finished.connect(self._on_brew_finished)

        self._rows: dict[str, _Row] = {}
        self._installing: Optional[EmulatorSpec] = None

        self._build_ui()

    # ── build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        desc = QLabel(
            "Status of emulator backends required by eXo collections. "
            "Homebrew-available emulators can be installed here; "
            "others open their download page."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        # ── scrollable grouped rows ───────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(4)

        for group in EMULATOR_GROUPS:
            specs = group.specs()
            if not specs:
                continue
            hdr = QLabel(f"<span style='color:gray;font-size:11px;'><b>{group.label}</b></span>")
            hdr.setContentsMargins(10, 8, 0, 2)
            cl.addWidget(hdr)
            for i, spec in enumerate(specs):
                if i > 0:
                    div = QFrame()
                    div.setFrameShape(QFrame.Shape.HLine)
                    cl.addWidget(div)
                row = _Row(spec)
                row.install_requested.connect(self._start_brew)
                self._rows[spec.name] = row
                cl.addWidget(row)
            grp_div = QFrame()
            grp_div.setFrameShape(QFrame.Shape.HLine)
            cl.addWidget(grp_div)

        cl.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        # ── brew log (hidden until first install) ─────────────────────────────
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFixedHeight(130)
        self._log.setVisible(False)
        if sys.platform == "darwin":
            self._log.setFontFamily("Menlo")
        layout.addWidget(self._log)

        # ── bottom buttons ────────────────────────────────────────────────────
        btn_box = QDialogButtonBox()
        recheck_btn = btn_box.addButton(
            "Re-check All", QDialogButtonBox.ButtonRole.ActionRole
        )
        assert recheck_btn is not None
        self._recheck_btn: QPushButton = recheck_btn
        self._recheck_btn.clicked.connect(self._recheck_all)
        btn_box.addButton(QDialogButtonBox.StandardButton.Close)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    # ── install ───────────────────────────────────────────────────────────────

    def _start_brew(self, spec: EmulatorSpec) -> None:
        self._installing = spec
        row = self._rows.get(spec.name)
        if row:
            row.set_installing()

        self._log.setVisible(True)
        self._log.clear()
        self._recheck_btn.setEnabled(False)

        formulas = [spec.brew_formula] if spec.brew_formula else []
        casks    = [spec.brew_cask]    if spec.brew_cask    else []
        self._pool.start(_BrewRunner(formulas, casks, self._brew_sigs))

    @pyqtSlot(str)
    def _append_log(self, line: str) -> None:
        self._log.append(line)
        sb = self._log.verticalScrollBar()
        if sb is not None:
            sb.setValue(sb.maximum())

    @pyqtSlot(int)
    def _on_brew_finished(self, rc: int) -> None:
        self._recheck_btn.setEnabled(True)
        self._log.append("\n✓  Done." if rc == 0 else f"\n✗  brew exited with code {rc}.")
        if self._installing:
            row = self._rows.get(self._installing.name)
            if row:
                row.recheck()
            self._installing = None

    # ── re-check ──────────────────────────────────────────────────────────────

    def _recheck_all(self) -> None:
        for row in self._rows.values():
            row.recheck()
