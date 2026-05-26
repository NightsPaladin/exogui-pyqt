"""
media_pack_dialog.py — Dialog for importing a supplemental torrent
(e.g. the eXoDOS Media Pack) into an existing collection.

Drives LiteDownloadTask directly so it does not interfere with the main
window's library-loading state.  Progress is shown in-dialog; the main
library view is untouched throughout.
"""

from __future__ import annotations

import os

from PyQt6.QtCore import Qt, QThreadPool, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QFileDialog, QProgressBar, QScrollArea,
    QWidget, QComboBox, QDialogButtonBox, QFrame,
)

from core.torrent import parse as parse_torrent
from core.launcher import LiteDownloadTask, LiteDownloadSignals


def _fmt(n: int) -> str:
    if n >= 1_073_741_824:
        return f"{n / 1_073_741_824:.1f} GiB"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.0f} MiB"
    if n >= 1_024:
        return f"{n // 1_024} KiB"
    return f"{n} B"


class MediaPackImportDialog(QDialog):
    """
    Import a supplemental torrent pack into a configured collection.

    Parameters
    ----------
    collections : list of (display_name, root_path)
        All configured projects with valid root paths.
    default_id : str
        Collection id to pre-select (e.g. "exodos").
    """

    def __init__(
        self,
        collections: list[tuple[str, str, str]],   # (display_name, project_id, root)
        default_id: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Import Media Pack")
        self.setMinimumWidth(560)
        self.setMinimumHeight(420)

        self._collections = collections
        self._task: LiteDownloadTask | None = None
        self._signals: LiteDownloadSignals | None = None

        self._build_ui(default_id)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self, default_id: str) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setSpacing(10)

        # Description
        desc = QLabel(
            "Select a supplemental torrent pack and the collection to import it into.\n"
            "Everything except individual game ZIPs and Windows-only files will be\n"
            "downloaded and extracted into the collection root."
        )
        desc.setWordWrap(True)
        root_layout.addWidget(desc)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        root_layout.addWidget(sep)

        # Collection selector
        col_row = QHBoxLayout()
        col_row.addWidget(QLabel("Collection:"))
        self._col_combo = QComboBox()
        for name, pid, _ in self._collections:
            self._col_combo.addItem(name, pid)
        if default_id:
            idx = next(
                (i for i, (_, pid, _) in enumerate(self._collections)
                 if pid == default_id), 0
            )
            self._col_combo.setCurrentIndex(idx)
        col_row.addWidget(self._col_combo, 1)
        root_layout.addLayout(col_row)

        # Torrent file picker
        torrent_row = QHBoxLayout()
        torrent_row.addWidget(QLabel("Torrent:"))
        self._torrent_edit = QLineEdit()
        self._torrent_edit.setPlaceholderText("Select a .torrent file…")
        self._torrent_edit.setReadOnly(True)
        self._browse_btn = QPushButton("Browse…")
        self._browse_btn.setFixedWidth(80)
        self._browse_btn.clicked.connect(self._on_browse)
        torrent_row.addWidget(self._torrent_edit, 1)
        torrent_row.addWidget(self._browse_btn)
        root_layout.addLayout(torrent_row)

        # Torrent info panel (file list)
        self._info_label = QLabel("No torrent selected.")
        self._info_label.setStyleSheet("color: gray; font-size: 11px;")
        root_layout.addWidget(self._info_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(140)
        scroll.setFrameShape(QFrame.Shape.StyledPanel)
        self._file_list_widget = QWidget()
        self._file_list_layout = QVBoxLayout(self._file_list_widget)
        self._file_list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._file_list_layout.setSpacing(2)
        scroll.setWidget(self._file_list_widget)
        root_layout.addWidget(scroll)

        # Progress area (hidden until download starts)
        self._phase_label = QLabel("")
        self._phase_label.setStyleSheet("font-size: 11px; font-style: italic;")
        self._phase_label.hide()
        root_layout.addWidget(self._phase_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.hide()
        root_layout.addWidget(self._progress_bar)

        # Buttons
        btn_box = QDialogButtonBox()
        self._download_btn = QPushButton("Download && Install")
        self._download_btn.setEnabled(False)
        self._download_btn.clicked.connect(self._start_download)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_box.addButton(self._download_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        btn_box.addButton(self._cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
        root_layout.addWidget(btn_box)

    # ── slots ─────────────────────────────────────────────────────────────────

    def _on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Media Pack Torrent",
            os.path.expanduser("~"),
            "Torrent files (*.torrent);;All files (*)",
        )
        if path:
            self._torrent_edit.setText(path)
            self._refresh_torrent_info(path)

    def _refresh_torrent_info(self, path: str) -> None:
        # Clear old file list
        while self._file_list_layout.count():
            item = self._file_list_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        try:
            info = parse_torrent(path)
        except Exception as exc:
            self._info_label.setText(f"Could not parse torrent: {exc}")
            self._download_btn.setEnabled(False)
            return

        lite_set = set(info.select_lite())
        lite_files = [f for f in info.files if f.index in lite_set]
        total_size = sum(f.size for f in lite_files)

        self._info_label.setText(
            f"{info.name}  ·  {len(lite_files)} files to download  ·  {_fmt(total_size)}"
        )

        for f in lite_files:
            row = QLabel(f"  {f.path}  ({_fmt(f.size)})")
            row.setStyleSheet("font-size: 11px; font-family: monospace;")
            self._file_list_layout.addWidget(row)

        self._download_btn.setEnabled(bool(lite_files))

    def _start_download(self) -> None:
        torrent_path = self._torrent_edit.text()
        idx = self._col_combo.currentIndex()
        if idx < 0 or idx >= len(self._collections):
            return
        _, _, collection_root = self._collections[idx]

        self._signals = LiteDownloadSignals()
        self._signals.phase.connect(self._on_phase)
        self._signals.progress.connect(self._on_progress)
        self._signals.finished.connect(self._on_finished)
        self._signals.cancelled.connect(self._on_cancelled)

        self._task = LiteDownloadTask(
            torrent_path=torrent_path,
            collection_root=collection_root,
            signals=self._signals,
        )
        QThreadPool.globalInstance().start(self._task)

        # Switch to downloading state
        self._browse_btn.setEnabled(False)
        self._col_combo.setEnabled(False)
        self._download_btn.setEnabled(False)
        self._cancel_btn.setText("Abort")
        self._phase_label.show()
        self._progress_bar.show()

    def _on_cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
        else:
            self.reject()

    # ── task signal handlers ──────────────────────────────────────────────────

    @pyqtSlot(str)
    def _on_phase(self, phase: str) -> None:
        self._phase_label.setText(phase)

    @pyqtSlot(int, int)
    def _on_progress(self, current: int, total: int) -> None:
        if total > 0:
            self._progress_bar.setValue(int(current / total * 100))

    @pyqtSlot(bool, str)
    def _on_finished(self, success: bool, msg: str) -> None:
        self._task = None
        self._progress_bar.setValue(100 if success else 0)
        self._progress_bar.hide()
        self._phase_label.hide()
        self._cancel_btn.setText("Close")

        if success:
            self._info_label.setText(f"Done.  {msg}")
            self._info_label.setStyleSheet("color: green; font-size: 11px;")
        else:
            self._info_label.setText(f"Failed: {msg}")
            self._info_label.setStyleSheet("color: red; font-size: 11px;")

    @pyqtSlot()
    def _on_cancelled(self) -> None:
        self._task = None
        self._phase_label.hide()
        self._progress_bar.hide()
        self._info_label.setText("Download cancelled.")
        self._info_label.setStyleSheet("color: orange; font-size: 11px;")
        self._cancel_btn.setText("Close")

    def closeEvent(self, event) -> None:
        if self._task is not None:
            self._task.cancel()
        super().closeEvent(event)
