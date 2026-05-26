"""
launcher.py — Launch and install eXo collection games on macOS and Linux.

For supported collection types (eXoDOS, eXoWin3x, eXoScummVM, eXoAppleIIGS)
games are launched via core.native_launch, which builds the emulator argv
directly in Python without invoking any shell scripts.

Other collection types fall back to eXo/util/launch_helper.sh.

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
from typing import Optional

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot

from core.project import ProjectConfig, EXODOS
from core import aria_index as _aria_index
import core.native_launch as _native_launch
from core.torrent import parse as _parse_torrent
from core.debug import dbg


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


class LiteDownloadSignals(QObject):
    """Signals for LiteDownloadTask (download + install the Lite metadata set)."""
    phase     = pyqtSignal(str)         # phase description
    progress  = pyqtSignal(int, int)    # current, total (% or file count)
    finished  = pyqtSignal(bool, str)   # success, message
    cancelled = pyqtSignal()


# ── runnable tasks ───────────────────────────────────────────────────────────

class NativeLaunchTask(QRunnable):
    """
    Launch a game via a directly-built argv.

    Bypasses all shell scripts — the argv is constructed by core.native_launch
    from the collection configuration and game metadata.
    """

    def __init__(self, game_id: str, argv: list[str], cwd: str,
                 signals: LaunchSignals,
                 emulators: dict[str, str] | None = None):
        super().__init__()
        self.game_id   = game_id
        self.argv      = argv
        self.cwd       = cwd
        self.signals   = signals
        self._emulators = emulators or {}
        self.setAutoDelete(True)

    @pyqtSlot()
    def run(self):
        self.signals.started.emit(self.game_id)
        try:
            env = os.environ.copy()
            if sys.platform == "darwin":
                for _prefix in ("/opt/homebrew", "/usr/local"):
                    for _sub in ("bin", "sbin"):
                        _p = f"{_prefix}/{_sub}"
                        if os.path.isdir(_p) and _p not in env.get("PATH", ""):
                            env["PATH"] = _p + ":" + env["PATH"]
            if sys.platform.startswith("linux"):
                if "XDG_RUNTIME_DIR" not in env:
                    try:
                        xdg_dir = f"/run/user/{os.getuid()}"
                        if os.path.isdir(xdg_dir):
                            env["XDG_RUNTIME_DIR"] = xdg_dir
                    except AttributeError:
                        pass

            from core.mac_shortcuts import disable_conflicting_shortcuts, restore_shortcuts
            _saved = disable_conflicting_shortcuts()
            try:
                result = subprocess.run(self.argv, env=env, cwd=self.cwd)
            finally:
                restore_shortcuts(_saved)
            self.signals.finished.emit(self.game_id, result.returncode)
        except Exception as exc:
            self.signals.error.emit(self.game_id, str(exc))


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
                 signals: InstallSignals, extra_zip_path: str = "",
                 extra_extract_dir: str = ""):
        super().__init__()
        self.game_id = game_id
        self.zip_path = zip_path
        self.dest_dir = dest_dir
        self.signals = signals
        self.extra_zip_path = extra_zip_path
        self.extra_extract_dir = extra_extract_dir
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
        except Exception as exc:
            zip_name = os.path.basename(self.zip_path)
            self.signals.finished.emit(
                self.game_id, False,
                f"Failed to extract '{zip_name}':\n{exc}"
            )
            return

        if self.extra_zip_path and os.path.isfile(self.extra_zip_path):
            extra_dest = self.extra_extract_dir or self.dest_dir
            try:
                with zipfile.ZipFile(self.extra_zip_path, "r") as zf:
                    for member in zf.namelist():
                        zf.extract(member, extra_dest)
            except Exception:
                pass  # GameData extras are optional

        self.signals.finished.emit(self.game_id, True, "")


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

            def _onerror(_func, _path, exc_info):
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
        game_id:          str,
        gamename:         str,          # e.g. "Dune 2 - The Building of a Dynasty (1992)"
        zip_dest_dir:     str,          # where to place the downloaded ZIP
        extract_dir:      str,          # where to extract (usually same as zip_dest_dir)
        aria_index_path:  str,
        torrent_path:     str,
        zip_source_path:  str,          # user-configured local/network path (may be "")
        signals:              FetchSignals,
        gamedata_zip_dir:     str = "",  # Content/GameData/<project>/ on disk (may be "")
        gamedata_extract_dir: str = "",  # collection root for GameData extraction (may be "")
        gamedata_source_path: str = "",  # local/network GameData ZIP directory (may be "")
    ):
        super().__init__()
        self.game_id          = game_id
        self.gamename         = gamename
        self.zip_dest_dir     = zip_dest_dir
        self.extract_dir      = extract_dir
        self.aria_index_path  = aria_index_path
        self.torrent_path     = torrent_path
        self.zip_source_path  = zip_source_path
        self.signals          = signals
        self.gamedata_zip_dir     = gamedata_zip_dir
        self.gamedata_extract_dir = gamedata_extract_dir
        self.gamedata_source_path = gamedata_source_path
        self._cancel              = threading.Event()
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
        gamedata_zip  = (
            os.path.join(self.gamedata_zip_dir, zip_filename)
            if self.gamedata_zip_dir else ""
        )

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

        # Phase 3 & 4: GameData extras (best-effort, eXoDOS only) ────────────
        if gamedata_zip and not self._cancel.is_set():
            if not os.path.isfile(gamedata_zip):
                try:
                    self._acquire_media(zip_filename, gamedata_zip)
                except Exception:
                    pass
            if not self._cancel.is_set() and os.path.isfile(gamedata_zip):
                extras_dest = self.gamedata_extract_dir or self.extract_dir
                self.signals.phase_changed.emit(self.game_id, "Extracting extras…")
                try:
                    with zipfile.ZipFile(gamedata_zip, "r") as zf:
                        members = zf.namelist()
                        total = len(members)
                        for i, member in enumerate(members):
                            if self._cancel.is_set():
                                break
                            zf.extract(member, extras_dest)
                            self.signals.progress.emit(self.game_id, i + 1, total)
                except Exception:
                    pass  # extras are optional

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
            dbg(f"FetchTask: checking zip source: {source_zip}")
            if os.path.isfile(source_zip):
                dbg("FetchTask: found at zip source — copying")
                return self._copy_from_source(source_zip, final_zip)
            dbg("FetchTask: not at zip source — falling back to torrent")

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

        # Resolve file index: prefer index.txt (fast lookup); fall back to
        # parsing the torrent directly for Lite installs that have no index.txt.
        file_index: Optional[int] = None
        dbg(f"FetchTask: looking up '{self.gamename}' in index: {self.aria_index_path}")
        index = _aria_index.load_index(self.aria_index_path)
        entry = index.get(self.gamename)
        if entry:
            file_index = entry.game_index
            dbg(f"FetchTask: found in index.txt at file_index={file_index}")
        else:
            dbg(f"FetchTask: not in index.txt — parsing torrent: {self.torrent_path}")
            try:
                torrent_info = _parse_torrent(self.torrent_path)
                indices = torrent_info.select_game(zip_filename)
                if indices:
                    file_index = indices[0]
                    dbg(f"FetchTask: found in torrent at file_index={file_index} (of {len(torrent_info.files)} files)")
                else:
                    dbg(f"FetchTask: '{zip_filename}' not found in torrent")
            except Exception as exc:
                dbg(f"FetchTask: torrent parse failed: {exc}")

        if file_index is None:
            dbg("FetchTask: cannot resolve file index — aborting torrent download")
            return False

        self.signals.phase_changed.emit(self.game_id, "Downloading via torrent…")
        self.signals.progress.emit(self.game_id, 0, 100)

        # aria2c must run from a temp working directory so it doesn't litter
        # the project root with partial files.
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

            # Find the downloaded file (aria2c may create subdirs)
            downloaded = None
            for dirpath, _, files in os.walk(tmpdir):
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

    def _acquire_media(self, zip_filename: str, gamedata_zip: str) -> bool:
        """Copy or download the GameData extras ZIP. Best-effort."""
        if self._cancel.is_set():
            return False

        # 1. Local/network source
        if self.gamedata_source_path:
            source = os.path.join(self.gamedata_source_path, zip_filename)
            if os.path.isfile(source):
                self.signals.phase_changed.emit(self.game_id, "Copying extras…")
                try:
                    os.makedirs(os.path.dirname(gamedata_zip), exist_ok=True)
                    shutil.copy2(source, gamedata_zip)
                    return True
                except OSError:
                    pass  # fall through to torrent

        # 2. Torrent fallback
        if not self.torrent_path or not os.path.isfile(self.torrent_path):
            return False
        if self._cancel.is_set():
            return False

        aria2c_cmd = _aria_index.find_aria2c()
        if not aria2c_cmd:
            return False

        index = _aria_index.load_index(self.aria_index_path)
        entry = index.get(self.gamename)
        if not entry or not entry.media_index:
            return False

        self.signals.phase_changed.emit(self.game_id, "Downloading extras via torrent…")
        self.signals.progress.emit(self.game_id, 0, 100)

        os.makedirs(os.path.dirname(gamedata_zip), exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="exogui_dl_") as tmpdir:
            argv = _aria_index.build_aria2c_command(
                aria2c_cmd,
                self.torrent_path,
                [(entry.media_index, zip_filename)],
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

            downloaded = None
            for dirpath, _, fnames in os.walk(tmpdir):
                for fname in fnames:
                    if fname == zip_filename:
                        downloaded = os.path.join(dirpath, fname)
                        break

            if not downloaded or not os.path.isfile(downloaded):
                return False
            if os.path.getsize(downloaded) == 0:
                return False

            shutil.move(downloaded, gamedata_zip)
            return True


class SetupTask(QRunnable):
    """
    Extract already-downloaded Content/*.zip files into a collection root.

    This is the programmatic equivalent of the Windows setup .bat script that
    ships with every eXo collection.  It handles the case where the user has
    a full torrent download on disk but the metadata ZIPs haven't been
    extracted yet (Content/XO*Metadata.zip, Content/!*metadata.zip, etc.).

    Uses the same signals class as LiteDownloadTask for UI reuse.
    """

    def __init__(self, collection_root: str, signals: LiteDownloadSignals):
        super().__init__()
        self.collection_root = collection_root
        self.signals = signals
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @pyqtSlot()
    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            self.signals.finished.emit(False, str(exc))

    def _run(self) -> None:
        content_dir = os.path.join(self.collection_root, "Content")
        util_dir    = os.path.join(self.collection_root, "eXo", "util")
        os.makedirs(util_dir, exist_ok=True)
        skipped: list[str] = []

        try:
            zips = sorted(os.listdir(content_dir))
        except OSError:
            self.signals.finished.emit(False, f"Content directory not found: {content_dir}")
            return

        # Content/*.zip: extract to collection root
        for fname in zips:
            if self._cancel.is_set():
                self.signals.cancelled.emit()
                return
            if not fname.endswith(".zip"):
                continue
            # Only extract metadata zips; skip LaunchBox.zip and media packs
            if fname == "LaunchBox.zip":
                continue
            src = os.path.join(content_dir, fname)
            if not zipfile.is_zipfile(src):
                dbg(f"SetupTask: skipping {fname} (not a valid ZIP)")
                skipped.append(fname)
                continue
            self.signals.phase.emit(f"Extracting {fname}…")
            dbg(f"SetupTask: extracting {fname} → {self.collection_root}")
            self._extract_zip(src, self.collection_root)

        # Pass 1 — eXo/util/util*.zip and similar: extract to eXo/util/.
        # OPT*.zip are Windows-only tools; skip entirely.
        # EXT*.zip are deferred to pass 2 because they may not exist yet
        # (they are nested inside util*.zip and appear only after extraction).
        eXo_dir = os.path.join(self.collection_root, "eXo")
        if not self._cancel.is_set() and os.path.isdir(util_dir):
            for fname in sorted(os.listdir(util_dir)):
                if self._cancel.is_set():
                    self.signals.cancelled.emit()
                    return
                if not fname.endswith(".zip"):
                    continue
                fupper = fname.upper()
                if fupper.startswith("OPT") or fupper.startswith("EXT"):
                    dbg(f"SetupTask: deferring/skipping {fname}")
                    continue
                src = os.path.join(util_dir, fname)
                if not zipfile.is_zipfile(src):
                    dbg(f"SetupTask: skipping {fname} (not a valid ZIP)")
                    skipped.append(fname)
                    continue
                self.signals.phase.emit(f"Extracting {fname}…")
                dbg(f"SetupTask: extracting {fname} → {util_dir}")
                self._extract_zip(src, util_dir)

        # Pass 2 — EXT*.zip: extract emulator assets (ROMs, fonts, …) to eXo/.
        # These are unpacked from util*.zip in pass 1, so the directory is
        # re-scanned here to pick them up.
        if not self._cancel.is_set() and os.path.isdir(util_dir):
            for fname in sorted(os.listdir(util_dir)):
                if self._cancel.is_set():
                    self.signals.cancelled.emit()
                    return
                if not (fname.upper().startswith("EXT") and fname.endswith(".zip")):
                    continue
                src = os.path.join(util_dir, fname)
                if not zipfile.is_zipfile(src):
                    dbg(f"SetupTask: skipping {fname} (not a valid ZIP)")
                    skipped.append(fname)
                    continue
                self.signals.phase.emit(f"Extracting {fname}…")
                dbg(f"SetupTask: extracting {fname} → {eXo_dir}")
                self._extract_zip(src, eXo_dir)

        if not self._cancel.is_set():
            if skipped:
                msg = (
                    "Setup complete, but the following files could not be extracted\n"
                    "(they may be incomplete downloads):\n\n"
                    + "\n".join(f"  • {f}" for f in skipped)
                )
                self.signals.finished.emit(True, msg)
            else:
                self.signals.finished.emit(True, "Setup complete.")

    def _extract_zip(self, zip_path: str, dest_dir: str) -> None:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = zf.namelist()
            total = len(members)
            for i, member in enumerate(members):
                if self._cancel.is_set():
                    return
                zf.extract(member, dest_dir)
                self.signals.progress.emit(i + 1, total)


class LiteDownloadTask(QRunnable):
    """
    Download the Lite metadata subset of a project torrent then install it.

    Steps
    -----
    1. Parse the torrent → determine which files belong to the Lite set.
    2. Run aria2c ``--select-file=<indices>`` to fetch them into a temp dir.
    3. Extract ``Content/XO*Metadata.zip``  → collection root  (images + XML).
    4. Extract ``Content/!*metadata.zip``   → collection root  (launch scripts).
    5. Extract ``eXo/util/util*.zip``       → ``<root>/eXo/util/``  (emulator configs).
    6. Copy    ``eXo/util/unzip.exe``       → ``<root>/eXo/util/``  (Windows helper).
    """

    def __init__(
        self,
        torrent_path:    str,
        collection_root: str,
        signals:         LiteDownloadSignals,
    ):
        super().__init__()
        self.torrent_path    = torrent_path
        self.collection_root = collection_root
        self.signals         = signals
        self._cancel         = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self.setAutoDelete(True)

    def cancel(self) -> None:
        self._cancel.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    @pyqtSlot()
    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            self.signals.finished.emit(False, str(exc))

    # ── internal ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        # 1. Parse torrent
        self.signals.phase.emit("Parsing torrent…")
        dbg(f"LiteDownload: parsing torrent: {self.torrent_path}")
        info = _parse_torrent(self.torrent_path)
        lite_indices = info.select_lite()
        dbg(f"LiteDownload: torrent name={info.name!r}, {len(info.files)} files total, {len(lite_indices)} Lite")
        for f in info.files:
            if f.index in set(lite_indices):
                dbg(f"LiteDownload:   Lite file [{f.index}] {f.path}  ({f.size:,} bytes)")
        if not lite_indices:
            self.signals.finished.emit(False, "No Lite files found in torrent.")
            return

        # 2. Find aria2c
        aria2c_cmd = _aria_index.find_aria2c()
        if not aria2c_cmd:
            self.signals.finished.emit(
                False,
                "aria2c not found.\n\n"
                "Install it with:\n"
                "  macOS:  brew install aria2\n"
                "  Linux:  sudo apt install aria2  (or equivalent)",
            )
            return

        if self._cancel.is_set():
            self.signals.cancelled.emit()
            return

        # 3. Download directly into the collection's parent directory so that
        # aria2c places files at their final torrent paths
        # (<parent>/<torrent_name>/Content/… and <parent>/<torrent_name>/eXo/util/…).
        # This means pre-copied or previously downloaded zips are found by aria2c's
        # piece-hash verification and skipped, and the zips are preserved in place
        # after install rather than being discarded.
        dl_dir = os.path.dirname(os.path.normpath(self.collection_root))

        self.signals.phase.emit("Downloading Lite files…")
        self.signals.progress.emit(0, 100)

        argv = self._build_argv(aria2c_cmd, lite_indices, dl_dir)
        env  = self._make_env()

        try:
            proc = subprocess.Popen(
                argv,
                cwd=dl_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
            self._proc = proc
            assert proc.stdout is not None
            for line in proc.stdout:
                if self._cancel.is_set():
                    proc.terminate()
                    break
                pct = _parse_aria_progress(line)
                if pct is not None:
                    self.signals.progress.emit(pct, 100)
            proc.wait()
        except (OSError, subprocess.SubprocessError) as exc:
            self.signals.finished.emit(False, f"aria2c error: {exc}")
            return
        finally:
            self._proc = None

        if self._cancel.is_set():
            self.signals.cancelled.emit()
            return

        if proc.returncode != 0:
            self.signals.finished.emit(
                False,
                f"aria2c exited with code {proc.returncode}.\n"
                "Check the torrent file is valid and you have internet access.",
            )
            return

        # torrent_root resolves to collection_root since dl_dir is its parent.
        torrent_root = os.path.join(dl_dir, info.name)
        lite_paths = {f.path for f in info.files if f.index in set(lite_indices)}
        dbg(f"LiteDownload: installing from torrent_root={torrent_root}")
        dbg(f"LiteDownload: collection_root={self.collection_root}")
        self._install(torrent_root, lite_paths)

        if not self._cancel.is_set():
            self._index_collection()

    def _index_collection(self) -> None:
        """Copy the torrent into eXo/util/aria/ and generate index.txt."""
        aria_dir = os.path.join(self.collection_root, "eXo", "util", "aria")
        os.makedirs(aria_dir, exist_ok=True)

        torrent_fname = os.path.basename(self.torrent_path)
        dest_torrent = os.path.join(aria_dir, torrent_fname)

        if not (os.path.exists(dest_torrent) and os.path.samefile(self.torrent_path, dest_torrent)):
            self.signals.phase.emit("Copying torrent…")
            dbg(f"LiteDownload: copying torrent → {dest_torrent}")
            shutil.copy2(self.torrent_path, dest_torrent)

        self.signals.phase.emit("Indexing torrent…")
        index_path = os.path.join(aria_dir, "index.txt")
        try:
            count = _aria_index.build_index(dest_torrent, index_path)
            dbg(f"LiteDownload: indexed {count:,} files → {index_path}")
        except Exception as exc:
            dbg(f"LiteDownload: index generation failed: {exc}")

    def _build_argv(
        self, aria2c_cmd: str, indices: list[int], out_dir: str
    ) -> list[str]:
        argv = aria2c_cmd.split()
        argv += [
            f"--select-file={','.join(str(i) for i in indices)}",
            "--file-allocation=none",
            "--allow-overwrite=true",
            "--seed-time=0",
            f"--dir={out_dir}",
            self.torrent_path,
        ]
        return argv

    def _make_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if sys.platform == "darwin":
            for prefix in ("/opt/homebrew", "/usr/local"):
                for sub in ("bin", "sbin"):
                    p = f"{prefix}/{sub}"
                    if os.path.isdir(p) and p not in env.get("PATH", ""):
                        env["PATH"] = p + ":" + env["PATH"]
        return env

    def _install(self, torrent_root: str, lite_paths: set[str]) -> None:
        """Extract downloaded zips into the collection root.

        The zips remain at their torrent paths (Content/ and eXo/util/) after
        extraction so that aria2c can verify and seed them on subsequent runs.
        """
        dest_util = os.path.join(self.collection_root, "eXo", "util")
        os.makedirs(dest_util, exist_ok=True)

        for rel_path in sorted(lite_paths):
            if self._cancel.is_set():
                self.signals.cancelled.emit()
                return

            src = os.path.join(torrent_root, *rel_path.split("/"))
            dbg(f"LiteDownload: processing {rel_path}")
            dbg(f"LiteDownload:   src exists={os.path.isfile(src)}: {src}")
            if not os.path.isfile(src):
                continue

            parts = rel_path.split("/")

            if parts[0] == "Content" and len(parts) == 2:
                fname = parts[1]
                if fname.endswith(".zip"):
                    if not zipfile.is_zipfile(src):
                        dbg(f"LiteDownload:   skipping {fname} (not a valid ZIP)")
                        continue
                    self.signals.phase.emit(f"Extracting {fname}…")
                    dbg(f"LiteDownload:   extracting {fname} → {self.collection_root}")
                    self._extract_zip(src, self.collection_root)

            elif parts[0] == "eXo" and len(parts) >= 3 and parts[1] == "util":
                fname = parts[-1]
                if fname.endswith(".zip"):
                    if not zipfile.is_zipfile(src):
                        dbg(f"LiteDownload:   skipping {fname} (not a valid ZIP)")
                        continue
                    self.signals.phase.emit(f"Extracting {fname}…")
                    dbg(f"LiteDownload:   extracting {fname} → {dest_util}")
                    self._extract_zip(src, dest_util)
                else:
                    dest = os.path.join(dest_util, fname)
                    if not (os.path.exists(dest) and os.path.samefile(src, dest)):
                        dbg(f"LiteDownload:   copying {fname} → {dest_util}")
                        shutil.copy2(src, dest)

        # Second pass — EXT*.zip: emulator assets (ROMs, fonts, …) nested
        # inside util*.zip are only present in dest_util after the loop above.
        # Extract them to eXo/ so paths like emulators/GSPlus/ROM1 resolve
        # relative to the collection's eXo/ directory.
        dest_eXo = os.path.join(self.collection_root, "eXo")
        if not self._cancel.is_set() and os.path.isdir(dest_util):
            for fname in sorted(os.listdir(dest_util)):
                if self._cancel.is_set():
                    self.signals.cancelled.emit()
                    return
                if not (fname.upper().startswith("EXT") and fname.endswith(".zip")):
                    continue
                src = os.path.join(dest_util, fname)
                if not zipfile.is_zipfile(src):
                    continue
                self.signals.phase.emit(f"Extracting {fname}…")
                dbg(f"LiteDownload:   extracting {fname} → {dest_eXo}")
                self._extract_zip(src, dest_eXo)

        if not self._cancel.is_set():
            self.signals.finished.emit(True, "Lite download complete.")

    def _extract_zip(self, zip_path: str, dest_dir: str) -> None:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = zf.namelist()
            total = len(members)
            for i, member in enumerate(members):
                if self._cancel.is_set():
                    return
                zf.extract(member, dest_dir)
                self.signals.progress.emit(i + 1, total)


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
    lite_phase         = pyqtSignal(str)             # phase description
    lite_progress      = pyqtSignal(int, int)        # current, total
    lite_finished      = pyqtSignal(bool, str)       # success, message
    lite_cancelled     = pyqtSignal()

    def __init__(self, root: str, config: Optional[ProjectConfig] = None,
                 zip_source_path: str = "",
                 torrent_path: str = "",
                 emulators: dict[str, str] | None = None,
                 parent=None):
        super().__init__(parent)
        self.root             = root
        self._config          = config if config is not None else EXODOS
        self._zip_source_path = zip_source_path
        self._torrent_path    = torrent_path
        self._emulators       = emulators or {}
        pool = QThreadPool.globalInstance()
        assert pool is not None
        self._pool: QThreadPool = pool
        self._active_fetch_task: Optional[FetchTask] = None

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

        self._lite_signals = LiteDownloadSignals()
        self._lite_signals.phase.connect(self.lite_phase)
        self._lite_signals.progress.connect(self.lite_progress)
        self._lite_signals.finished.connect(self.lite_finished)
        self._lite_signals.finished.connect(self._clear_lite_task)
        self._lite_signals.cancelled.connect(self.lite_cancelled)
        self._lite_signals.cancelled.connect(self._clear_lite_task)
        self._active_lite_task: Optional[LiteDownloadTask | SetupTask] = None

    def _clear_fetch_task(self, *_) -> None:
        self._active_fetch_task = None

    def _clear_lite_task(self, *_) -> None:
        self._active_lite_task = None

    # ── public ────────────────────────────────────────────────────────────────

    def launch(self, game, variant: str = "", launch_variant=None) -> bool:
        """
        Launch *game* on macOS / Linux.

        *launch_variant* (a ``LaunchVariant`` from game_library) is the choice
        made by the user in the pre-launch dialog.  When provided it overrides
        the default conf or engine selection.

        *variant* is the platform subdirectory name for multi-platform ScummVM
        games (e.g. "Blue's ABC Time (Windows)"); pass "" to auto-detect.

        For supported collection types (eXoDOS, eXoWin3x, eXoScummVM,
        eXoAppleIIGS) the emulator argv is built directly in Python via
        core.native_launch, bypassing all shell scripts.  Other collection
        types fall back to launch_helper.sh.

        Returns False if the launch cannot be started.
        """
        if _native_launch.is_supported(self._config):
            argv, err = _native_launch.build_argv(
                self._config, self.root, game, self._emulators, variant,
                launch_variant=launch_variant,
            )
            if argv is None:
                self.launch_error.emit(game.id, err or f"Cannot launch '{game.title}'.")
                return False
            cwd = _native_launch.get_cwd(self._config, self.root)
            task = NativeLaunchTask(
                game.id, argv, cwd, self._launch_signals, emulators=self._emulators
            )
            self._pool.start(task)
            return True

        # Fall back to shell helper for unsupported collection types
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

        extra_zip = ""
        if self._config.gamedata_content_subdir and expected_name:
            gd_dir = os.path.join(self.root, *self._config.gamedata_content_subdir.split("/"))
            candidate = os.path.join(gd_dir, expected_name + ".zip")
            if os.path.isfile(candidate):
                extra_zip = candidate

        task = InstallTask(game.id, zip_path, dest_dir, self._install_signals,
                           extra_zip_path=extra_zip,
                           extra_extract_dir=self.root)
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

        # Resolve effective game ZIP source: if the user pointed to a collection
        # root (e.g. /Volumes/BigDrive/eXoDOS), descend into game_data_subdir
        # (e.g. eXo/eXoDOS/) where the ZIPs actually live.
        # If they pointed directly at a flat directory of ZIPs, use it as-is.
        effective_source = self._zip_source_path
        gamedata_source_path = ""
        if effective_source:
            subdir = self._config.abs_game_data(effective_source)
            if os.path.isdir(subdir):
                effective_source = subdir
            # Resolve GameData extras source from the same collection root.
            if self._config.gamedata_content_subdir:
                gd_candidate = os.path.join(
                    self._zip_source_path,
                    *self._config.gamedata_content_subdir.split("/"),
                )
                if os.path.isdir(gd_candidate):
                    gamedata_source_path = gd_candidate

        # Use the user-configured torrent path; fall back to the in-collection path.
        effective_torrent = self._torrent_path or self._config.torrent_path(self.root)

        gamedata_zip_dir = ""
        if self._config.gamedata_content_subdir:
            gamedata_zip_dir = os.path.join(
                self.root, *self._config.gamedata_content_subdir.split("/")
            )

        dbg(f"Launcher.fetch: gamename={gamename!r}")
        dbg(f"Launcher.fetch: torrent={effective_torrent!r}")
        dbg(f"Launcher.fetch: zip_source={effective_source!r}")
        dbg(f"Launcher.fetch: dest_dir={dest_dir!r}")
        dbg(f"Launcher.fetch: gamedata_zip_dir={gamedata_zip_dir!r}")
        task = FetchTask(
            game_id              = game.id,
            gamename             = gamename,
            zip_dest_dir         = dest_dir,
            extract_dir          = dest_dir,
            aria_index_path      = self._config.aria_index_path(self.root),
            torrent_path         = effective_torrent,
            zip_source_path      = effective_source,
            gamedata_zip_dir     = gamedata_zip_dir,
            gamedata_extract_dir = self.root,
            gamedata_source_path = gamedata_source_path,
            signals              = self._fetch_signals,
        )
        self._active_fetch_task = task
        self._pool.start(task)
        return True

    def cancel_fetch(self) -> None:
        """Cancel the currently active fetch task, if any."""
        if self._active_fetch_task is not None:
            self._active_fetch_task.cancel()

    def download_lite(self, torrent_path: str) -> bool:
        """
        Download the Lite metadata subset for this project and install it.

        Uses aria2c ``--select-file`` to fetch only the XML database, artwork,
        launch scripts, and utility files from the project torrent — skipping
        game ZIPs and optional media packs.

        Returns False immediately if a Lite download is already in progress.
        Progress and completion are reported via ``lite_phase``, ``lite_progress``,
        ``lite_finished``, and ``lite_cancelled`` signals.
        """
        if self._active_lite_task is not None:
            return False

        dbg(f"Launcher.download_lite: torrent={torrent_path!r}")
        dbg(f"Launcher.download_lite: collection_root={self.root!r}")
        task = LiteDownloadTask(
            torrent_path=torrent_path,
            collection_root=self.root,
            signals=self._lite_signals,
        )
        self._active_lite_task = task
        self._pool.start(task)
        return True

    @property
    def is_lite_downloading(self) -> bool:
        """True while a Lite download or setup task is in progress."""
        return self._active_lite_task is not None

    def cancel_lite(self) -> None:
        """Cancel the currently active Lite download, if any."""
        if self._active_lite_task is not None:
            self._active_lite_task.cancel()

    @staticmethod
    def needs_content_setup(root: str, config: ProjectConfig) -> bool:
        """
        Return True when Content/*.zip files are present but the primary XML
        has not yet been extracted — i.e., the collection needs to be set up.
        """
        content_dir = os.path.join(root, "Content")
        if not os.path.isdir(content_dir):
            return False
        has_meta_zip = any(
            (f.startswith("XO") or f.startswith("!")) and f.endswith(".zip")
            for f in os.listdir(content_dir)
        )
        if not has_meta_zip:
            return False
        # Check whether the xml/ directory already has content
        xml_dir = os.path.join(root, "xml")
        if os.path.isdir(xml_dir) and any(os.scandir(xml_dir)):
            return False
        # Also accept Data/Platforms/ as evidence of a prior extraction
        if os.path.isdir(os.path.join(root, "Data", "Platforms")):
            return False
        return True

    def setup_collection(self) -> bool:
        """
        Extract Content/*.zip files into the collection root (setup equivalent
        of the Windows setup .bat).  Returns False if already in progress.
        Progress is reported via the same lite_* signals as download_lite().
        """
        if self._active_lite_task is not None:
            return False
        task = SetupTask(
            collection_root=self.root,
            signals=self._lite_signals,
        )
        self._active_lite_task = task
        self._pool.start(task)
        return True

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
        _, gamename = self._find_gamedir_and_name(game)
        if not gamename:
            return None, ""
        zip_path = os.path.join(self._config.abs_game_data(self.root), gamename + ".zip")
        return (zip_path if os.path.isfile(zip_path) else None), gamename
