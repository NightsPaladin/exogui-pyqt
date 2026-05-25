"""
torrent.py — Minimal .torrent parser for Lite-mode selective download.

Returns file entries with 1-based indices suitable for aria2c's
--select-file flag so that only the metadata subset (images, XML, launch
scripts) is fetched rather than the full game-ZIP library.

Lite set definition
-------------------
Content/XO*Metadata.zip   — images + LaunchBox XML database
Content/!*metadata.zip    — per-game launch scripts and DOSBox configs
eXo/util/*                — unzip.exe and per-project util*.zip

Everything else is excluded from the Lite set:
  Content/LaunchBox.zip   — Windows-only frontend application
  Content/*.zip (other)   — optional media packs (videos, magazines, books)
  eXo/<ProjectDir>/*.zip  — individual game ZIPs (fetched on demand)
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

def _is_lite(parts: list[str]) -> bool:
    """Return True if this path belongs to the Lite download subset."""
    if not parts:
        return False
    top = parts[0]

    if top == "eXo":
        return len(parts) >= 2 and parts[1] == "util"

    if top == "Content":
        if len(parts) < 2:
            return True   # zero-byte placeholder / marker file
        name = parts[1]
        if name.startswith("XO") and "Metadata" in name and name.endswith(".zip"):
            return True
        if name.startswith("!") and "metadata" in name.lower() and name.endswith(".zip"):
            return True
        return False

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
