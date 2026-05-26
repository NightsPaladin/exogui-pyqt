"""
emulator_deps.py — Emulator dependency specifications for eXo collections.

Defines which emulators each collection requires, how to detect them on the
host system, and how to install them (Homebrew formula/cask, or download URL).
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EmulatorSpec:
    name: str               # internal key — matches dosbox_macos.txt / project default_emulator
    display_name: str       # shown in UI
    projects: list[str]     # project IDs that need this emulator

    check_commands: list[str] = field(default_factory=list)   # tried via shutil.which()
    check_app_paths: list[str] = field(default_factory=list)  # macOS .app bundle paths

    brew_formula: str = ""  # brew install <formula>
    brew_cask: str = ""     # brew install --cask <cask>
    download_url: str = ""  # shown as "Download…" button when no Homebrew option
    notes: str = ""         # tooltip / description shown in the dialog


def _brew_augmented_path() -> str:
    """Return PATH with Homebrew prefixes prepended (macOS only)."""
    parts = list(os.environ.get("PATH", "").split(os.pathsep))
    if sys.platform == "darwin":
        for prefix in ("/opt/homebrew", "/usr/local"):
            for sub in ("bin", "sbin"):
                p = f"{prefix}/{sub}"
                if os.path.isdir(p) and p not in parts:
                    parts.insert(0, p)
    return os.pathsep.join(parts)


def check_emulator(spec: EmulatorSpec) -> Optional[str]:
    """Return the resolved path or command string if the emulator is found, else None."""
    search_path = _brew_augmented_path()

    for cmd in spec.check_commands:
        found = shutil.which(cmd, path=search_path)
        if found:
            return found

    for raw_path in spec.check_app_paths:
        expanded = os.path.expanduser(raw_path)
        if os.path.exists(expanded):
            return expanded

    return None


# ---------------------------------------------------------------------------
# Emulator specs — all supported collections (eXoWin9x excluded for now)
# ---------------------------------------------------------------------------

ALL_EMULATOR_SPECS: list[EmulatorSpec] = [
    EmulatorSpec(
        name="dosbox-staging",
        display_name="DOSBox Staging",
        projects=["exodos", "exowin3x", "exodemoscene"],
        check_commands=["dosbox-staging"],
        check_app_paths=["/Applications/DOSBox-Staging.app"],
        brew_formula="dosbox-staging",
        notes="Primary DOSBox variant; used by the majority of eXoDOS and eXoWin3x titles.",
    ),
    EmulatorSpec(
        name="dosbox-x",
        display_name="DOSBox-X",
        projects=["exodos", "exowin3x"],
        check_commands=["dosbox-x"],
        check_app_paths=["/Applications/DOSBox-X.app"],
        brew_formula="dosbox-x",
        notes="Enhanced-compatibility DOSBox fork; used by a subset of eXoDOS/Win3x titles.",
    ),
    EmulatorSpec(
        name="scummvm",
        display_name="ScummVM",
        projects=["exoscummvm", "exodos", "exowin3x"],
        check_commands=["scummvm"],
        check_app_paths=["/Applications/ScummVM.app"],
        brew_formula="scummvm",
        notes="Required for eXoScummVM; also handles some eXoDOS/Win3x adventure games.",
    ),
    EmulatorSpec(
        name="86box",
        display_name="86Box",
        projects=["exodos"],
        check_commands=["86Box", "86box"],
        check_app_paths=["/Applications/86Box.app"],
        brew_cask="86box",
        notes="Optional hardware-accurate PC emulator used by a small number of eXoDOS titles.",
    ),
    EmulatorSpec(
        name="dreamm",
        display_name="DREAMM",
        projects=["exodreamm"],
        check_commands=["dreamm"],
        check_app_paths=[
            "/Applications/DREAMM.app",
            "~/Applications/DREAMM.app",
        ],
        download_url="https://dreamm.aarongiles.com/",
        notes="LucasArts-specific emulator required for eXoDREAMM. Not available via Homebrew.",
    ),
    EmulatorSpec(
        name="kegs",
        display_name="KEGS",
        projects=["exoappleiigs"],
        check_commands=["kegs"],
        download_url="https://kegs.sourceforge.net/",
        notes="Recommended primary Apple IIGS emulator for eXoAppleIIGS. "
              "KEGS is the upstream project that GSplus forked from; active development "
              "resumed in 2020 and it now surpasses GSplus in features and compatibility. "
              "Prebuilt macOS binaries available on SourceForge. "
              "ROM files (ROM1/ROM3) are bundled in the eXoAppleIIGS collection.",
    ),
    EmulatorSpec(
        name="gsplus",
        display_name="GSplus",
        projects=["exoappleiigs"],
        check_commands=["gsplus"],
        check_app_paths=[
            "/Applications/GSplus.app",
            "~/Applications/GSplus.app",
        ],
        download_url="https://github.com/digarok/gsplus",
        notes="Legacy Apple IIGS emulator — no longer developed and has no prebuilt macOS "
              "binaries (must be compiled from source). KEGS is the recommended replacement.",
    ),
    EmulatorSpec(
        name="mame",
        display_name="MAME",
        projects=["exoappleiigs"],
        check_commands=["mame"],
        brew_formula="mame",
        notes="Used by ~17% of eXoAppleIIGS titles as a secondary Apple IIGS emulator.",
    ),
    EmulatorSpec(
        name="martypc",
        display_name="MartyPC",
        projects=["exodemoscene"],
        check_commands=["martypc"],
        check_app_paths=[
            "/Applications/martypc.app",
            "~/Applications/martypc.app",
        ],
        download_url="https://github.com/dbalsom/martypc/releases",
        notes="Cycle-accurate 8088 PC emulator used by some eXoDemoScene productions.",
    ),
    EmulatorSpec(
        name="frotz",
        display_name="Frotz",
        projects=["exoif"],
        check_commands=["frotz"],
        brew_formula="frotz",
        notes="Z-Machine interpreter; primary engine for eXoIF text adventures.",
    ),
    EmulatorSpec(
        name="gargoyle",
        display_name="Gargoyle",
        projects=["exoif"],
        check_commands=["gargoyle"],
        check_app_paths=[
            "/Applications/Gargoyle.app",
            "~/Applications/Gargoyle.app",
        ],
        download_url="https://ccxvii.net/gargoyle/",
        notes="Multi-format IF interpreter for eXoIF; supports formats beyond Z-Machine.",
    ),
]

_BY_NAME: dict[str, EmulatorSpec] = {s.name: s for s in ALL_EMULATOR_SPECS}


def get_spec(name: str) -> Optional[EmulatorSpec]:
    """Return the EmulatorSpec for *name*, or None if unknown."""
    return _BY_NAME.get(name)


def specs_for_projects(project_ids: set[str]) -> list[EmulatorSpec]:
    """Return specs whose projects list overlaps with *project_ids*."""
    return [s for s in ALL_EMULATOR_SPECS if set(s.projects) & project_ids]


# ---------------------------------------------------------------------------
# Emulator groups — used to organise the Settings UI and the downloads dialog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EmulatorGroup:
    label: str             # section header shown in the UI
    spec_names: list[str]  # ordered list of EmulatorSpec.name values in this group

    def specs(self) -> list[EmulatorSpec]:
        """Return EmulatorSpec objects for this group (skips unknown names)."""
        return [s for s in (_BY_NAME.get(n) for n in self.spec_names) if s is not None]


EMULATOR_GROUPS: list[EmulatorGroup] = [
    EmulatorGroup(
        label="eXoDOS · eXoWin3x · eXoDemoScene",
        spec_names=["dosbox-staging", "dosbox-x", "86box"],
    ),
    EmulatorGroup(
        label="eXoScummVM · eXoDOS · eXoWin3x",
        spec_names=["scummvm"],
    ),
    EmulatorGroup(
        label="eXoDREAMM",
        spec_names=["dreamm"],
    ),
    EmulatorGroup(
        label="eXoAppleIIGS",
        spec_names=["kegs", "gsplus", "mame"],
    ),
    EmulatorGroup(
        label="eXoDemoScene",
        spec_names=["martypc"],
    ),
    EmulatorGroup(
        label="eXoIF",
        spec_names=["frotz", "gargoyle"],
    ),
]
