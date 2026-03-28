"""
aria_index.py — Parse eXo project torrent index files and build aria2c commands.

The index.txt file in eXo/util/aria/ maps every file in the full project
torrent to a number.  This allows aria2c to download just the ZIP needed for
a single game without fetching the entire multi-hundred-GB archive.

Index format (one entry per line):
    <number>:<filename_or_path>:<size_string> (<bytes>)

Examples:
    1:Gabriel Knight 2 - The Beast Within (1995).zip:5.9GiB (6,436,799,422)
    8:./GameData/eXoDOS/!Bingo Granny! (2002).zip:351KiB (360,060)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field


# ── data types ────────────────────────────────────────────────────────────────

@dataclass
class GameEntry:
    """Torrent index information for a single game."""
    game_index:       int   # 1-based file number in the torrent for the main game ZIP
    game_size_str:    str   # human-readable size, e.g. "45 MiB"
    game_size_bytes:  int   # size in bytes (0 if un-parseable)
    media_index:      int   # file number for the media ZIP (0 = not present)
    media_size_str:   str   # human-readable size of the media ZIP
    media_size_bytes: int   # size in bytes (0 if not present)

    @property
    def total_size_str(self) -> str:
        """Human-readable combined download size."""
        if not self.media_size_bytes:
            return self.game_size_str
        total = self.game_size_bytes + self.media_size_bytes
        if total >= 1_073_741_824:
            return f"{total / 1_073_741_824:.1f} GiB"
        if total >= 1_048_576:
            return f"{total / 1_048_576:.0f} MiB"
        if total >= 1_024:
            return f"{total / 1_024:.0f} KiB"
        return f"{total} B"


# ── index parsing ─────────────────────────────────────────────────────────────

_SIZE_RE = re.compile(r"^([\d.]+ \w+)\s+\((\d[\d,]*)\)$")


def _parse_size(size_str: str) -> tuple[str, int]:
    """Return (human_str, bytes) from a token like '45 MiB (47185920)'."""
    m = _SIZE_RE.match(size_str.strip())
    if m:
        try:
            return m.group(1), int(m.group(2).replace(",", ""))
        except ValueError:
            return m.group(1), 0
    return size_str.strip(), 0


def load_index(index_path: str) -> dict[str, GameEntry]:
    """
    Load the full index file into a ``{gamename: GameEntry}`` dict.

    *gamename* is the ZIP stem without the ``.zip`` extension, e.g.
    ``"Dune 2 - The Building of a Dynasty (1992)"``.

    Both the main game ZIP and the optional media ZIP entries are read and
    merged into the same GameEntry so callers get all torrent indices in one
    lookup.
    """
    if not os.path.isfile(index_path):
        return {}

    # Two-pass: first collect all entries, then merge main + media per game.
    # Entry key patterns:
    #   Main game: "<gamename>.zip"
    #   Media:     "./GameData/<project>/<gamename>.zip"
    main: dict[str, tuple[int, str, int]] = {}   # gamename → (idx, size_str, bytes)
    media: dict[str, tuple[int, str, int]] = {}

    try:
        with open(index_path, "r", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                # Split on the first two colons only — filenames may contain colons
                parts = line.split(":", 2)
                if len(parts) < 3:
                    continue
                idx_str, entry_path, size_part = parts
                try:
                    idx = int(idx_str)
                except ValueError:
                    continue

                human, nbytes = _parse_size(size_part)
                entry_name = entry_path.strip()

                if not entry_name.endswith(".zip"):
                    continue

                if "/GameData/" in entry_name:
                    # Media ZIP: "./GameData/eXoDOS/<gamename>.zip"
                    gamename = os.path.basename(entry_name)[:-4]  # strip .zip
                    media[gamename] = (idx, human, nbytes)
                else:
                    # Main game ZIP — entry may be a bare filename ("Game (1992).zip")
                    # or a full path ("eXo/eXoWin3x/Game (1992).zip"); use basename
                    # so both formats resolve to the same gamename.
                    gamename = os.path.basename(entry_name)[:-4]
                    main[gamename] = (idx, human, nbytes)
    except OSError:
        return {}

    result: dict[str, GameEntry] = {}
    for gamename, (idx, size_str, nbytes) in main.items():
        med = media.get(gamename, (0, "", 0))
        result[gamename] = GameEntry(
            game_index=idx,
            game_size_str=size_str,
            game_size_bytes=nbytes,
            media_index=med[0],
            media_size_str=med[1],
            media_size_bytes=med[2],
        )

    return result


# ── aria2c detection ──────────────────────────────────────────────────────────

def find_aria2c() -> str | None:
    """
    Return the aria2c command string for the current platform, or None.

    Linux: prefers the Flatpak-packaged ``retro_exo.aria2c``; falls back to
    the native ``aria2c`` binary if available.
    macOS: looks for the Homebrew-installed binary first, then PATH.
    """
    if sys.platform.startswith("linux"):
        try:
            result = subprocess.run(
                ["flatpak", "list", "--app"],
                capture_output=True, text=True, timeout=5,
            )
            if "retro_exo.aria2c" in result.stdout:
                return "flatpak run com.retro_exo.aria2c"
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        if shutil.which("aria2c"):
            return "aria2c"
        return None

    # macOS — check common Homebrew prefixes before falling back to PATH
    for candidate in ("/opt/homebrew/bin/aria2c", "/usr/local/bin/aria2c"):
        if os.path.isfile(candidate):
            return candidate
    if shutil.which("aria2c"):
        return "aria2c"
    return None


# ── command builder ───────────────────────────────────────────────────────────

def build_aria2c_command(
    aria2c_cmd: str,
    torrent_path: str,
    files: list[tuple[int, str]],
) -> list[str]:
    """
    Build the argv list for a selective aria2c torrent download.

    Parameters
    ----------
    aria2c_cmd : str
        The aria2c executable / flatpak invocation string.
    torrent_path : str
        Absolute path to the ``.torrent`` file.
    files : list of (torrent_index, output_filename)
        Each entry selects one file from the torrent by its 1-based index
        (from index.txt) and names the output file.

    Returns
    -------
    list[str]
        Ready to pass to ``subprocess.Popen``.
    """
    argv = aria2c_cmd.split()
    # --select-file takes a comma-separated list of 1-based indices
    argv.append(f"--select-file={','.join(str(idx) for idx, _ in files)}")
    for idx, fname in files:
        argv.append(f"--index-out={idx}={fname}")
    argv += [
        "--file-allocation=none",
        "--allow-overwrite=true",
        "--seed-time=0",
        torrent_path,
    ]
    return argv
