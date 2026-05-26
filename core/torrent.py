"""
torrent.py — Minimal .torrent parser for Lite-mode selective download.

Returns file entries with 1-based indices suitable for aria2c's
--select-file flag so that only the non-game-data subset is fetched.

Lite set definition (everything EXCEPT game data and Windows-only files)
------------------------------------------------------------------------
Content/*.zip             — all content ZIPs (metadata, images, books,
                            magazines, soundtracks, videos, …)
                            EXCEPT LaunchBox.zip (Windows frontend)
                            EXCEPT Content/GameData/ (per-game data ZIPs,
                              fetched on demand via install)
                            EXCEPT Content/Dependencies/ (Windows runtimes)
                            EXCEPT zero-byte placeholder entries (no ".")
eXo/util/*                — unzip.exe and per-project util*.zip
<root>/*.pdf, *.txt, …    — project manuals, readmes, catalogues
                            (excludes *.bat and *.exe — Windows-only)

Individual game ZIPs are never Lite:
  eXo/<ProjectDir>/*.zip  — fetched on demand via Download & Install
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class TorrentFile:
    index: int    # 1-based index for aria2c --select-file
    path: str     # forward-slash path relative to the torrent root
    size: int     # bytes


@dataclass
class TorrentInfo:
    name: str
    files: list[TorrentFile]

    def select_lite(self) -> list[int]:
        """Return 1-based file indices for a Lite download."""
        return [f.index for f in self.files if _is_lite(f.path.split("/"))]

    def select_game(self, zip_filename: str) -> list[int]:
        """Return 1-based indices whose basename matches *zip_filename* (case-insensitive)."""
        target = zip_filename.lower()
        return [
            f.index for f in self.files
            if os.path.basename(f.path).lower() == target
        ]

    def lite_size(self) -> int:
        """Total byte size of the Lite subset."""
        indices = set(self.select_lite())
        return sum(f.size for f in self.files if f.index in indices)

    def total_size(self) -> int:
        return sum(f.size for f in self.files)


# ── Lite selection logic ──────────────────────────────────────────────────────

# Top-level Content/ entries that are Windows-only and never downloaded.
_CONTENT_WINDOWS_ONLY = frozenset({"LaunchBox.zip"})

# Content/ subdirectories whose contents are never part of the Lite set.
# GameData/ = per-game data ZIPs (fetched on demand via install).
# Dependencies/ = Windows runtime installers.
_CONTENT_EXCLUDED_DIRS = frozenset({"GameData", "Dependencies"})


def _is_lite(parts: list[str]) -> bool:
    """Return True if this torrent entry belongs to the Lite download subset.

    Only evaluates torrent-level paths (e.g. ``Content/IFBooks.zip``).
    Has no visibility into the contents of ZIP files.
    """
    if not parts:
        return False

    top  = parts[0]
    name = parts[-1]

    # Windows-only executables and batch scripts are never wanted on any
    # platform, regardless of where they appear in the torrent.
    if name.lower().endswith((".bat", ".exe")):
        return False

    # eXo/util/* — unzip helper and per-project util ZIPs.
    if top == "eXo":
        return len(parts) >= 2 and parts[1] == "util"

    if top == "Content":
        if len(parts) < 2:
            return False  # bare "Content" placeholder
        second = parts[1]
        # Subdirectories with per-game or Windows-only content.
        if second in _CONTENT_EXCLUDED_DIRS:
            return False
        # Windows-only top-level Content entries.
        if second in _CONTENT_WINDOWS_ONLY:
            return False
        # Zero-byte placeholder files have no file extension.
        if "." not in second:
            return False
        # Everything else in Content/ is Lite (metadata, books, magazines,
        # soundtracks, videos, catalogues, …).
        return True

    # Root-level files: manuals, readmes, catalogues, version markers.
    # .bat/.exe already excluded above.
    if len(parts) == 1:
        return "." in name  # skip extensionless placeholders

    # eXo/<ProjectDir>/*.zip and anything else — not Lite.
    return False


# ── parser ────────────────────────────────────────────────────────────────────

def parse(torrent_path: str) -> TorrentInfo:
    """Parse a .torrent file and return a :class:`TorrentInfo`."""
    with open(torrent_path, "rb") as fh:
        data = fh.read()

    meta, _ = _bdecode(data)
    assert isinstance(meta, dict)
    info = meta[b"info"]
    assert isinstance(info, dict)

    name = info[b"name"]
    assert isinstance(name, bytes)
    top_name = name.decode("utf-8", errors="replace")

    files: list[TorrentFile] = []
    raw_files = info.get(b"files")
    if raw_files is not None:
        assert isinstance(raw_files, list)
        for i, entry in enumerate(raw_files, start=1):
            assert isinstance(entry, dict)
            raw_path = entry[b"path"]
            assert isinstance(raw_path, list)
            parts = [
                p.decode("utf-8", errors="replace")
                for p in raw_path
                if isinstance(p, bytes)
            ]
            raw_len = entry[b"length"]
            assert isinstance(raw_len, int)
            files.append(TorrentFile(index=i, path="/".join(parts), size=raw_len))
    else:
        raw_len = info[b"length"]
        assert isinstance(raw_len, int)
        files.append(TorrentFile(index=1, path=top_name, size=raw_len))

    return TorrentInfo(name=top_name, files=files)


# ── minimal bencode decoder ───────────────────────────────────────────────────

_BVal = bytes | int | list | dict


def _bdecode(data: bytes, idx: int = 0) -> tuple[_BVal, int]:
    c = chr(data[idx])
    if c == "i":
        end = data.index(b"e", idx + 1)
        return int(data[idx + 1 : end]), end + 1
    if c == "l":
        idx += 1
        result: list[_BVal] = []
        while chr(data[idx]) != "e":
            v, idx = _bdecode(data, idx)
            result.append(v)
        return result, idx + 1
    if c == "d":
        idx += 1
        d: dict[_BVal, _BVal] = {}
        while chr(data[idx]) != "e":
            k, idx = _bdecode(data, idx)
            v, idx = _bdecode(data, idx)
            d[k] = v
        return d, idx + 1
    colon = data.index(b":", idx)
    n = int(data[idx:colon])
    s = data[colon + 1 : colon + 1 + n]
    return s, colon + 1 + n
