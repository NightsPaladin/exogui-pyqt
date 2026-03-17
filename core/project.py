"""
project.py — ProjectConfig dataclass describing an eXo collection's path
conventions, XML structure, and emulator defaults.

Two built-in configs are provided:
  EXODOS   — the classic eXoDOS DOS-game collection
  EXOWIN3X — the eXoWin3x Windows 3.x game collection

A third-party or future collection can define its own ProjectConfig and pass
it to GameLibrary / Launcher at construction time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ProjectConfig:
    """
    Describes the path layout and metadata conventions for one eXo collection.

    Parameters
    ----------
    id : str
        Short machine-readable key, e.g. ``"exodos"`` or ``"exowin3x"``.
    display_name : str
        Human-readable name shown in the UI, e.g. ``"eXoDOS"``.
    platform_tag : str
        Value of ``<Platform>`` in the LaunchBox XML, e.g. ``"MS-DOS"``.
    xml_variants : dict[str, str]
        Mapping of xml_mode → path relative to collection root.
        Must contain at least the key ``"auto"`` as the default.
    image_subdir : str
        Path relative to collection root where per-game artwork lives,
        e.g. ``"Images/MS-DOS"``.
    game_data_subdir : str
        Path relative to collection root where game ZIPs are extracted,
        e.g. ``"eXo/eXoDOS"``.
    scripts_subdir : str
        Path relative to collection root where per-game launch scripts live,
        e.g. ``"eXo/eXoDOS/!dos"``.
    default_emulator : str
        Emulator name used when the per-game emulator map has no entry,
        e.g. ``"dosbox-staging"``.
    detect_marker : str
        Path relative to collection root whose existence identifies this
        project type, e.g. ``"eXo/eXoDOS"``.
    """

    id: str
    display_name: str
    platform_tag: str
    xml_variants: dict          # mode → relative path from collection root
    image_subdir: str           # e.g. "Images/MS-DOS"
    game_data_subdir: str       # e.g. "eXo/eXoDOS"
    scripts_subdir: str         # e.g. "eXo/eXoDOS/!dos"
    default_emulator: str       # fallback when emulator map has no entry
    detect_marker: str          # relative path to test for auto-detection
    torrent_name: str = ""      # torrent filename in eXo/util/aria/, e.g. "eXoDOS.torrent"

    def xml_path(self, root: str, xml_mode: str = "auto") -> str:
        """Return the absolute path to the XML file for the given mode."""
        rel = self.xml_variants.get(xml_mode) or self.xml_variants.get("auto", "")
        return os.path.join(root, *rel.replace("\\", "/").split("/"))

    def abs_image_base(self, root: str) -> str:
        return os.path.join(root, *self.image_subdir.split("/"))

    def abs_game_data(self, root: str) -> str:
        return os.path.join(root, *self.game_data_subdir.split("/"))

    def abs_scripts(self, root: str) -> str:
        return os.path.join(root, *self.scripts_subdir.split("/"))

    def aria_index_path(self, root: str) -> str:
        """Absolute path to the aria2c index file."""
        return os.path.join(root, "eXo", "util", "aria", "index.txt")

    def torrent_path(self, root: str) -> str:
        """Absolute path to the project torrent file, or '' if not configured."""
        if not self.torrent_name:
            return ""
        return os.path.join(root, "eXo", "util", "aria", self.torrent_name)


# ---------------------------------------------------------------------------
# Built-in project configurations
# ---------------------------------------------------------------------------

EXODOS = ProjectConfig(
    id="exodos",
    display_name="eXoDOS",
    platform_tag="MS-DOS",
    xml_variants={
        "auto":    "Data/Platforms/MS-DOS.xml",
        "all":     "xml/all/MS-DOS.xml",
        "family":  "xml/family/MS-DOS.xml",
        "kidsafe": "xml/kidsafe/MS-DOS.xml",
    },
    image_subdir="Images/MS-DOS",
    game_data_subdir="eXo/eXoDOS",
    scripts_subdir="eXo/eXoDOS/!dos",
    default_emulator="dosbox-staging",
    detect_marker="eXo/eXoDOS",
    torrent_name="eXoDOS.torrent",
)

EXOWIN3X = ProjectConfig(
    id="exowin3x",
    display_name="eXoWin3x",
    platform_tag="Windows 3x",
    xml_variants={
        "auto":   "xml/Windows 3x.xml",
        "all":    "xml/Windows 3x.xml",
        "family": "xml/WinFAMILY.xml",
    },
    image_subdir="Images/Windows 3x",
    game_data_subdir="eXo/eXoWin3x",
    scripts_subdir="eXo/eXoWin3x/!win3x",
    default_emulator="dosbox-staging",  # ECE not available on macOS/Linux; staging is the fallback
    detect_marker="eXo/eXoWin3x",
    torrent_name="eXoWin3x.torrent",
)

ALL_PROJECTS: list[ProjectConfig] = [EXODOS, EXOWIN3X]

_BY_ID: dict[str, ProjectConfig] = {p.id: p for p in ALL_PROJECTS}


def get_project(project_id: str) -> Optional[ProjectConfig]:
    """Return the ProjectConfig for *project_id*, or None if unknown."""
    return _BY_ID.get(project_id)


def detect_project(root: str) -> Optional[ProjectConfig]:
    """
    Auto-detect which project type lives at *root* by checking for known
    marker paths.  Returns the first match or None.
    """
    for config in ALL_PROJECTS:
        marker = os.path.join(root, *config.detect_marker.split("/"))
        if os.path.isdir(marker):
            return config
    return None
