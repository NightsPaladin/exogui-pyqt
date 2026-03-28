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

import errno
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

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
                 signals: LaunchSignals,
                 emulators: dict[str, str] | None = None):
        super().__init__()
        self.game_id = game_id
        self.helper = helper
        self.gamedir = gamedir
        self.gamename = gamename
        self.signals = signals
        self._emulators = emulators or {}
        self.setAutoDelete(True)

    @staticmethod
    def _emu_env_key(name: str) -> str:
        """Normalise a family name to a EXOGUI_EMU_* env-var key.

        E.g. "dosbox-staging" → "EXOGUI_EMU_DOSBOX_STAGING"
        """
        return "EXOGUI_EMU_" + re.sub(r"[^A-Z0-9]", "_", name.upper())

    @staticmethod
    def _cmd_available(cmd: str) -> bool:
        """Return True if the first token of *cmd* resolves to an executable."""
        if not cmd:
            return False
        first = cmd.split()[0]
        if os.path.isabs(first):
            return os.path.isfile(first) and os.access(first, os.X_OK)
        return shutil.which(first) is not None

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

            if sys.platform.startswith("linux"):
                # Ensure PipeWire/PulseAudio session sockets are reachable from
                # the subprocess.  When the GUI is launched via a desktop file or
                # custom launcher, XDG_RUNTIME_DIR may be absent from the inherited
                # environment, which prevents flatpak audio from working.
                if "XDG_RUNTIME_DIR" not in env:
                    try:
                        xdg_dir = f"/run/user/{os.getuid()}"
                        if os.path.isdir(xdg_dir):
                            env["XDG_RUNTIME_DIR"] = xdg_dir
                    except AttributeError:
                        pass

            # Set emulator override env vars so the shell scripts can respect them.
            # Each entry: EXOGUI_EMU_<FAMILY>=<command>
            # Global fallback: EXOGUI_EMU_FALLBACK_CMD = first available command.
            fallback_cmd = ""
            for name, cmd in self._emulators.items():
                if not name or not cmd:
                    continue
                env[self._emu_env_key(name)] = cmd
                if not fallback_cmd and self._cmd_available(cmd):
                    fallback_cmd = cmd
            if fallback_cmd:
                env["EXOGUI_EMU_FALLBACK_CMD"] = fallback_cmd

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


class UninstallTask(QRunnable):
    def __init__(self, game_id: str, game_dir: str, signals: InstallSignals):
        super().__init__()
        self.game_id  = game_id
        self.game_dir = game_dir
        self.signals  = signals
        self.setAutoDelete(True)

    @pyqtSlot()
    def run(self):
        try:
            # On macOS, HFS+/APFS directories can contain synthetic ._* (AppleDouble)
            # resource-fork entries that appear in os.listdir() but cannot be unlinked
            # directly — they vanish automatically when their real sibling is removed.
            # Collect non-ENOENT errors; skip ENOENT so the walk continues.
            first_error: list[Exception] = []

            def _onerror(func, path, exc_info):
                err = exc_info[1]
                if isinstance(err, OSError) and err.errno == errno.ENOENT:
                    return  # synthetic ._* entry — ignore
                if not first_error:
                    first_error.append(err)

            shutil.rmtree(self.game_dir, onerror=_onerror)

            if os.path.isdir(self.game_dir):
                raise first_error[0] if first_error else OSError(
                    f"Directory still exists after removal: {self.game_dir}"
                )

            self.signals.finished.emit(self.game_id, True, "")
        except Exception as exc:
            self.signals.finished.emit(
                self.game_id, False,
                f"Failed to remove '{os.path.basename(self.game_dir)}':\n{exc}"
            )


# ── aria2c progress parsing ───────────────────────────────────────────────────

_ARIA_PCT_RE = re.compile(r"\((\d{1,3})%\)")


