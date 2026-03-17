"""
launcher.py — Launch and install eXo collection games on macOS and Linux.

Games are launched via eXo/util/launch_helper.sh which sets the required
environment variables (gamedir, gamename, indexname, var) and delegates to
the platform-appropriate launcher:
  macOS: eXo/util/launch.msh
  Linux: eXo/util/launch.bsh

Install = extract the game's zip archive into the collection's game_data_subdir.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot

from core.project import ProjectConfig, EXODOS
from core import aria_index as _aria_index


# ── signals carrier ──────────────────────────────────────────────────────────

class LaunchSignals(QObject):
    started  = pyqtSignal(str)          # game_id
    finished = pyqtSignal(str, int)     # game_id, return_code
    error    = pyqtSignal(str, str)     # game_id, message


class InstallSignals(QObject):
    progress = pyqtSignal(str, int, int)    # game_id, current, total
    finished = pyqtSignal(str, bool, str)   # game_id, success, message


class FetchSignals(QObject):
    """Signals for FetchTask (acquire ZIP then extract)."""
    phase_changed = pyqtSignal(str, str)        # game_id, phase label
    progress      = pyqtSignal(str, int, int)   # game_id, current, total (bytes or files)
    finished      = pyqtSignal(str, bool, str)  # game_id, success, message
    cancelled     = pyqtSignal(str)             # game_id


# ── runnable tasks ───────────────────────────────────────────────────────────

class LaunchTask(QRunnable):
    def __init__(self, game_id: str, helper: str, gamedir: str, gamename: str,
                 signals: LaunchSignals):
        super().__init__()
        self.game_id = game_id
        self.helper = helper
        self.gamedir = gamedir
        self.gamename = gamename
        self.signals = signals
        self.setAutoDelete(True)

    @pyqtSlot()
    def run(self):
        self.signals.started.emit(self.game_id)
        try:
            env = os.environ.copy()

            if sys.platform == "darwin":
                # Guarantee Homebrew tools (bash 5, aria2c, wget…) are on PATH
                # regardless of how the GUI was launched.
                for _prefix in ("/opt/homebrew", "/usr/local"):  # Apple Silicon, Intel
                    for _sub in ("bin", "sbin"):
                        _p = f"{_prefix}/{_sub}"
                        if os.path.isdir(_p) and _p not in env.get("PATH", ""):
                            env["PATH"] = _p + ":" + env["PATH"]

            # Suppress macOS Ctrl+Arrow shortcuts (Spaces / Mission Control)
            # while the game runs so they don't steal input from DOSBox.
            # All DOSBox F-key controls (Ctrl+F1, Ctrl+F10 …) are unaffected.
            from core.mac_shortcuts import disable_conflicting_shortcuts, restore_shortcuts
            _saved_shortcuts = disable_conflicting_shortcuts()
            try:
                result = subprocess.run(
                    ["/usr/bin/env", "bash", self.helper, self.gamedir, self.gamename],
                    env=env,
                    # No explicit cwd — the helper itself cd's to the right place
                )
            finally:
                restore_shortcuts(_saved_shortcuts)

            self.signals.finished.emit(self.game_id, result.returncode)
        except Exception as exc:
            self.signals.error.emit(self.game_id, str(exc))


class InstallTask(QRunnable):
    def __init__(self, game_id: str, zip_path: str, dest_dir: str,
                 signals: InstallSignals):
        super().__init__()
        self.game_id = game_id
        self.zip_path = zip_path
        self.dest_dir = dest_dir
        self.signals = signals
        self.setAutoDelete(True)

    @pyqtSlot()
    def run(self):
        try:
            with zipfile.ZipFile(self.zip_path, "r") as zf:
                members = zf.namelist()
                total = len(members)
                for i, member in enumerate(members):
                    zf.extract(member, self.dest_dir)
                    self.signals.progress.emit(self.game_id, i + 1, total)
            self.signals.finished.emit(self.game_id, True, "")
        except Exception as exc:
            zip_name = os.path.basename(self.zip_path)
            self.signals.finished.emit(
                self.game_id, False,
                f"Failed to extract '{zip_name}':\n{exc}"
            )


# ── aria2c progress parsing ───────────────────────────────────────────────────

_ARIA_PCT_RE = re.compile(r"\((\d{1,3})%\)")


def _parse_aria_progress(line: str) -> Optional[int]:
    """Extract completion percentage from an aria2c status line, or None."""
    m = _ARIA_PCT_RE.search(line)
    return int(m.group(1)) if m else None


# ── fetch task (acquire ZIP via local copy or torrent, then extract) ──────────

class FetchTask(QRunnable):
    """
    Download (or copy) a game ZIP then extract it.

    Acquisition priority:
      1. Local/network source path — look for ``<gamename>.zip`` there and copy
         it to the project ZIP directory.
      2. Torrent fallback — use aria2c to download the specific file from the
         full project torrent (requires ``index.txt`` and the ``.torrent`` file).

    Emits FetchSignals throughout both phases.
    """

    def __init__(
        self,
        game_id:         str,
        gamename:        str,          # e.g. "Dune 2 - The Building of a Dynasty (1992)"
        zip_dest_dir:    str,          # where to place the downloaded ZIP
        extract_dir:     str,          # where to extract (usually same as zip_dest_dir)
        aria_index_path: str,
        torrent_path:    str,
        zip_source_path: str,          # user-configured local/network path (may be "")
        signals:         FetchSignals,
    ):
        super().__init__()
        self.game_id         = game_id
        self.gamename        = gamename
        self.zip_dest_dir    = zip_dest_dir
        self.extract_dir     = extract_dir
        self.aria_index_path = aria_index_path
        self.torrent_path    = torrent_path
        self.zip_source_path = zip_source_path
        self.signals         = signals
        self._cancel         = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self.setAutoDelete(True)

    def cancel(self) -> None:
        """Signal this task to stop at the next cancellation checkpoint."""
        self._cancel.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    @pyqtSlot()
    def run(self) -> None:
        zip_filename  = self.gamename + ".zip"
        final_zip     = os.path.join(self.zip_dest_dir, zip_filename)

        # Phase 1: acquire ZIP ────────────────────────────────────────────────
        try:
            acquired = self._acquire(zip_filename, final_zip)
        except Exception as exc:
            self.signals.finished.emit(self.game_id, False, str(exc))
            return

        if not acquired:
            if self._cancel.is_set():
                self.signals.cancelled.emit(self.game_id)
            else:
                self.signals.finished.emit(
                    self.game_id, False,
                    f"Could not acquire '{zip_filename}'.\n\n"
                    "The file was not found at the configured ZIP source path, "
                    "and the torrent download failed or was unavailable.\n\n"
                    "Please check your ZIP Source setting in preferences, or ensure "
                    "aria2c is installed and the torrent file is present."
                )
            return

        # Phase 2: extract ────────────────────────────────────────────────────
        self.signals.phase_changed.emit(self.game_id, "Extracting…")
        try:
            with zipfile.ZipFile(final_zip, "r") as zf:
                members = zf.namelist()
                total = len(members)
                for i, member in enumerate(members):
                    if self._cancel.is_set():
                        self.signals.cancelled.emit(self.game_id)
                        return
                    zf.extract(member, self.extract_dir)
                    self.signals.progress.emit(self.game_id, i + 1, total)
        except Exception as exc:
            self.signals.finished.emit(
                self.game_id, False,
                f"Failed to extract '{zip_filename}':\n{exc}"
            )
            return

        self.signals.finished.emit(self.game_id, True, "")

    # ── acquisition helpers ───────────────────────────────────────────────────

    def _acquire(self, zip_filename: str, final_zip: str) -> bool:
        """
        Try to get the ZIP into *final_zip*.  Returns True on success.

        Tries local source first; falls back to torrent automatically.
        """
        os.makedirs(self.zip_dest_dir, exist_ok=True)

        # 1. Local/network source
        if self.zip_source_path:
            source_zip = os.path.join(self.zip_source_path, zip_filename)
            if os.path.isfile(source_zip):
                return self._copy_from_source(source_zip, final_zip)
            # Not found at source — fall through silently to torrent

        # 2. Torrent via aria2c
        return self._download_torrent(zip_filename, final_zip)

    def _copy_from_source(self, source_zip: str, final_zip: str) -> bool:
        """Copy *source_zip* to *final_zip* with byte-level progress."""
        self.signals.phase_changed.emit(
            self.game_id,
            f"Copying from {os.path.dirname(source_zip)}…",
        )
        try:
            total = os.path.getsize(source_zip)
            copied = 0
            chunk = 1 << 20  # 1 MiB chunks
            # Write to a temp file then rename atomically
            tmp = final_zip + ".tmp"
            with open(source_zip, "rb") as src, open(tmp, "wb") as dst:
                while True:
                    if self._cancel.is_set():
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                        return False
                    buf = src.read(chunk)
                    if not buf:
                        break
                    dst.write(buf)
                    copied += len(buf)
                    if total > 0:
                        self.signals.progress.emit(self.game_id, copied, total)
            os.replace(tmp, final_zip)
            return True
        except OSError as exc:
            # Clean up temp if it exists
            try:
                os.unlink(final_zip + ".tmp")
            except OSError:
                pass
            raise RuntimeError(f"Failed to copy ZIP from source:\n{exc}") from exc

    def _download_torrent(self, zip_filename: str, final_zip: str) -> bool:
        """Download *zip_filename* from the project torrent using aria2c."""
        if not self.torrent_path or not os.path.isfile(self.torrent_path):
            return False

        if self._cancel.is_set():
            return False

        aria2c_cmd = _aria_index.find_aria2c()
        if not aria2c_cmd:
            return False

        index = _aria_index.load_index(self.aria_index_path)
        entry = index.get(self.gamename)
        if not entry:
            return False

        self.signals.phase_changed.emit(self.game_id, "Downloading via torrent…")
        self.signals.progress.emit(self.game_id, 0, 100)

        # aria2c must run from a temp working directory so it doesn't litter
        # the project root with partial files.
        with tempfile.TemporaryDirectory(prefix="exogui_dl_") as tmpdir:
            argv = _aria_index.build_aria2c_command(
                aria2c_cmd,
                self.torrent_path,
                entry.game_index,
                zip_filename,
            )

            env = os.environ.copy()
            if sys.platform == "darwin":
                for prefix in ("/opt/homebrew", "/usr/local"):
                    for sub in ("bin", "sbin"):
                        p = f"{prefix}/{sub}"
                        if os.path.isdir(p) and p not in env.get("PATH", ""):
                            env["PATH"] = p + ":" + env["PATH"]

            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=tmpdir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env,
                )
                self._proc = proc
                for line in proc.stdout:  # type: ignore[union-attr]
                    if self._cancel.is_set():
                        proc.terminate()
                        break
                    pct = _parse_aria_progress(line)
                    if pct is not None:
                        self.signals.progress.emit(self.game_id, pct, 100)
                proc.wait()
            except (OSError, subprocess.SubprocessError):
                return False
            finally:
                self._proc = None

            if proc.returncode != 0:
                return False

            # Find the downloaded file (aria2c may create subdirs)
            downloaded = None
            for dirpath, _dirs, files in os.walk(tmpdir):
                for fname in files:
                    if fname == zip_filename:
                        downloaded = os.path.join(dirpath, fname)
                        break

            if not downloaded or not os.path.isfile(downloaded):
                return False

            # Verify non-empty
            if os.path.getsize(downloaded) == 0:
                return False

            shutil.move(downloaded, final_zip)
            return True

class Launcher(QObject):
    """
    Manages launching and installing eXo collection games.

    Parameters
    ----------
    root : str
        Absolute path to the collection root.
    config : ProjectConfig, optional
        Describes the collection's path layout.  Defaults to EXODOS.
    zip_source_path : str, optional
        Path to a local/network directory containing game ZIP files.  Used as
        the first acquisition source in Lite mode before falling back to the
        project torrent.
    """

    # Re-exported signals for convenience
    launch_started   = pyqtSignal(str)
    launch_finished  = pyqtSignal(str, int)
    launch_error     = pyqtSignal(str, str)
    install_progress = pyqtSignal(str, int, int)
    install_finished = pyqtSignal(str, bool, str)
    fetch_phase      = pyqtSignal(str, str)        # game_id, phase label
    fetch_progress   = pyqtSignal(str, int, int)   # game_id, current, total
    fetch_finished   = pyqtSignal(str, bool, str)  # game_id, success, message
    fetch_cancelled  = pyqtSignal(str)             # game_id

    def __init__(self, root: str, config: Optional[ProjectConfig] = None,
                 zip_source_path: str = "", parent=None):
        super().__init__(parent)
        self.root             = root
        self._config          = config if config is not None else EXODOS
        self._zip_source_path = zip_source_path
        self._pool            = QThreadPool.globalInstance()
        self._active_fetch_task: Optional[FetchTask] = None

        self._launch_signals = LaunchSignals()
        self._launch_signals.started.connect(self.launch_started)
        self._launch_signals.finished.connect(self.launch_finished)
        self._launch_signals.error.connect(self.launch_error)

        self._install_signals = InstallSignals()
        self._install_signals.progress.connect(self.install_progress)
        self._install_signals.finished.connect(self.install_finished)

        self._fetch_signals = FetchSignals()
        self._fetch_signals.phase_changed.connect(self.fetch_phase)
        self._fetch_signals.progress.connect(self.fetch_progress)
        self._fetch_signals.finished.connect(self.fetch_finished)
        self._fetch_signals.finished.connect(self._clear_fetch_task)
        self._fetch_signals.cancelled.connect(self.fetch_cancelled)
        self._fetch_signals.cancelled.connect(self._clear_fetch_task)

    def _clear_fetch_task(self, *_) -> None:
        self._active_fetch_task = None

    # ── public ────────────────────────────────────────────────────────────────

    def launch(self, game) -> bool:
        """
        Launch *game* via launch_helper.sh → launch.msh (macOS) or launch.bsh (Linux).

        Returns False if the game is not installed or the helper cannot be found.
        """
        gamedir, gamename = self._find_gamedir_and_name(game)
        if not gamedir:
            self.launch_error.emit(game.id, f"Game folder not found for '{game.title}'")
            return False

        helper = os.path.join(self.root, "eXo", "util", "launch_helper.sh")
        if not os.path.isfile(helper):
            self.launch_error.emit(game.id, "launch_helper.sh not found")
            return False

        task = LaunchTask(game.id, helper, gamedir, gamename, self._launch_signals)
        self._pool.start(task)
        return True

    def install(self, game) -> bool:
        """
        Extract the game's ZIP archive into the project's game data directory.

        Returns False if the ZIP cannot be found.
        """
        zip_path, expected_name = self._find_zip(game)
        if not zip_path:
            data_dir = self._config.game_data_subdir
            if expected_name:
                msg = (f"ZIP not found: '{expected_name}.zip'\n\n"
                       f"Expected in: {data_dir}/\n"
                       f"(Looked up from game folder name, not XML title '{game.title}')")
            else:
                msg = (f"Cannot determine ZIP filename for '{game.title}'.\n\n"
                       f"No launch script found in the game folder — "
                       f"the folder may be missing or incorrectly named.")
            self.install_finished.emit(game.id, False, msg)
            return False

        # ZIPs contain a <gamedir>/ prefix (e.g. dune2/DUNE2.EXE).
        # Extract to the project's game_data_subdir so data lands at
        # <game_data_subdir>/<gamedir>/ — where the launch scripts expect it.
        dest_dir = self._config.abs_game_data(self.root)
        task = InstallTask(game.id, zip_path, dest_dir, self._install_signals)
        self._pool.start(task)
        return True

    def fetch(self, game) -> bool:
        """
        Acquire a game's ZIP (via local source or torrent) and extract it.

        This is the Lite-mode counterpart to ``install()``: called when the
        game's ZIP is not present on disk.  Returns False if we cannot
        determine the game name (no launch script found).
        """
        gamename = getattr(game, "gamename", "") or self._find_gamedir_and_name(game)[1]
        if not gamename:
            self.fetch_finished.emit(
                game.id, False,
                f"Cannot determine ZIP filename for '{game.title}'.\n\n"
                "No launch script found in the game folder."
            )
            return False

        dest_dir = self._config.abs_game_data(self.root)

        # Resolve effective ZIP source: if the user pointed to a project root
        # (e.g. /Volumes/BigDrive/eXoDOS), automatically descend into the
        # game_data_subdir (e.g. eXo/eXoDOS/) where the ZIPs actually live.
        # If they pointed directly at a flat directory of ZIPs, use it as-is.
        effective_source = self._zip_source_path
        if effective_source:
            subdir = self._config.abs_game_data(effective_source)
            if os.path.isdir(subdir):
                effective_source = subdir

        task = FetchTask(
            game_id         = game.id,
            gamename        = gamename,
            zip_dest_dir    = dest_dir,
            extract_dir     = dest_dir,
            aria_index_path = self._config.aria_index_path(self.root),
            torrent_path    = self._config.torrent_path(self.root),
            zip_source_path = effective_source,
            signals         = self._fetch_signals,
        )
        self._active_fetch_task = task
        self._pool.start(task)
        return True

    def cancel_fetch(self) -> None:
        """Cancel the currently active fetch task, if any."""
        if self._active_fetch_task is not None:
            self._active_fetch_task.cancel()

    # ── private ───────────────────────────────────────────────────────────────

    def _find_gamedir_and_name(self, game) -> tuple[str, str]:
        """
        Return (gamedir_short, gamename_without_ext) or ('', '').

        gamedir  — the short folder name under !dos/ (e.g. 'dune2')
        gamename — the script stem including year (e.g. 'Dune 2 - The Building of a Dynasty (1992)')
        """
        if not game.game_dir:
            return "", ""

        game_folder = os.path.join(self._config.abs_scripts(self.root), game.game_dir)
        if not os.path.isdir(game_folder):
            return "", ""

        year_pattern = re.compile(r"\(\d{4}\)\.(bsh|msh|command|sh)$")
        for fname in sorted(os.listdir(game_folder)):
            if fname.startswith("._"):          # skip macOS AppleDouble metadata
                continue
            if year_pattern.search(fname):
                gamename = os.path.splitext(fname)[0]   # strip extension
                return game.game_dir, gamename

        return "", ""

    def _find_launch_script(self, game) -> tuple[Optional[str], str]:
        """Legacy helper — kept for any direct callers; prefers _find_gamedir_and_name."""
        gamedir, gamename = self._find_gamedir_and_name(game)
        if not gamedir:
            return None, ""
        bsh = os.path.join(self._config.abs_scripts(self.root), gamedir, gamename + ".bsh")
        if os.path.isfile(bsh):
            return bsh, self.root
        msh = os.path.join(self._config.abs_scripts(self.root), gamedir, gamename + ".msh")
        if os.path.isfile(msh):
            return msh, self.root
        cmd = os.path.join(self._config.abs_scripts(self.root), gamedir, gamename + ".command")
        if os.path.isfile(cmd):
            return cmd, self.root
        return None, ""

    def _find_zip(self, game) -> tuple[Optional[str], str]:
        """Return (zip_path, expected_name) where zip_path is None if not found.

        expected_name is the stem (no extension) used to look up the ZIP, e.g.
        'Dune 2 - The Building of a Dynasty (1992)'.  It is '' if we couldn't
        even determine a name (no launch script in game folder).

        The ZIP is named '<Title> (Year).zip' and lives in eXo/eXoDOS/.
        The same '<Title> (Year)' string appears as the .bsh filename in
        !dos/<gamedir>/, so we reuse _find_gamedir_and_name to get it —
        this avoids mismatches between the XML title and the ZIP filename
        (e.g. XML: 'Dune II: The Building of a Dynasty' vs
               ZIP: 'Dune 2 - The Building of a Dynasty (1992).zip').
        """
        gamedir, gamename = self._find_gamedir_and_name(game)
        if not gamename:
            return None, ""
        zip_path = os.path.join(self._config.abs_game_data(self.root), gamename + ".zip")
        return (zip_path if os.path.isfile(zip_path) else None), gamename