def _parse_aria_progress(line: str) -> int | None:
    """Extract completion percentage from an aria2c status line, or None."""
    m = _ARIA_PCT_RE.search(line)
    return int(m.group(1)) if m else None


# ── fetch task (acquire ZIP via local copy or torrent, then extract) ──────────

class FetchTask(QRunnable):
    """
    Download (or copy) a game ZIP then extract it.  Optionally also fetches
    the GameData extras ZIP (videos, music, manuals) if available.

    Acquisition priority for each ZIP:
      1. Local/network source path — copy the file if found there.
      2. Torrent fallback — use aria2c to download the specific file from the
         full project torrent (requires ``index.txt`` and the ``.torrent`` file).

    GameData acquisition is non-fatal: if it fails the game is still installed.

    Emits FetchSignals throughout all phases.
    """

    def __init__(
        self,
        game_id:              str,
        gamename:             str,   # e.g. "Dune 2 - The Building of a Dynasty (1992)"
        zip_dest_dir:         str,   # where to place the game ZIP
        extract_dir:          str,   # where to extract the game ZIP
        aria_index_path:      str,
        torrent_path:         str,
        zip_source_path:      str,   # local/network directory for game ZIPs (may be "")
        signals:              FetchSignals,
        # Optional GameData extras support:
        gamedata_zip_dest_dir: str = "",  # where to place the GameData ZIP
        gamedata_extract_dir:  str = "",  # collection root — GameData paths are relative to it
        gamedata_source_path:  str = "",  # local/network directory for GameData ZIPs
    ):
        super().__init__()
        self.game_id               = game_id
        self.gamename              = gamename
        self.zip_dest_dir          = zip_dest_dir
        self.extract_dir           = extract_dir
        self.aria_index_path       = aria_index_path
        self.torrent_path          = torrent_path
        self.zip_source_path       = zip_source_path
        self.signals               = signals
        self.gamedata_zip_dest_dir = gamedata_zip_dest_dir
        self.gamedata_extract_dir  = gamedata_extract_dir
        self.gamedata_source_path  = gamedata_source_path
        self._cancel               = threading.Event()
        self._proc: subprocess.Popen | None = None
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
        zip_filename = self.gamename + ".zip"
        final_zip    = os.path.join(self.zip_dest_dir, zip_filename)

        # Phase 1: acquire game ZIP ───────────────────────────────────────────
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

        # Phase 2: extract game ZIP ───────────────────────────────────────────
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

        # Phases 3 & 4: acquire + extract GameData extras ZIP (non-fatal) ─────
        # GameData ZIPs contain per-game videos, music, manuals, and in-game
        # extras.  They extract to the collection root because their internal
        # paths are collection-relative (e.g. "Videos/MS-DOS/…", "Music/MS-DOS/…").
        if self.gamedata_zip_dest_dir and self.gamedata_extract_dir:
            self._fetch_gamedata(zip_filename)

        self.signals.finished.emit(self.game_id, True, "")

    # ── acquisition helpers ───────────────────────────────────────────────────

    def _acquire(self, zip_filename: str, final_zip: str) -> bool:
        """
        Get the main game ZIP into *final_zip*.  Returns True on success.

        Tries the local/network source first; falls back to torrent.
        """
        os.makedirs(self.zip_dest_dir, exist_ok=True)

        if self.zip_source_path:
            source_zip = os.path.join(self.zip_source_path, zip_filename)
            if os.path.isfile(source_zip):
                return self._copy_from_source(source_zip, final_zip)
            # Not found at source — fall through silently to torrent

        return self._download_torrent(zip_filename, final_zip)

    def _acquire_gamedata(self, zip_filename: str, final_zip: str) -> bool:
        """
        Get the GameData extras ZIP into *final_zip*.  Returns True on success.

        Tries the local/network source first; falls back to torrent.
        """
        os.makedirs(self.gamedata_zip_dest_dir, exist_ok=True)

        if self.gamedata_source_path:
            source_zip = os.path.join(self.gamedata_source_path, zip_filename)
            if os.path.isfile(source_zip):
                return self._copy_from_source(source_zip, final_zip)

        return self._download_torrent_media(zip_filename, final_zip)

    def _fetch_gamedata(self, zip_filename: str) -> None:
        """
        Acquire and extract the GameData extras ZIP.  Non-fatal: any failure
        is silently ignored because the game itself is still playable.
        """
        final_zip = os.path.join(self.gamedata_zip_dest_dir, zip_filename)

        if not os.path.isfile(final_zip):
            try:
                acquired = self._acquire_gamedata(zip_filename, final_zip)
            except Exception:
                return
            if not acquired or self._cancel.is_set():
                return

        # GameData ZIPs use collection-root-relative paths (e.g. "Videos/MS-DOS/…"),
        # so we extract to the collection root, not the game data subdirectory.
        self.signals.phase_changed.emit(self.game_id, "Extracting extras…")
        try:
            with zipfile.ZipFile(final_zip, "r") as zf:
                for member in zf.namelist():
                    if self._cancel.is_set():
                        return
                    zf.extract(member, self.gamedata_extract_dir)
        except Exception:
            pass  # non-fatal

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
            # Write to a temp file first, then rename atomically on success.
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
            try:
                os.unlink(final_zip + ".tmp")
            except OSError:
                pass
            raise RuntimeError(f"Failed to copy ZIP from source:\n{exc}") from exc

    def _download_torrent(self, zip_filename: str, final_zip: str) -> bool:
        """Download the main game ZIP from the project torrent using aria2c."""
        index = _aria_index.load_index(self.aria_index_path)
        entry = index.get(self.gamename)
        if not entry:
            return False
        self.signals.phase_changed.emit(self.game_id, "Downloading via torrent…")
        self.signals.progress.emit(self.game_id, 0, 100)
        return self._run_torrent_download(entry.game_index, zip_filename, final_zip)

    def _download_torrent_media(self, zip_filename: str, final_zip: str) -> bool:
        """Download the GameData extras ZIP from the project torrent using aria2c."""
        index = _aria_index.load_index(self.aria_index_path)
        entry = index.get(self.gamename)
        if not entry or not entry.media_index:
            return False
        self.signals.phase_changed.emit(self.game_id, "Downloading extras via torrent…")
        self.signals.progress.emit(self.game_id, 0, 100)
        return self._run_torrent_download(entry.media_index, zip_filename, final_zip)

    def _run_torrent_download(
        self, file_index: int, zip_filename: str, final_zip: str
    ) -> bool:
        """
        Run aria2c to selectively download one file from the project torrent.

        *file_index* is the 1-based index from index.txt.  The downloaded file
        is moved to *final_zip* on success.  Returns True on success.
        """
        if not self.torrent_path or not os.path.isfile(self.torrent_path):
            return False
        if self._cancel.is_set():
            return False

        aria2c_cmd = _aria_index.find_aria2c()
        if not aria2c_cmd:
            return False

        # Run aria2c from a temp directory so partial downloads don't litter
        # the project root and are cleaned up automatically on failure.
        with tempfile.TemporaryDirectory(prefix="exogui_dl_") as tmpdir:
            argv = _aria_index.build_aria2c_command(
                aria2c_cmd,
                self.torrent_path,
                [(file_index, zip_filename)],
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

            # Locate the downloaded file — aria2c may create subdirectories
            # even when --index-out is specified if the torrent path includes them.
            downloaded = None
            for dirpath, _dirs, files in os.walk(tmpdir):
                for fname in files:
                    if fname == zip_filename:
                        downloaded = os.path.join(dirpath, fname)
                        break

            if not downloaded or not os.path.isfile(downloaded):
                return False
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
    launch_started     = pyqtSignal(str)
    launch_finished    = pyqtSignal(str, int)
    launch_error       = pyqtSignal(str, str)
    install_progress   = pyqtSignal(str, int, int)
    install_finished   = pyqtSignal(str, bool, str)
    uninstall_finished = pyqtSignal(str, bool, str)  # game_id, success, message
    fetch_phase        = pyqtSignal(str, str)        # game_id, phase label
    fetch_progress     = pyqtSignal(str, int, int)   # game_id, current, total
    fetch_finished     = pyqtSignal(str, bool, str)  # game_id, success, message
    fetch_cancelled    = pyqtSignal(str)             # game_id

    def __init__(self, root: str, config: ProjectConfig | None = None,
                 zip_source_path: str = "",
                 emulators: dict[str, str] | None = None,
                 parent=None):
        super().__init__(parent)
        self.root             = root
        self._config          = config if config is not None else EXODOS
        self._zip_source_path = zip_source_path
        self._emulators       = emulators or {}
        self._pool            = QThreadPool.globalInstance()
        self._active_fetch_task: FetchTask | None = None

        self._launch_signals = LaunchSignals()
        self._launch_signals.started.connect(self.launch_started)
        self._launch_signals.finished.connect(self.launch_finished)
        self._launch_signals.error.connect(self.launch_error)

        self._install_signals = InstallSignals()
        self._install_signals.progress.connect(self.install_progress)
        self._install_signals.finished.connect(self.install_finished)

        self._uninstall_signals = InstallSignals()
        self._uninstall_signals.finished.connect(self.uninstall_finished)

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

        task = LaunchTask(game.id, helper, gamedir, gamename, self._launch_signals,
                          emulators=self._emulators)
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

    def uninstall(self, game) -> bool:
        """
        Remove the game's extracted directory from the project's game data directory.

        The removed path is <game_data_subdir>/<game.game_dir>, e.g.
        eXo/eXoDOS/<game_dir> or eXo/eXoWin3x/<game_dir>.
        Returns False if the directory does not exist.
        """
        game_dir = os.path.join(self._config.abs_game_data(self.root), game.game_dir)
        if not os.path.isdir(game_dir):
            self.uninstall_finished.emit(
                game.id, False,
                f"Game directory not found:\n{game_dir}"
            )
            return False
        task = UninstallTask(game.id, game_dir, self._uninstall_signals)
        self._pool.start(task)
        return True

    def fetch(self, game) -> bool:
        """
        Acquire a game's ZIP (via local source or torrent) and extract it.

        Also fetches the GameData extras ZIP when available (contains per-game
        videos, music, manuals, and in-game extras).  GameData acquisition is
        non-fatal — if it fails the game is still installed and playable.

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

        # Resolve GameData extras ZIP paths.
        # The destination is always within the current collection root.
        # The source is derived from the user's ZIP source path in the same way.
        gamedata_zip_dest = self._config.abs_gamedata_zip_base(self.root)
        gamedata_extract  = self.root   # GameData paths are collection-root-relative
        gamedata_source   = ""
        if self._zip_source_path and gamedata_zip_dest:
            gd_source_dir = self._config.abs_gamedata_zip_base(self._zip_source_path)
            if gd_source_dir and os.path.isdir(gd_source_dir):
                gamedata_source = gd_source_dir

        task = FetchTask(
            game_id               = game.id,
            gamename              = gamename,
            zip_dest_dir          = dest_dir,
            extract_dir           = dest_dir,
            aria_index_path       = self._config.aria_index_path(self.root),
            torrent_path          = self._config.torrent_path(self.root),
            zip_source_path       = effective_source,
            signals               = self._fetch_signals,
            gamedata_zip_dest_dir = gamedata_zip_dest,
            gamedata_extract_dir  = gamedata_extract,
            gamedata_source_path  = gamedata_source,
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

    def _find_launch_script(self, game) -> tuple[str | None, str]:
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

    def _find_zip(self, game) -> tuple[str | None, str]:
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
